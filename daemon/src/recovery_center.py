"""Foundation Recovery Center: operator queues, actions, and alerts.

The Recovery Center lists every declared unhealthy durable state:
unknown effects, unknown deliveries, dead letters, stale leases,
outbox lag, WAL pressure, artifact health, clock faults, backup
health, and expired qualifications. Each item shows its tenant, task,
run, age, fence, evidence, and allowed actions, after the redaction
and access rules run.

Every action authenticates, creates one journaled control decision,
and never updates a ledger directly. An unsafe irreversible retry
still needs its separated approval; the interface records the request
and the approval as distinct journal decisions.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import database as db
import effect_service as effects
import runtime_journal as journal
from access_control import ObjectRef, Principal, check_access
from activation_service import run_identity

if TYPE_CHECKING:
    from core.asset_store import ArtifactStore

RECOVERY_QUEUES = (
    "unknown_effects",
    "delivery_unknown_dispatches",
    "dead_letters",
    "stale_leases",
    "outbox_lag",
    "wal_pressure",
    "artifact_health",
    "clock_faults",
    "backup_health",
    "expired_qualifications",
)

RECOVERY_ACTIONS = (
    "reconcile_by_lookup",
    "retry_safe_effect",
    "request_unsafe_retry",
    "approve_unsafe_retry",
    "cancel_activation",
    "dead_letter_activation",
    "reclaim_stale_lease",
    "replay_outbox_record",
    "run_wal_checkpoint",
    "pause_new_work",
    "repair_artifact_reference",
    "restore_artifact",
    "erase_artifact",
)

DEFAULT_THRESHOLDS = {
    "outbox_lag_seconds": 300,
    "wal_bytes": 64_000_000,
    "checkpoint_age_seconds": 3600,
    "queue_count_alert": 1,
}

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class RecoveryCenterError(ValueError):
    """One Recovery Center rule failed closed."""


def _age_seconds(now: str, then: str | None) -> int:
    if not then:
        return 0
    try:
        delta = (
            datetime.strptime(now, _TIME_FORMAT)
            - datetime.strptime(str(then), _TIME_FORMAT)
        )
    except ValueError:
        return 0
    return max(int(delta.total_seconds()), 0)


def _item(
    *,
    queue: str,
    item_id: str,
    tenant_id: str,
    now: str,
    task_id: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
    fence: str | None = None,
    evidence: dict[str, Any] | None = None,
    allowed_actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one redacted queue item.

    The item carries identifiers, states, and reasons only. Request
    bodies, prompts, secrets, and sensitive evidence never render
    here; an advanced view never bypasses this rule.
    """
    return {
        "queue": queue,
        "item_id": item_id,
        "tenant_id": tenant_id,
        "task_id": task_id,
        "run_id": run_id,
        "age_seconds": _age_seconds(now, since),
        "fence": fence,
        "evidence": dict(evidence or {}),
        "allowed_actions": list(allowed_actions),
    }


async def list_queue(
    queue: str,
    *,
    principal: Principal,
    thresholds: dict[str, int] | None = None,
    artifact_store: ArtifactStore | None = None,
    database_time: str | None = None,
) -> list[dict[str, Any]]:
    """List one unhealthy-work queue for one authorized principal.

    The listing shows only items inside the principal's tenant, and
    the read authorization uses the same object checks as every other
    API.
    """
    if queue not in RECOVERY_QUEUES:
        raise RecoveryCenterError(f"Unknown recovery queue: {queue!r}")
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        items = await _collect_queue(
            connection,
            queue,
            now=now,
            limits=limits,
            artifact_store=artifact_store,
        )
    authorized = []
    for item in items:
        try:
            check_access(
                principal,
                "read",
                ObjectRef(
                    kind="recovery_item",
                    tenant_id=str(item["tenant_id"]),
                    object_id=str(item["item_id"]),
                    task_id=item.get("task_id"),
                    run_id=item.get("run_id"),
                ),
            )
        except PermissionError:
            continue
        authorized.append(item)
    return authorized


