"""The behavioral conformance suite runs real services per runtime pair.

The reference runtime executes in process and proves the native
column. The legacy pairs prove their compatibility column through
the durable footprint a legacy execution leaves. A regression in the
reference runtime or a legacy path that writes native rows fails the
pair, and the release gate ledger records only a full pass.
"""

from __future__ import annotations

import dataclasses

import pytest
import pytest_asyncio

import capability_publication as cap
import conformance_behavior as behavior
import database as db
import release_gates
from core import variants
from core.variants import RuntimeKey, VariantConfigurationError
from core.variants.reference import ReferenceVariantRuntime, build_configuration

REFERENCE = RuntimeKey("reference", "1")
LEGACY_PAIRS = (RuntimeKey("classic", "1"), RuntimeKey("patchboard", "1"), RuntimeKey("stigmergic", "1"))
STAGE0_QUALIFIED = (REFERENCE, *LEGACY_PAIRS)


@pytest_asyncio.fixture
async def conformance_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "conformance.db"))
    await db.init_db()
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("key", STAGE0_QUALIFIED, ids=lambda key: f"{key.runtime_id}-{key.runtime_contract_version}")
async def test_each_stage0_pair_passes_the_behavioral_suite(conformance_db, key):
    record = cap.CapabilityDirectory().get(key)
    env = await behavior.prepare_environment(record, behavior.executor_for(record), conformance_db)
    report = await behavior.run_behavioral_suite(env)
    assert report.passed, [dataclasses.asdict(result) for result in report.failures()]
    assert [result.case_id for result in report.case_results] == list(behavior.BEHAVIOR_CASES)
    native = {result.case_id: result.observed_value for result in report.case_results}
    if key == REFERENCE:
        assert native["seed_state"] == "native"
        assert native["cancellation_deadlines"] == "native"
        assert native["lease_fencing_restart_replay"] == "native"
        assert native["activation_effect_ledgers"] == "native"
        assert native["agent_protocol_negotiation"] == "native"
    else:
        assert native["seed_state"] == "recorded_only"
        assert native["agent_protocol_negotiation"] == "compatibility_adapter"
        assert native["activation_effect_ledgers"] == "compatibility_adapter"
    ledger = release_gates.GateLedger()
    ledger.record_conformance(report)
    assert ledger.gate_passed("conformance", key)


@pytest.mark.asyncio
async def test_a_reference_runtime_that_ignores_the_seed_fails_the_native_column(conformance_db, monkeypatch):
    from core.variants import reference

    monkeypatch.setattr(reference, "initial_digest", lambda user_task, seed: reference.digest_hex(reference.DIGEST_DOMAIN, {"user_task": user_task}))
    record = cap.CapabilityDirectory().get(REFERENCE)
    env = await behavior.prepare_environment(record, behavior.ReferenceExecutor(REFERENCE), conformance_db)
    report = await behavior.run_behavioral_suite(env)
    seed_case = next(result for result in report.case_results if result.case_id == "seed_state")
    assert not seed_case.passed
    assert seed_case.observed_value == "recorded_only"
    assert not report.passed
    ledger = release_gates.GateLedger()
    ledger.record_conformance(report)
    assert not ledger.gate_passed("conformance", REFERENCE)


