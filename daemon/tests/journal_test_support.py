"""Shared builders for the Foundation runtime-journal test suites.

Each builder returns one valid typed operation. ``seed_full_run``
commits one representative record per typed operation, so atomicity
and replay tests cover the complete operation set.
"""
from __future__ import annotations

from typing import Any

import runtime_journal as journal
from core.digest_profile import digest_hex

TASK_ID = "task-journal"
RUN_ID = "run-journal"
CLASSIC_PAIR = {"runtime_id": "classic", "runtime_contract_version": "1"}


def base_arguments(run_id: str = RUN_ID, task_id: str = TASK_ID) -> dict:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "runtime_id": "classic",
        "runtime_contract_version": "1",
    }


def admission_operation(
    run_id: str = RUN_ID, task_id: str = TASK_ID, **overrides: Any,
) -> journal.JournalOperation:
    payload = {
        "admission_id": f"admission-{run_id}",
        "version_set": {"checkpoint_schema_version": "1"},
        "specification_digest": "1" * 64,
        "capability_document_digest": "2" * 64,
        "admission_digest": "3" * 64,
    }
    arguments = dict(
        operation_type="admission_identity",
        payload=payload,
        idempotency_token=f"admission-{run_id}",
        **base_arguments(run_id, task_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def activation_operation(
    run_id: str = RUN_ID, *, state: str = "running", **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="activation_transition",
        payload={"activation_id": "activation-a", "activation_state": state},
        idempotency_token=f"activation-{run_id}-{state}",
        activation_dispatch_id="activation-a" if state == "granted" else None,
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def effect_operation(
    run_id: str = RUN_ID, **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="effect_transition",
        payload={"effect_id": "effect-a", "effect_state": "approved"},
        idempotency_token=f"effect-{run_id}",
        effect_dispatch_id="effect-a",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def proposal_operation(
    run_id: str = RUN_ID,
    *,
    decision: str = "accepted",
    **overrides: Any,
) -> journal.JournalOperation:
    payload = {
        "decision": decision,
        "proposal_digest": "4" * 64,
        "execution_envelope_digest": "5" * 64,
        "projection_changes": {"board_summary": f"{decision} proposal"},
        "checkpoint_digest": "6" * 64,
        "circuit_state": "closed",
        "circuit_decision": "allow",
        "activation_id": "activation-a",
        "activation_state": "completed",
        "effect_id": "effect-a",
        "effect_state": "completed",
        "budget": {"reserved": 2000, "consumed": 1500},
        "trace_event": {"event": "state.committed"},
    }
    arguments = dict(
        operation_type="proposal_decision",
        payload=payload,
        idempotency_token=f"proposal-{run_id}-{decision}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def control_operation(
    run_id: str = RUN_ID, *, operation: str = "pause", **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="human_control",
        payload={
            "control_id": f"control-{operation}",
            "operation": operation,
            "actor_id": "operator-lead",
            "reason": "test control",
        },
        idempotency_token=f"control-{run_id}-{operation}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def evidence_operation(
    run_id: str = RUN_ID, **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="evidence_update",
        payload={"claim_id": "claim-a", "evidence_state": "verified"},
        idempotency_token=f"evidence-{run_id}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def goal_operation(
    run_id: str = RUN_ID, **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="goal_update",
        payload={"goal_id": "goal-a", "goal_state": "satisfied"},
        idempotency_token=f"goal-{run_id}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def budget_operation(
    run_id: str = RUN_ID, **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="budget_reconciliation",
        payload={"reservation_id": "budget-a", "consumed_usd_millionths": 500},
        idempotency_token=f"budget-{run_id}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def outcome_payload(run_id: str = RUN_ID) -> dict[str, Any]:
    body = {
        "outcome_id": f"outcome-{run_id}",
        "common_class": "success",
        "reason_code": "task_answer_accepted",
        "mapping_version": "1",
    }
    return {**body, "outcome_digest": digest_hex("journal-payload", body)}


def terminal_operation(
    run_id: str = RUN_ID, **overrides: Any,
) -> journal.JournalOperation:
    arguments = dict(
        operation_type="terminal_outcome",
        payload=outcome_payload(run_id),
        idempotency_token=f"terminal-{run_id}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def invalidation_operation(
    run_id: str = RUN_ID, **overrides: Any,
) -> journal.JournalOperation:
    outcome = outcome_payload(run_id)
    arguments = dict(
        operation_type="post_terminal_invalidation",
        payload={
            "invalidation_id": f"invalidation-{run_id}",
            "outcome_id": outcome["outcome_id"],
            "outcome_digest": outcome["outcome_digest"],
            "targets": [
                {"kind": "projection", "reference": "projection-current"},
                {"kind": "report", "reference": "report-weekly"},
            ],
            "reason_code": "source_retracted",
            "authority_id": "authority-review-board",
        },
        idempotency_token=f"invalidation-{run_id}",
        **base_arguments(run_id),
    )
    arguments.update(overrides)
    return journal.JournalOperation(**arguments)


def full_run_operations(
    run_id: str = RUN_ID,
) -> list[journal.JournalOperation]:
    """One representative operation per registered type, in order."""
    return [
        admission_operation(run_id),
        activation_operation(run_id),
        effect_operation(run_id),
        proposal_operation(run_id, decision="accepted"),
        proposal_operation(run_id, decision="rejected"),
        control_operation(run_id, operation="pause"),
        control_operation(run_id, operation="resume"),
        evidence_operation(run_id),
        goal_operation(run_id),
        budget_operation(run_id),
        terminal_operation(run_id),
        invalidation_operation(run_id),
    ]


async def seed_full_run(
    run_id: str = RUN_ID,
) -> list[journal.JournalRecord]:
    """Commit one representative record per typed operation."""
    return [
        await journal.commit_operation(operation)
        for operation in full_run_operations(run_id)
    ]
