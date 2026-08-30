"""Foundation Stage 0B: one host-created terminal outcome per run.

The host maps every registered terminal reason through the reason
registry. A model cannot choose any trusted terminal field, and a run
holds exactly one immutable terminal outcome.
"""
from __future__ import annotations

import pytest

from core.run_contracts import (
    SHARED_PRE_EXECUTION_REASONS,
    OutcomeClass,
    OutcomeConflictError,
    OutcomeLedger,
    ReasonBinding,
    ReasonRegistry,
    RunContractError,
    RunLedger,
    RunState,
    UnregisteredReasonError,
)
from core.variants import RuntimeKey

RUNTIME_KEYS = (
    RuntimeKey("classic", "1"),
    RuntimeKey("patchboard", "1"),
    RuntimeKey("stigmergic", "1"),
)

RUNTIME_REASONS = {
    "task_answer_accepted": ReasonBinding(OutcomeClass.SUCCESS, retryable=False),
    "coordination_stalled": ReasonBinding(
        OutcomeClass.SUBSTANTIVE_FAILURE, retryable=True,
    ),
    "budget_exhausted": ReasonBinding(
        OutcomeClass.SUBSTANTIVE_FAILURE, retryable=False,
    ),
    "agent_endpoint_unreachable": ReasonBinding(
        OutcomeClass.INFRASTRUCTURE_FAILURE, retryable=True,
    ),
    "operator_cancelled": ReasonBinding(
        OutcomeClass.CANCELLATION, retryable=False,
    ),
}

EXPECTED_TERMINAL_STATE = {
    OutcomeClass.SUCCESS: RunState.COMPLETED,
    OutcomeClass.SUBSTANTIVE_FAILURE: RunState.FAILED,
    OutcomeClass.INFRASTRUCTURE_FAILURE: RunState.FAILED,
    OutcomeClass.CANCELLATION: RunState.CANCELLED,
}


@pytest.fixture()
def registry() -> ReasonRegistry:
    registry = ReasonRegistry()
    for key in RUNTIME_KEYS:
        registry.register_mapping(
            key, dict(RUNTIME_REASONS), mapping_version="1",
        )
    return registry


@pytest.fixture()
def ledger() -> RunLedger:
    return RunLedger()


def running_run(ledger: RunLedger, key: RuntimeKey):
    run = ledger.create_run(
        task_id="task-outcome", tenant_id="tenant-a", runtime_key=key,
    )
    ledger.transition(run.run_id, RunState.QUEUED)
    ledger.transition(run.run_id, RunState.RUNNING)
    return run


def test_every_registered_reason_maps_to_one_host_outcome(registry, ledger):
    outcomes = OutcomeLedger(ledger, registry)
    for key in RUNTIME_KEYS:
        mapping = registry.mapping_for(key)
        for reason_code, binding in sorted(mapping.items()):
            run = running_run(ledger, key)
            journal_before = len(outcomes.journal)
            outcome = outcomes.record_outcome(run.run_id, reason_code)
            assert outcome.common_class is binding.common_class
            assert outcome.reason_code == reason_code
            assert outcome.mapping_version == "1"
            assert outcome.runtime_key == key
            assert run.state is EXPECTED_TERMINAL_STATE[binding.common_class]
            # One terminal journal transaction per run.
            assert len(outcomes.journal) == journal_before + 1
            assert outcomes.journal[-1]["run_id"] == run.run_id
            assert outcomes.journal[-1]["cursor"] == outcome.terminal_journal_cursor


def test_every_mapping_registers_the_shared_pre_execution_reasons(registry):
    for key in RUNTIME_KEYS:
        mapping = registry.mapping_for(key)
        for shared, binding in SHARED_PRE_EXECUTION_REASONS.items():
            assert mapping[shared] == binding