@dataclasses.dataclass
class NativeWritingLegacyExecutor(behavior.LegacyTraceExecutor):
    """A legacy executor that wrongly authors one native authority record."""

    async def execute(self, **arguments):
        result = await super().execute(**arguments)
        import runtime_journal as journal

        await journal.commit_operation(journal.JournalOperation(
            operation_type="admission_identity", task_id=result.task_id,
            run_id=f"run-native-{result.task_id}", runtime_id=self.runtime_key.runtime_id,
            runtime_contract_version=self.runtime_key.runtime_contract_version,
            payload={
                "admission_id": f"admission-{result.task_id}",
                "version_set": {"checkpoint_schema_version": "1"},
                "specification_digest": "1" * 64, "capability_document_digest": "2" * 64,
                "admission_digest": "3" * 64,
            },
            idempotency_token=f"admission-{result.task_id}",
        ))
        # A runtime-authored record: the legacy runtime must never commit one.
        await journal.commit_operation(journal.JournalOperation(
            operation_type="evidence_update", task_id=result.task_id,
            run_id=f"run-native-{result.task_id}", runtime_id=self.runtime_key.runtime_id,
            runtime_contract_version=self.runtime_key.runtime_contract_version,
            payload={"claim_id": "claim-native", "evidence_state": "verified"},
            idempotency_token=f"evidence-{result.task_id}",
        ))
        return dataclasses.replace(result, native_rows=await behavior.native_authority_rows(result.task_id))


@pytest.mark.asyncio
async def test_a_legacy_execution_that_writes_a_native_row_fails_the_ledger_case(conformance_db):
    key = RuntimeKey("classic", "1")
    record = cap.CapabilityDirectory().get(key)
    env = await behavior.prepare_environment(record, NativeWritingLegacyExecutor(key), conformance_db)
    report = await behavior.run_behavioral_suite(env)
    ledger_case = next(result for result in report.case_results if result.case_id == "activation_effect_ledgers")
    assert not ledger_case.passed
    assert ledger_case.observed_value == "native"
    assert ledger_case.expected_value == "compatibility_adapter"


@pytest.mark.asyncio
async def test_the_reference_runtime_resumes_and_stays_deterministic(conformance_db):
    executor = behavior.ReferenceExecutor(REFERENCE, steps=4)
    complete = await executor.execute(task_id="task-reference-full", user_task="count", seed=11)
    assert complete.phases == ["initial_state", "reference_step", "reference_step", "reference_step", "reference_step", "final_result"]
    assert complete.result["resumed_from_step"] == 0 and complete.result["steps"] == 4
    interrupted = await executor.execute(task_id="task-reference-part", user_task="count", seed=11, abort_after_steps=2)
    assert interrupted.aborted and interrupted.checkpoint["completed_steps"] == 2
    resumed = await executor.execute(task_id="task-reference-part", user_task="count", seed=11, resume=True)
    assert resumed.result["resumed_from_step"] == 2
    assert resumed.result["digest"] == complete.result["digest"]
    assert resumed.answer == complete.answer
    assert resumed.phases == ["reference_step", "reference_step", "final_result"]
    other = await executor.execute(task_id="task-reference-other", user_task="count", seed=12)
    assert other.answer != complete.answer
    assert complete.native_rows == 0


def test_the_reference_runtime_is_admissible_but_unlisted():
    assert variants.require_admissible_runtime(REFERENCE) is ReferenceVariantRuntime
    assert variants.require_checkpoint_reader(REFERENCE) is ReferenceVariantRuntime
    assert REFERENCE in variants.registered_runtime_keys()
    listed = {entry["id"] for entry in variants.variant_capabilities()["variants"]}
    assert "reference" not in listed and "classic" in listed
    assert variants.resolve_runtime_key("reference") == REFERENCE


def test_the_reference_configuration_fails_closed():
    assert build_configuration(None) == {"schema_version": "1", "steps": 3, "seed": 0, "answer": None}
    assert build_configuration({"schema_version": "1", "steps": 2, "seed": 9})["steps"] == 2
    for bad in ({"steps": 0}, {"steps": True}, {"seed": "7"}, {"answer": 3}, {"unknown": 1}, {"steps": 65}):
        with pytest.raises(VariantConfigurationError):
            build_configuration(bad)
    assert ReferenceVariantRuntime.configuration_from_metadata({"effective_configuration": {"schema_version": "1", "steps": 5}})["steps"] == 5
    assert ReferenceVariantRuntime.configuration_from_metadata({"effective_configuration": {"schema_version": "0"}}) is None