async def _collect_queue(
    connection: Any,
    queue: str,
    *,
    now: str,
    limits: dict[str, int],
    artifact_store: ArtifactStore | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if queue == "unknown_effects":
        cursor = await connection.execute(
            "SELECT * FROM effect_attempts WHERE state = 'outcome_unknown'",
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=str(row["effect_id"]),
                tenant_id="tenant-default",
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                since=str(row["state_changed_at"]),
                now=now,
                evidence={
                    "effect_operation_id": str(row["effect_operation_id"]),
                    "retry_safety": row["retry_safety"],
                },
                allowed_actions=(
                    "reconcile_by_lookup",
                    "retry_safe_effect",
                    "request_unsafe_retry",
                ),
            ))
    elif queue == "delivery_unknown_dispatches":
        cursor = await connection.execute(
            "SELECT * FROM activation_dispatch_outbox "
            "WHERE dispatch_state = 'delivery_unknown'",
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=str(row["grant_id"]),
                tenant_id="tenant-default",
                run_id=str(row["run_id"]),
                since=row["claimed_at"],
                now=now,
                fence=row["claim_fence"],
                evidence={
                    "activation_id": str(row["activation_id"]),
                    "delivery_count": int(row["delivery_count"]),
                },
                allowed_actions=(
                    "cancel_activation", "dead_letter_activation",
                ),
            ))
    elif queue == "dead_letters":
        cursor = await connection.execute(
            "SELECT * FROM activation_dispatch_outbox "
            "WHERE dispatch_state = 'dead_letter'",
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=str(row["grant_id"]),
                tenant_id="tenant-default",
                run_id=str(row["run_id"]),
                since=row["last_attempt_at"],
                now=now,
                evidence={"terminal_reason": row["terminal_reason"]},
                allowed_actions=("replay_outbox_record",),
            ))
        cursor = await connection.execute(
            "SELECT * FROM activations WHERE state = 'abandoned' "
            "AND terminal_reason IS NOT NULL",
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=f"{row['activation_id']}#{row['attempt']}",
                tenant_id=str(row["tenant_id"]),
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                since=str(row["state_changed_at"]),
                now=now,
                fence=row["task_fence"],
                evidence={"terminal_reason": row["terminal_reason"]},
                allowed_actions=(),
            ))
    elif queue == "stale_leases":
        cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE lease_owner IS NOT NULL "
            "AND (lease_expired = 1 OR lease_expires_at <= ?)",
            (now,),
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=f"run-lease-{row['run_id']}",
                tenant_id="tenant-default",
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                since=row["lease_expires_at"],
                now=now,
                fence=row["lease_fence"],
                evidence={"kind": "task_lease"},
                allowed_actions=("reclaim_stale_lease", "pause_new_work"),
            ))
        cursor = await connection.execute(
            "SELECT * FROM activation_leases WHERE released = 0 "
            "AND expires_at <= ?",
            (now,),
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=str(row["lease_id"]),
                tenant_id="tenant-default",
                run_id=str(row["run_id"]),
                since=str(row["expires_at"]),
                now=now,
                fence=str(row["lease_fence"]),
                evidence={
                    "kind": "activation_lease",
                    "activation_id": str(row["activation_id"]),
                    "activation_attempt": int(row["attempt"]),
                },
                allowed_actions=("reclaim_stale_lease",),
            ))
    elif queue == "outbox_lag":
        for table, id_column in (
            ("journal_outbox", "outbox_id"),
            ("activation_dispatch_outbox", "dispatch_id"),
            ("effect_dispatch_outbox", "dispatch_id"),
        ):
            time_column = (
                "created_at" if table == "journal_outbox" else "not_before"
            )
            state_filter = (
                "" if table == "journal_outbox"
                else " AND dispatch_state = 'queued'"
            )
            cursor = await connection.execute(
                f"SELECT * FROM {table} WHERE 1=1{state_filter}",
            )
            for row in await cursor.fetchall():
                age = _age_seconds(now, row[time_column])
                if age < int(limits["outbox_lag_seconds"]):
                    continue
                columns = set(row.keys())
                items.append(_item(
                    queue=queue,
                    item_id=f"{table}-{row[id_column]}",
                    tenant_id="tenant-default",
                    run_id=str(row["run_id"])
                    if "run_id" in columns else None,
                    since=row[time_column],
                    now=now,
                    evidence={"table": table},
                    allowed_actions=("replay_outbox_record",),
                ))
    elif queue == "wal_pressure":
        wal_path = f"{db.DB_PATH}-wal"
        wal_bytes = (
            os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        )
        if wal_bytes >= int(limits["wal_bytes"]):
            items.append(_item(
                queue=queue,
                item_id="wal-size",
                tenant_id="tenant-default",
                now=now,
                evidence={"wal_bytes": wal_bytes},
                allowed_actions=("run_wal_checkpoint", "pause_new_work"),
            ))
    elif queue == "artifact_health":
        if artifact_store is not None:
            report = artifact_store.health_report()
            for kind in ("missing", "corrupt", "quarantined", "orphans"):
                for digest in report[kind]:
                    items.append(_item(
                        queue=queue,
                        item_id=f"{kind}-{digest}",
                        tenant_id="tenant-default",
                        now=now,
                        evidence={"kind": kind, "content_digest": digest},
                        allowed_actions=(
                            "repair_artifact_reference",
                            "restore_artifact",
                            "erase_artifact",
                        ),
                    ))
    elif queue == "clock_faults":
        cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE clock_fault = 1",
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=f"clock-{row['run_id']}",
                tenant_id="tenant-default",
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                now=now,
                fence=str(row["task_fence"]),
                evidence={"watermark": str(row["database_time_watermark"])},
                allowed_actions=("pause_new_work",),
            ))
    elif queue == "backup_health":
        cursor = await connection.execute(
            "SELECT * FROM backup_records WHERE state = 'failed' "
            "OR (expires_at IS NOT NULL AND expires_at <= ?)",
            (now,),
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=str(row["backup_id"]),
                tenant_id="tenant-default",
                since=str(row["created_at"]),
                now=now,
                evidence={
                    "kind": str(row["kind"]),
                    "state": str(row["state"]),
                },
                allowed_actions=(),
            ))
    elif queue == "expired_qualifications":
        cursor = await connection.execute(
            "SELECT * FROM provider_qualifications "
            "WHERE revoked = 1 OR expires_at <= ?",
            (now,),
        )
        for row in await cursor.fetchall():
            items.append(_item(
                queue=queue,
                item_id=str(row["qualification_id"]),
                tenant_id="tenant-default",
                since=str(row["expires_at"]),
                now=now,
                evidence={
                    "provider": str(row["provider"]),
                    "model": str(row["model"]),
                    "adapter": str(row["adapter"]),
                },
                allowed_actions=(),
            ))
    return items