def test_a_shared_reason_cannot_be_redefined():
    registry = ReasonRegistry()
    redefined = dict(RUNTIME_REASONS)
    redefined["admission_failure"] = ReasonBinding(
        OutcomeClass.SUCCESS, retryable=False,
    )
    with pytest.raises(RunContractError, match="cannot be redefined"):
        registry.register_mapping(
            RUNTIME_KEYS[0], redefined, mapping_version="1",
        )


def test_an_incomplete_mapping_is_rejected():
    registry = ReasonRegistry()
    incomplete = {
        "task_answer_accepted": ReasonBinding(
            OutcomeClass.SUCCESS, retryable=False,
        ),
    }
    with pytest.raises(RunContractError, match="every common class"):
        registry.register_mapping(
            RUNTIME_KEYS[0], incomplete, mapping_version="1",
        )


def test_a_versioned_reason_code_is_rejected():
    registry = ReasonRegistry()
    versioned = dict(RUNTIME_REASONS)
    versioned["stall-v2"] = ReasonBinding(
        OutcomeClass.SUBSTANTIVE_FAILURE, retryable=True,
    )
    with pytest.raises(RunContractError, match="generation-neutral"):
        registry.register_mapping(
            RUNTIME_KEYS[0], versioned, mapping_version="1",
        )


def test_a_second_terminal_outcome_is_rejected(registry, ledger):
    outcomes = OutcomeLedger(ledger, registry)
    run = running_run(ledger, RUNTIME_KEYS[0])
    first = outcomes.record_outcome(run.run_id, "task_answer_accepted")
    journal_before = list(outcomes.journal)
    with pytest.raises(OutcomeConflictError):
        outcomes.record_outcome(run.run_id, "coordination_stalled")
    # The rejection writes no new journal record and keeps the outcome.
    assert outcomes.journal == journal_before
    assert outcomes.outcome_for(run.run_id) == first
    assert run.state is RunState.COMPLETED


def test_the_host_ignores_model_claimed_terminal_fields(registry, ledger):
    outcomes = OutcomeLedger(ledger, registry)
    run = running_run(ledger, RUNTIME_KEYS[0])
    model_payload = {
        "status": "completed",
        "terminal_reason": "task_answer_accepted",
        "common_class": "success",
        "note": "The model claims success in free text.",
    }
    # The host observed an infrastructure failure. The model payload has
    # no channel into the outcome ledger, so its claims change nothing.
    outcome = outcomes.record_outcome(run.run_id, "agent_endpoint_unreachable")
    assert outcome.reason_code == "agent_endpoint_unreachable"
    assert outcome.common_class is OutcomeClass.INFRASTRUCTURE_FAILURE
    assert run.state is RunState.FAILED
    assert model_payload["terminal_reason"] not in (outcome.reason_code,)


def test_a_model_injected_reason_code_is_rejected(registry, ledger):
    outcomes = OutcomeLedger(ledger, registry)
    run = running_run(ledger, RUNTIME_KEYS[0])
    with pytest.raises(UnregisteredReasonError):
        outcomes.record_outcome(run.run_id, "i-declare-success")
    assert run.state is RunState.RUNNING


def test_an_unmapped_runtime_pair_records_no_outcome(ledger):
    outcomes = OutcomeLedger(ledger, ReasonRegistry())
    run = running_run(ledger, RUNTIME_KEYS[0])
    with pytest.raises(UnregisteredReasonError):
        outcomes.record_outcome(run.run_id, "task_answer_accepted")


def test_the_outcome_digest_covers_every_field(registry, ledger):
    outcomes = OutcomeLedger(ledger, registry)
    run = running_run(ledger, RUNTIME_KEYS[0])
    outcome = outcomes.record_outcome(
        run.run_id,
        "task_answer_accepted",
        final_references=("artifact-a",),
        resource_references=("budget-a",),
    )
    digest = outcome.digest()
    assert len(digest) == 64
    import dataclasses

    varied = dataclasses.replace(outcome, final_references=("artifact-b",))
    assert varied.digest() != digest
