"""Foundation Stage 0B: task and run identity contracts.

A task is the durable user objective. A run is one admitted execution
under one immutable runtime pair. A terminal run never reopens and
never changes its pair.
"""
from __future__ import annotations

import pytest

from core.run_contracts import (
    TERMINAL_RUN_STATES,
    InvalidRunTransitionError,
    OutcomeLedger,
    ReasonRegistry,
    RunLedger,
    RunLineageReason,
    RunState,
    TerminalRunError,
    validate_run_transition,
)
from core.variants import RuntimeKey

CLASSIC_KEY = RuntimeKey("classic", "1")
PATCHBOARD_KEY = RuntimeKey("patchboard", "1")


@pytest.fixture()
def ledger() -> RunLedger:
    return RunLedger()


def start_run(ledger: RunLedger, key: RuntimeKey = CLASSIC_KEY):
    run = ledger.create_run(
        task_id="task-identity", tenant_id="tenant-a", runtime_key=key,
    )
    ledger.transition(run.run_id, RunState.QUEUED)
    ledger.transition(run.run_id, RunState.RUNNING)
    return run


def finish_run(ledger: RunLedger, run, reason: str = "task_answer_accepted"):
    registry = ReasonRegistry()
    registry.register_mapping(
        run.runtime_key,
        {
            "task_answer_accepted": _success_binding(),
            "coordination_stalled": _substantive_binding(),
            "operator_cancelled": _cancel_binding(),
        },
        mapping_version="1",
    )
    outcomes = OutcomeLedger(ledger, registry)
    return outcomes.record_outcome(run.run_id, reason)


def _success_binding():
    from core.run_contracts import OutcomeClass, ReasonBinding

    return ReasonBinding(OutcomeClass.SUCCESS, retryable=False)


def _substantive_binding():
    from core.run_contracts import OutcomeClass, ReasonBinding

    return ReasonBinding(OutcomeClass.SUBSTANTIVE_FAILURE, retryable=True)


def _cancel_binding():
    from core.run_contracts import OutcomeClass, ReasonBinding

    return ReasonBinding(OutcomeClass.CANCELLATION, retryable=False)


def test_run_state_registry_is_complete():
    assert {state.value for state in RunState} == {
        "admitted",
        "queued",
        "running",
        "paused",
        "needs_attention",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
    }
    assert {state.value for state in TERMINAL_RUN_STATES} == {
        "completed",
        "failed",
        "cancelled",
    }


def test_terminal_entry_requires_the_outcome_ledger():
    with pytest.raises(InvalidRunTransitionError):
        validate_run_transition(RunState.RUNNING, RunState.COMPLETED)
    with pytest.raises(InvalidRunTransitionError):
        validate_run_transition(RunState.CANCELLING, RunState.CANCELLED)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_RUN_STATES))
def test_terminal_runs_never_reopen(ledger, terminal):
    run = start_run(ledger)
    run.state = terminal
    for target in RunState:
        with pytest.raises(TerminalRunError):
            ledger.transition(run.run_id, target)
    with pytest.raises(TerminalRunError):
        ledger.restart(run.run_id)
    with pytest.raises(TerminalRunError):
        ledger.retry_activation(run.run_id)
    with pytest.raises(TerminalRunError):
        ledger.reroute(run.run_id, runtime_key=PATCHBOARD_KEY)


def test_one_task_owns_several_immutable_runs(ledger):
    first = ledger.create_run(
        task_id="task-many", tenant_id="tenant-a", runtime_key=CLASSIC_KEY,
    )
    second = ledger.create_run(
        task_id="task-many", tenant_id="tenant-a", runtime_key=PATCHBOARD_KEY,
    )
    assert first.run_id != second.run_id
    runs = ledger.runs_for_task("task-many")
    assert {run.run_id for run in runs} == {first.run_id, second.run_id}
    assert first.runtime_key == CLASSIC_KEY
    assert second.runtime_key == PATCHBOARD_KEY


def test_restart_resumes_the_same_run(ledger):
    run = start_run(ledger)
    ledger.transition(run.run_id, RunState.PAUSED)
    restarted = ledger.restart(run.run_id)
    assert restarted.run_id == run.run_id
    assert restarted.runtime_key == CLASSIC_KEY
    assert restarted.state is RunState.RUNNING
    assert restarted.attempt == run.attempt


def test_activation_retry_changes_the_attempt_only(ledger):
    run = start_run(ledger)
    before = run.run_id
    retried = ledger.retry_activation(run.run_id)
    assert retried.run_id == before
    assert retried.attempt == 1
    assert retried.state is RunState.RUNNING


def test_rerun_creates_a_successor_with_lineage(ledger):
    run = start_run(ledger)
    finish_run(ledger, run)
    successor = ledger.rerun(run.run_id)
    assert successor.run_id != run.run_id
    assert successor.task_id == run.task_id
    assert successor.parent_run_id == run.run_id
    assert successor.lineage_reason is RunLineageReason.RERUN
    assert successor.state is RunState.ADMITTED


def test_rerun_requires_a_terminal_predecessor(ledger):
    run = start_run(ledger)
    with pytest.raises(Exception, match="terminal predecessor"):
        ledger.rerun(run.run_id)


def test_reroute_creates_a_successor_and_keeps_the_old_pair(ledger):
    run = start_run(ledger)
    successor = ledger.reroute(run.run_id, runtime_key=PATCHBOARD_KEY)
    assert successor.rerouted_from_run_id == run.run_id
    assert successor.lineage_reason is RunLineageReason.REROUTE
    assert successor.runtime_key == PATCHBOARD_KEY
    # The predecessor keeps its immutable pair and winds down.
    assert run.runtime_key == CLASSIC_KEY
    assert run.state is RunState.CANCELLING


def test_benchmark_repetition_fills_one_planned_slot(ledger):
    parent = start_run(ledger)
    repetition = ledger.benchmark_repetition(
        task_id="task-identity",
        tenant_id="tenant-a",
        runtime_key=CLASSIC_KEY,
        parent_run_id=parent.run_id,
        repetition_slot=3,
    )
    assert repetition.repetition_slot == 3
    assert repetition.lineage_reason is RunLineageReason.BENCHMARK_REPETITION
    with pytest.raises(Exception, match="slot"):
        ledger.benchmark_repetition(
            task_id="task-identity",
            tenant_id="tenant-a",
            runtime_key=CLASSIC_KEY,
            parent_run_id=parent.run_id,
            repetition_slot=0,
        )


def test_a_successor_names_its_parent_and_reason(ledger):
    with pytest.raises(Exception, match="lineage"):
        ledger.create_run(
            task_id="task-identity",
            tenant_id="tenant-a",
            runtime_key=CLASSIC_KEY,
            parent_run_id="run-unknown",
        )
