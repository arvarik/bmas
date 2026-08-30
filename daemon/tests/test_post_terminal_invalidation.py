"""Foundation Stage 0B: post-terminal invalidation never rewrites an
immutable terminal outcome.

The invalidation service changes current validity and supersession
projections only. It deduplicates through one deterministic idempotency
key, fails closed on a reused key with another request, and denies any
model or tool execution.
"""
from __future__ import annotations

import dataclasses

import pytest

from core.run_contracts import (
    InvalidationConflictError,
    InvalidationExecutionError,
    InvalidationRejectedError,
    InvalidationRequest,
    InvalidationService,
    OutcomeClass,
    OutcomeLedger,
    ReasonBinding,
    ReasonRegistry,
    RunLedger,
    RunState,
    TargetReference,
    derive_invalidation_idempotency_key,
    replay_invalidation_projections,
)
from core.variants import RuntimeKey

CLASSIC_KEY = RuntimeKey("classic", "1")

PROJECTION_TARGET = TargetReference("projection", "projection-current-answer")
REPORT_TARGET = TargetReference("report", "report-weekly-summary")


@pytest.fixture()
def environment():
    run_ledger = RunLedger()
    registry = ReasonRegistry()
    registry.register_mapping(
        CLASSIC_KEY,
        {
            "task_answer_accepted": ReasonBinding(
                OutcomeClass.SUCCESS, retryable=False,
            ),
            "coordination_stalled": ReasonBinding(
                OutcomeClass.SUBSTANTIVE_FAILURE, retryable=True,
            ),
            "operator_cancelled": ReasonBinding(
                OutcomeClass.CANCELLATION, retryable=False,
            ),
        },
        mapping_version="1",
    )
    outcome_ledger = OutcomeLedger(run_ledger, registry)
    run = run_ledger.create_run(
        task_id="task-invalidate", tenant_id="tenant-a", runtime_key=CLASSIC_KEY,
    )
    run_ledger.transition(run.run_id, RunState.QUEUED)
    run_ledger.transition(run.run_id, RunState.RUNNING)
    outcome = outcome_ledger.record_outcome(
        run.run_id,
        "task_answer_accepted",
        final_references=("artifact-final",),
    )
    service = InvalidationService(
        run_ledger,
        outcome_ledger,
        authorized_authority_ids=frozenset({"authority-review-board"}),
        policy_version="1",
        known_targets=frozenset(
            {
                (PROJECTION_TARGET.kind, PROJECTION_TARGET.reference),
                (REPORT_TARGET.kind, REPORT_TARGET.reference),
            }
        ),
    )
    return run_ledger, outcome_ledger, run, outcome, service


def build_request(run, outcome, **overrides) -> InvalidationRequest:
    arguments = dict(
        tenant_id="tenant-a",
        task_id="task-invalidate",
        run_id=run.run_id,
        outcome_id=outcome.outcome_id,
        outcome_digest=outcome.digest(),
        source_type="retraction-notice",
        source_id="source-original-citation",
        source_digest="f" * 64,
        targets=(PROJECTION_TARGET, REPORT_TARGET),
        reason_code="source_retracted",
        authority_id="authority-review-board",
        decision_reference="decision-2026-124",
    )
    arguments.update(overrides)
    return InvalidationRequest(**arguments)


def test_one_commit_covers_record_projections_and_journal(environment):
    _, _, run, outcome, service = environment
    record = service.submit(build_request(run, outcome))
    assert len(service.journal) == 1
    state = service.projection_state()
    for target in (PROJECTION_TARGET, REPORT_TARGET):
        assert state[(target.kind, target.reference)] == {
            "current": False,
            "superseded_by": record.invalidation_id,
        }
    assert record.journal_cursor == 0
    replayed = replay_invalidation_projections(service.journal)
    assert replayed == service.projection_state()


def test_the_terminal_outcome_never_changes(environment):
    _, outcome_ledger, run, outcome, service = environment
    fields_before = dataclasses.asdict(outcome)
    digest_before = outcome.digest()
    service.submit(build_request(run, outcome))
    stored = outcome_ledger.outcome_for(run.run_id)
    assert dataclasses.asdict(stored) == fields_before
    assert stored.digest() == digest_before
    assert stored is outcome
    # Exactly one terminal outcome, and the run state stays terminal.
    assert run.state is RunState.COMPLETED
    assert len(outcome_ledger.journal) == 1


def test_an_exact_repeat_returns_the_stored_invalidation(environment):
    _, _, run, outcome, service = environment
    first = service.submit(build_request(run, outcome))
    second = service.submit(build_request(run, outcome))
    assert second is first
    assert len(service.journal) == 1