async def list_all_queues(
    *,
    principal: Principal,
    thresholds: dict[str, int] | None = None,
    artifact_store: ArtifactStore | None = None,
    database_time: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """List every declared unhealthy-work queue."""
    return {
        queue: await list_queue(
            queue,
            principal=principal,
            thresholds=thresholds,
            artifact_store=artifact_store,
            database_time=database_time,
        )
        for queue in RECOVERY_QUEUES
    }


# ── Journaled control decisions ──────────────────────────────────────


async def _journal_control_decision(
    *,
    principal: Principal,
    run_id: str,
    operation: str,
    reason: str,
    payload_extra: dict[str, Any] | None = None,
    extra_writes: Any = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Record one authenticated operator decision through the journal.

    Every Recovery Center action passes here. The interface never
    updates a ledger directly.
    """
    identity = await run_identity(run_id)
    if principal.tenant_id != identity["tenant_id"]:
        raise RecoveryCenterError(
            "The principal cannot act across the tenant boundary"
        )
    control_id = f"recovery-{uuid.uuid4()}"
    return await journal.commit_operation(
        journal.JournalOperation(
            operation_type="human_control",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload={
                "control_id": control_id,
                "operation": operation,
                "actor_id": principal.principal_id,
                "reason": reason,
                **(payload_extra or {}),
            },
            idempotency_token=control_id,
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra_writes,
    )


def _require_operator(principal: Principal) -> None:
    if "operator" not in principal.roles:
        raise RecoveryCenterError(
            "Recovery actions require the operator role"
        )


async def reconcile_by_lookup(
    *,
    principal: Principal,
    run_id: str,
    effect_id: str,
    lookup_evidence: str,
    outcome: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Reconcile one unknown effect through an authenticated lookup."""
    _require_operator(principal)
    decision = await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_reconcile_by_lookup",
        reason=lookup_evidence,
        payload_extra={"effect_id": effect_id, "outcome": outcome},
        task_fence=task_fence,
        database_time=database_time,
    )
    await effects.observe_via_lookup(
        run_id=run_id,
        effect_id=effect_id,
        lookup_evidence=lookup_evidence,
        outcome=outcome,
        task_fence=task_fence,
        database_time=database_time,
    )
    record = await effects.reconcile_effect(
        run_id=run_id,
        effect_id=effect_id,
        usage=None,
        task_fence=task_fence,
        database_time=database_time,
    )
    return {"decision": decision, "record": record}


async def retry_safe_effect(
    *,
    principal: Principal,
    run_id: str,
    effect_id: str,
    reservation_id: str,
    adapter_capabilities: effects.AdapterCapabilities,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Retry one safe effect with a new attempt identity chain."""
    _require_operator(principal)
    if adapter_capabilities.retry_safety != "safe":
        raise RecoveryCenterError(
            "Only a safe effect retries without the separated approval"
        )
    decision = await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_retry_safe_effect",
        reason="operator_retry",
        payload_extra={"effect_id": effect_id},
        task_fence=task_fence,
        database_time=database_time,
    )
    retry = await effects.retry_effect(
        run_id=run_id,
        predecessor_effect_id=effect_id,
        reservation_id=reservation_id,
        adapter_capabilities=adapter_capabilities,
        requested_by=principal.principal_id,
        task_fence=task_fence,
        database_time=database_time,
    )
    return {"decision": decision, "retry": retry}


async def request_unsafe_retry(
    *,
    principal: Principal,
    run_id: str,
    effect_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Record one unsafe-retry request as its own journal decision."""
    _require_operator(principal)
    return await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_request_unsafe_retry",
        reason=reason,
        payload_extra={"effect_id": effect_id},
        task_fence=task_fence,
        database_time=database_time,
    )


async def approve_unsafe_retry(
    *,
    principal: Principal,
    requested_by: str,
    run_id: str,
    effect_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Approve one unsafe retry through the separated approval path."""
    if "effect_approver" not in principal.roles:
        raise RecoveryCenterError(
            "Only an effect approver approves an unsafe retry"
        )
    if principal.principal_id == requested_by:
        raise RecoveryCenterError(
            "The requester cannot approve the unsafe retry alone"
        )
    attempt = await effects.get_attempt(effect_id)
    return await effects.approve_unsafe_retry(
        run_id=run_id,
        effect_operation_id=str(attempt["effect_operation_id"]),
        retry_of_effect_id=effect_id,
        requested_by=requested_by,
        approved_by=principal.principal_id,
        reason=reason,
        task_fence=task_fence,
        database_time=database_time,
    )


async def reclaim_stale_lease(
    *,
    principal: Principal,
    run_id: str,
    activation_id: str,
    attempt: int,
    new_owner: str,
    lease_ttl_seconds: float,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Reclaim one stale activation lease with a new higher fence."""
    _require_operator(principal)
    import activation_service as activations

    decision = await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_reclaim_stale_lease",
        reason="stale_lease",
        payload_extra={
            "activation_id": activation_id,
            "activation_attempt": attempt,
        },
        task_fence=task_fence,
        database_time=database_time,
    )
    await activations.requeue_expired_lease(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        task_fence=task_fence,
        database_time=database_time,
    )
    claim = await activations.claim_activation(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        owner=new_owner,
        lease_ttl_seconds=lease_ttl_seconds,
        task_fence=task_fence,
        database_time=database_time,
    )
    return {"decision": decision, "claim": claim}


async def replay_outbox_record(
    *,
    principal: Principal,
    run_id: str,
    journal_cursor: int,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Re-enqueue one journal outbox record for delivery."""
    _require_operator(principal)

    async def extra(
        connection: Any, cursor_value: int, now: str,
    ) -> None:
        await connection.execute(
            "INSERT INTO journal_delivery (journal_cursor, delivery_state, "
            "attempts) VALUES (?, 'pending', 0) "
            "ON CONFLICT(journal_cursor) DO UPDATE SET "
            "delivery_state = 'pending', attempts = 0",
            (journal_cursor,),
        )

    return await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_replay_outbox",
        reason="outbox_replay",
        payload_extra={"replay_journal_cursor": journal_cursor},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
    )


async def run_wal_checkpoint(
    *,
    principal: Principal,
    run_id: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Run one WAL checkpoint after the journaled control decision."""
    _require_operator(principal)
    decision = await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_wal_checkpoint",
        reason="wal_pressure",
        task_fence=task_fence,
        database_time=database_time,
    )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)",
        )
        row = await cursor.fetchone()
    return {"decision": decision, "checkpoint": tuple(row or ())}


