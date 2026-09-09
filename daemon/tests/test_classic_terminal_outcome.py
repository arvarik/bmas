"""The terminal outcome of a Classic native run.

The Classic reason table registers through the reason registry and the
benchmark mapping ``outcome-mapping-classic-2``. Every native run ends
with exactly one ``terminal_outcome`` journal record the runtime
authored, the journal derives the run state from its common class, and
a second outcome never commits.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

import config
import database as db
import interactive_admission as admission
import runtime_journal as journal
import settings_store
from benchmarks import outcome_mappings
from core import foundation_gates
from core.gateway import LeaseLostError
from core.run_contracts import (
    SHARED_PRE_EXECUTION_REASONS,
    OutcomeClass,
    UnregisteredReasonError,
)
from core.variants import RuntimeKey, VariantConfigurationError, VariantExecutionRequest
from core.variants.classic import ClassicRuntime, outcomes, projection

if TYPE_CHECKING:
    from core.variants.classic.runtime import NativeRunBinding

NATIVE = RuntimeKey("classic", "2")
LEGACY = RuntimeKey("classic", "1")
TASK_ID = "task-native-outcome"
LOCK_ID = "lock-native-outcome"


@pytest_asyncio.fixture
async def native_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "outcome.db"))
    monkeypatch.setattr(config, "FOUNDATION_GATES", {name: True for name in foundation_gates.PLANNED_WRITER_GATES}, raising=False)
    monkeypatch.setattr(config, "STORAGE_OPERATOR_CONFIRMED", True, raising=False)
    monkeypatch.setattr(config, "REQUIRE_PROVIDER_QUALIFICATION", False, raising=False)
    monkeypatch.setattr(config, "ADMIT_TEST_ONLY_RUNTIMES", True, raising=False)
    monkeypatch.setattr(config, "ROLE_REGISTRY", {
        "planner": {"profile": "planner", "endpoints": ["http://agent.test"]},
        "critic": {"profile": "critic", "endpoints": ["http://agent.test"]},
        "decider": {"profile": "decider", "endpoints": ["http://agent.test"]},
    }, raising=False)
    monkeypatch.setattr(config, "MODEL_PRICING", {
        "test-light": {"input_cost_per_token": "0.0000001", "output_cost_per_token": "0.0000004", "source": "test"},
    }, raising=False)
    monkeypatch.setattr(settings_store, "_store", None)
    admission.reset_for_tests()
    await db.init_db()
    envelope = await ClassicRuntime.capture_configuration({"effort": "quick"})
    await db.create_task_with_meta(
        TASK_ID, "native outcome", "Add 20 and 22.", "classic",
        {"effective_configuration": envelope}, runtime_contract_version="2", run_state="staging",
    )
    admitted = await admission.admit_task_run(task_id=TASK_ID, runtime_key=NATIVE, effective_configuration=envelope)
    assert admitted is not None
    context = await admission.run_context_for(admitted["run_id"], lease_ref=LOCK_ID)
    services = await admission.runtime_services_for(
        context, lease_owner="orchestrator", lease_fence=LOCK_ID, lease_ttl_seconds=60.0,
        artifact_root=tmp_path / "classic-board",
    )
    assert await services.task_leases.acquire() is True
    binding = ClassicRuntime.bind_run(VariantExecutionRequest(
        task_id=TASK_ID, session_id="s", user_task="Add 20 and 22.", triage=None,
        run_context=context, runtime_services=services,
    ))
    assert binding is not None
    yield {"run_id": admitted["run_id"], "context": context, "binding": binding, "admitted": admitted}
    monkeypatch.setattr(settings_store, "_store", None)


# ── The reason table ─────────────────────────────────────────────────


def test_the_classic_reasons_register_every_common_class_and_the_shared_reasons():
    registry = ClassicRuntime.reason_registry()
    mapping = registry.mapping_for(NATIVE)
    assert set(outcomes.CLASSIC_REASON_CLASSES) <= set(mapping)
    assert set(SHARED_PRE_EXECUTION_REASONS) <= set(mapping)
    assert {binding.common_class for binding in mapping.values()} == set(OutcomeClass)
    assert registry.mapping_version_for(NATIVE) == "classic-reasons/1"
    assert registry.binding_for(NATIVE, "completed").common_class is OutcomeClass.SUCCESS
    assert registry.binding_for(NATIVE, "cancelled").common_class is OutcomeClass.CANCELLATION
    assert registry.binding_for(NATIVE, "integrity_failure").retryable is True
    assert registry.binding_for(NATIVE, "no_valid_candidate").retryable is False
    with pytest.raises(UnregisteredReasonError):
        registry.binding_for(NATIVE, "execution")
    with pytest.raises(UnregisteredReasonError):
        registry.mapping_for(LEGACY)
    # The same registry instance serves every call.
    assert ClassicRuntime.reason_registry() is registry


def test_the_benchmark_mapping_carries_the_classic_table_for_the_native_pair():
    native = outcome_mappings.registered_mapping("classic", "2")
    legacy = outcome_mappings.registered_mapping("classic", "1")
    assert native.mapping_id == "outcome-mapping-classic-2"
    assert legacy.mapping_id == "outcome-mapping-classic-1"
    assert native.digest != legacy.digest
    for reason, rules in outcomes.CLASSIC_TASK_REASONS.items():
        assert native.resolve(reason) == rules
    assert native.resolve("paper_aligned_context_limit_exceeded")["benchmark_class"] == "substantive_failure"
    assert native.resolve("integrity_failure") == {
        "benchmark_class": "infrastructure_failure", "retry_rule": "allowed",
        "missingness": "excludable", "denominator": "excludable",
    }
    assert native.resolve("cancelled") == legacy.resolve("cancelled")
    # The shared task reasons of the legacy lifecycle still resolve for the native pair.
    assert native.resolve("timeout")["benchmark_class"] == "substantive_failure"
    with pytest.raises(outcome_mappings.OutcomeMappingError):
        legacy.resolve("no_valid_candidate")
    # Every Classic benchmark row validates against the shared vocabulary.
    outcome_mappings.build_outcome_mapping(
        runtime_id="classic", runtime_contract_version="2", reasons=outcomes.CLASSIC_TASK_REASONS,
    )


def test_the_reason_of_a_result_and_of_a_failure():
    assert outcomes.reason_for_result({"answer": "42", "terminated_by": "solution"}) == "completed"
    assert outcomes.reason_for_result({"answer": "42", "terminated_by": "budget"}) == "completed"
    assert outcomes.reason_for_result({"answer": "", "terminated_by": "budget"}) == "budget_exhausted"
    assert outcomes.reason_for_result({"answer": " ", "terminated_by": "duration"}) == "deadline_exceeded"
    assert outcomes.reason_for_result({"answer": "", "terminated_by": "stalled"}) == "stalled"
    assert outcomes.reason_for_result({"answer": "", "terminated_by": "no_available_agents"}) == "no_available_agents"
    assert outcomes.reason_for_result({"answer": "", "terminated_by": "max_rounds"}) == "no_valid_candidate"
    assert outcomes.reason_for_result({}) == "no_valid_candidate"

    assert outcomes.reason_for_exception(asyncio.CancelledError(), phase="rounds") == "cancelled"
    assert outcomes.reason_for_exception(RuntimeError("Task aborted by operator"), phase="rounds") == "cancelled"
    assert outcomes.reason_for_exception(projection.RunCancelledError("cancelled"), phase="rounds") == "cancelled"
    assert outcomes.reason_for_exception(projection.RunDeadlineError("deadline"), phase="rounds") == "deadline_exceeded"
    assert outcomes.reason_for_exception(projection.ClassicIntegrityError("bad"), phase="rounds") == "integrity_failure"
    assert outcomes.reason_for_exception(journal.JournalIntegrityError("bad"), phase="genesis") == "integrity_failure"
    assert outcomes.reason_for_exception(ValueError("boom"), phase="genesis") == "genesis_failure"
    assert outcomes.reason_for_exception(ValueError("boom"), phase="start") == "initialization_failure"
    assert outcomes.reason_for_exception(ValueError("boom"), phase="rounds") == "no_valid_candidate"
    assert outcomes.reason_for_exception(ValueError("boom"), phase="finalize") == "no_valid_candidate"
    # Recoverable stops write no outcome.
    assert outcomes.reason_for_exception(LeaseLostError("lost"), phase="rounds") is None
    assert outcomes.reason_for_exception(projection.ClassicFenceError("epoch"), phase="start") is None
    assert outcomes.reason_for_exception(journal.JournalFenceError("stale"), phase="rounds") is None
    assert outcomes.reason_for_exception(VariantConfigurationError("blocked"), phase="start") is None


# ── The terminal record ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_native_run_ends_with_exactly_one_terminal_outcome(native_run):
    binding: NativeRunBinding = native_run["binding"]
    run_id = native_run["run_id"]
    references = binding.promote_final_answer("42")
    record = await binding.ensure_terminal_outcome(
        "completed", final_references=references,
        detail={"terminated_by": "solution", "answer_source": "decider", "budget_spent_usd": 0.01},
    )
    assert record.operation_type == "terminal_outcome"
    assert record.authority_type == "runtime"
    assert record.producer == "classic-runtime"
    payload = record.payload
    assert payload["outcome_id"] == f"outcome-{run_id}"
    assert payload["common_class"] == "success"
    assert payload["reason_code"] == "completed"
    assert payload["mapping_version"] == "classic-reasons/1"
    assert payload["final_references"] == list(references)
    assert payload["resource_references"] == [native_run["admitted"]["budget_id"], native_run["admitted"]["reservation_id"]]
    assert payload["policy_set_digest"] == native_run["context"].policy_set_digest
    assert payload["specification_digest"] == native_run["context"].effective_spec_digest
    assert payload["terminal_detail"]["budget_spent_usd"] == "0.01"
    chain = await journal.read_journal(run_id=run_id)
    assert payload["terminal_journal_cursor"] == chain[-2].journal_cursor
    assert projection.read_text(binding.artifacts, references[0]) == "42"
    # The journal reflected the run state from the common class.
    assert (await db.get_run(run_id))["state"] == "completed"

    # A second request returns the same record and writes nothing.
    again = await binding.ensure_terminal_outcome("no_valid_candidate")
    assert again.journal_cursor == record.journal_cursor
    fresh = await outcomes.commit_terminal_outcome(context=native_run["context"], reason_code="cancelled")
    assert fresh.journal_cursor == record.journal_cursor
    assert len([r for r in await journal.read_journal(run_id=run_id) if r.operation_type == "terminal_outcome"]) == 1
    # The journal itself refuses a second outcome under another token.
    with pytest.raises(journal.JournalError, match="exactly one"):
        await journal.commit_operation(journal.JournalOperation(
            operation_type="terminal_outcome", task_id=TASK_ID, run_id=run_id,
            runtime_id="classic", runtime_contract_version="2",
            payload={**payload, "outcome_id": "outcome-other"}, idempotency_token="terminal-other",
        ))
    # A terminal run accepts no further board mutation.
    with pytest.raises(journal.JournalError, match="terminal"):
        await binding.commit(projection.BoardMutation(
            kind="append", decision="accepted", task_id=TASK_ID, actor="planner", activation_id="late",
            round=1, token="late:0", proposal={"entry": {}},
            section={"schema_version": "1", "kind": "append", "actor": "planner", "activation_id": "late",
                     "round": 1, "mutation_id": None, "entries": [{
                         "entry_id": "e-1", "entry_type": "plan", "author": "planner", "activation_id": "late",
                         "round": 1, "space": "public", "title": None, "body_digest": projection.body_digest("x"),
                         "refs": [], "sources": [], "confidence": "0.5", "status": "open"}],
                     "status_changes": [], "tombstones": []},
            bodies={"e-1": "x"},
        ))
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_a_cancelled_run_and_a_pre_execution_failure_end_with_their_reasons(native_run):
    context = native_run["context"]
    # A stale fence never writes an outcome.
    stale = dataclasses.replace(context, task_fence="fence-stale")
    with pytest.raises(journal.JournalFenceError):
        await outcomes.commit_terminal_outcome(context=stale, reason_code="cancelled")
    with pytest.raises(UnregisteredReasonError):
        await outcomes.commit_terminal_outcome(context=context, reason_code="not-a-reason")
    assert await outcomes.terminal_outcome_record(native_run["run_id"]) is None
    cancelled = await outcomes.commit_terminal_outcome(
        context=context, reason_code="cancelled", detail={"error": "Task aborted by operator"},
    )
    assert cancelled.payload["common_class"] == "cancellation"
    assert (await db.get_run(native_run["run_id"]))["state"] == "cancelled"
    # The shared pre-execution reason resolves through the same registry.
    binding = ClassicRuntime.reason_registry().binding_for(NATIVE, "initialization_failure")
    assert binding.common_class is OutcomeClass.INFRASTRUCTURE_FAILURE