def test_a_reused_key_with_another_request_fails_closed(environment):
    _, _, run, outcome, service = environment
    service.submit(build_request(run, outcome))
    journal_before = service.journal
    changed = build_request(
        run, outcome, decision_reference="decision-2026-125",
    )
    with pytest.raises(InvalidationConflictError):
        service.submit(changed)
    assert service.journal == journal_before


def test_the_idempotency_key_is_deterministic(environment):
    _, _, run, outcome, _ = environment
    key = derive_invalidation_idempotency_key(
        tenant_id="tenant-a",
        run_id=run.run_id,
        outcome_digest=outcome.digest(),
        source_digest="f" * 64,
        policy_version="1",
        targets=(REPORT_TARGET, PROJECTION_TARGET),
    )
    reordered = derive_invalidation_idempotency_key(
        tenant_id="tenant-a",
        run_id=run.run_id,
        outcome_digest=outcome.digest(),
        source_digest="f" * 64,
        policy_version="1",
        targets=(PROJECTION_TARGET, REPORT_TARGET),
    )
    assert key == reordered
    other_source = derive_invalidation_idempotency_key(
        tenant_id="tenant-a",
        run_id=run.run_id,
        outcome_digest=outcome.digest(),
        source_digest="0" * 64,
        policy_version="1",
        targets=(PROJECTION_TARGET, REPORT_TARGET),
    )
    assert other_source != key


def test_rejection_gates_fire_before_any_mutation(environment):
    run_ledger, _, run, outcome, service = environment
    active = run_ledger.create_run(
        task_id="task-invalidate", tenant_id="tenant-a", runtime_key=CLASSIC_KEY,
    )
    rejected_requests = [
        # An active run.
        build_request(active, outcome, run_id=active.run_id),
        # A foreign tenant.
        build_request(run, outcome, tenant_id="tenant-b"),
        # A wrong outcome digest.
        build_request(run, outcome, outcome_digest="0" * 64),
        # An unauthorized actor.
        build_request(run, outcome, authority_id="authority-unknown"),
        # An inaccessible target.
        build_request(
            run,
            outcome,
            targets=(TargetReference("claim", "claim-not-registered"),),
        ),
        # An unregistered reason.
        build_request(run, outcome, reason_code="because-i-said-so"),
    ]
    for request in rejected_requests:
        with pytest.raises(InvalidationRejectedError):
            service.submit(request)
    assert service.journal == []
    assert service.projection_state() == {}


def test_a_failed_gate_leaves_the_old_complete_state(environment):
    _, _, run, outcome, service = environment
    committed = service.submit(build_request(run, outcome))
    state_before = service.projection_state()
    journal_before = service.journal
    with pytest.raises(InvalidationRejectedError):
        service.submit(
            build_request(run, outcome, outcome_digest="0" * 64),
        )
    assert service.projection_state() == state_before
    assert service.journal == journal_before
    assert service.projection_state()[
        (PROJECTION_TARGET.kind, PROJECTION_TARGET.reference)
    ]["superseded_by"] == committed.invalidation_id


def test_model_or_tool_execution_is_denied(environment):
    _, _, run, outcome, service = environment
    with pytest.raises(InvalidationExecutionError, match="successor run"):
        service.submit(
            build_request(
                run,
                outcome,
                requested_operations=("invalidate", "execute_model"),
            )
        )
    with pytest.raises(InvalidationExecutionError):
        service.submit(
            build_request(run, outcome, requested_operations=("execute_tool",)),
        )
    assert service.journal == []


def test_a_successor_run_references_the_invalidation(environment):
    run_ledger, _, run, outcome, service = environment
    record = service.submit(build_request(run, outcome))
    successor = run_ledger.rerun(
        run.run_id, invalidation_id=record.invalidation_id,
    )
    assert successor.invalidation_id == record.invalidation_id
    assert successor.parent_run_id == run.run_id


def test_a_sensitive_reason_is_redacted_outside_protected_access(environment):
    _, _, run, outcome, service = environment
    record = service.submit(
        build_request(
            run,
            outcome,
            data_classification="sensitive",
            redaction_policy="operator-only",
        )
    )
    assert record.reason_code == "redacted"
    assert record.source_id == "redacted"
    assert service.journal[0]["reason_code"] == "redacted"
    assert service.journal[0]["source_id"] == "redacted"
    detail = service.read_protected_detail(
        record.invalidation_id, "authority-review-board",
    )
    assert detail == {
        "reason_code": "source_retracted",
        "source_id": "source-original-citation",
    }
    with pytest.raises(InvalidationRejectedError):
        service.read_protected_detail(
            record.invalidation_id, "authority-unknown",
        )