async def pause_new_work(
    *,
    principal: Principal,
    run_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Pause one run through the journaled pause control."""
    _require_operator(principal)
    return await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="pause",
        reason=reason,
        task_fence=task_fence,
        database_time=database_time,
    )


async def erase_artifact(
    *,
    principal: Principal,
    run_id: str,
    artifact_store: ArtifactStore,
    content_digest: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Erase one artifact through a journaled security decision."""
    if "security_administrator" not in principal.roles:
        raise RecoveryCenterError(
            "Only a security administrator approves privacy erasure"
        )
    decision = await _journal_control_decision(
        principal=principal,
        run_id=run_id,
        operation="recovery_erase_artifact",
        reason=reason,
        payload_extra={"content_digest": content_digest},
        task_fence=task_fence,
        database_time=database_time,
    )
    record = artifact_store.erase(
        content_digest,
        authority_id=principal.principal_id,
        reason=reason,
        erased_at=decision.recorded_at,
    )
    return {"decision": decision, "erasure": record}


def evaluate_alerts(
    queue_counts: dict[str, int],
    *,
    thresholds: dict[str, int] | None = None,
    wal_bytes: int = 0,
) -> list[dict[str, Any]]:
    """Publish alerts with links to the filtered Recovery Center view."""
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    alerts: list[dict[str, Any]] = []
    for queue, count in queue_counts.items():
        if queue not in RECOVERY_QUEUES:
            raise RecoveryCenterError(f"Unknown recovery queue: {queue!r}")
        if count >= int(limits["queue_count_alert"]):
            alerts.append({
                "queue": queue,
                "metric": "count",
                "value": count,
                "threshold": int(limits["queue_count_alert"]),
                "view": f"/recovery-center?queue={queue}",
            })
    if wal_bytes >= int(limits["wal_bytes"]):
        alerts.append({
            "queue": "wal_pressure",
            "metric": "bytes",
            "value": wal_bytes,
            "threshold": int(limits["wal_bytes"]),
            "view": "/recovery-center?queue=wal_pressure",
        })
    return alerts


async def register_backup_outcome(
    *,
    backup_id: str,
    kind: str,
    state: str,
    published_path: str | None = None,
    details: str | dict[str, Any] | None = None,
    expires_at: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Record one backup or restore-test outcome for the health queue."""
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        await connection.execute(
            "INSERT INTO backup_records ("
            "backup_id, kind, state, published_path, details, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (backup_id, kind, state, published_path,
             json.dumps(details) if details else None, now, expires_at),
        )
        await connection.commit()
    return {"backup_id": backup_id, "state": state}
