"""Foundation durable activation lifecycle and dispatch operations.

Every activation transition commits as one journal transaction with
its ledger projection through the Foundation unit of work. The pure
transition tables in ``core.activation_states`` decide which
transitions exist; this module supplies the durable evidence for each
required condition and fails closed without it.

The activation ledger row is the durable projection of one activation
attempt. Live lease values stay in the durable lease row and never
enter an immutable contract. The activation dispatch outbox row is a
mutable delivery projection, not a replay authority; the journal
rebuilds it.
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import budget_service
import database as db
import runtime_journal as journal
from agent_protocol import (
    ActivationAcknowledgement,
    parse_acknowledgement,
    sign_activation_grant,
    verify_acknowledgement_signature,
)
from core.activation_states import (
    ACTIVATION_DISPATCH_TERMINAL_STATES,
    ActivationStateRegistry,
    validate_activation_dispatch_transition,
    validate_activation_transition,
)
from core.asset_store import (
    ARTIFACT_CONTENT_DIGEST_DOMAIN,
    DataClass,
    RetentionClass,
)
from core.digest_profile import digest_bytes
from core.variants import RuntimeKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    from core.asset_store import ArtifactStore
    from core.signing import KeyRegistry

    ReservationValidator = Callable[[str], Awaitable[bool]]

DAEMON_KEY_OWNER = "daemon"
DAEMON_KEY_PURPOSE = "daemon-grant"
AGENT_KEY_PURPOSE = "agent-receipt"

# The registered acknowledgement decision reason codes.
ACKNOWLEDGEMENT_REASON_CODES = (
    "accepted",
    "capability_missing",
    "overloaded",
    "invalid_grant",
    "duplicate_grant_conflict",
    "shutting_down",
)

# The versioned recovery policies for an expired dispatch row.
DISPATCH_RECOVERY_POLICIES = ("redeliver", "dead_letter")


class ActivationServiceError(ValueError):
    """One activation service rule failed closed."""


class ActivationClaimError(ActivationServiceError):
    """The atomic activation claim lost or was invalid."""


class DispatchClaimError(ActivationServiceError):
    """The dispatch claim transaction rejected one live check."""


class AcknowledgementRejectedError(ActivationServiceError):
    """The acknowledgement failed one daemon validation."""


class ResumeRevalidationError(ActivationServiceError):
    """One changed authority failed the post-resume recheck."""

    def __init__(self, failed_authorities: list[str]) -> None:
        super().__init__(
            f"Resume revalidation failed: {failed_authorities}"
        )
        self.failed_authorities = failed_authorities


class ProposalEligibilityError(ActivationServiceError):
    """One proposal eligibility condition failed before the decision."""


def persist_protected_artifact(
    store: ArtifactStore,
    payload: bytes,
    *,
    media_type: str,
    access_policy: str,
    referenced_by: str,
    data_class: DataClass = DataClass.INTERNAL,
    retention_class: RetentionClass = RetentionClass.EVIDENCE_REQUIRED,
) -> str:
    """Persist one protected immutable artifact and commit its reference."""
    declared_digest = digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, payload)
    staged = store.stage(
        payload,
        declared_digest=declared_digest,
        declared_size=len(payload),
        media_type=media_type,
        scanner_result="clean",
        data_class=data_class,
        access_policy=access_policy,
        retention_class=retention_class,
    )
    content_digest = store.promote(staged)
    store.commit_reference(content_digest, referenced_by=referenced_by)
    return content_digest


async def _run_identity(
    connection: aiosqlite.Connection, run_id: str,
) -> dict[str, str]:
    cursor = await connection.execute(
        "SELECT task_id, runtime_id, runtime_contract_version, tenant_id "
        "FROM runs WHERE run_id = ?",
        (run_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ActivationServiceError(f"Unknown run: {run_id}")
    return {
        "task_id": str(row["task_id"]),
        "runtime_id": str(row["runtime_id"]),
        "runtime_contract_version": str(row["runtime_contract_version"]),
        "tenant_id": str(row["tenant_id"]),
    }


async def run_identity(run_id: str) -> dict[str, str]:
    """Read the run identity of one admitted run."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _run_identity(connection, run_id)


async def _load_activation(
    connection: aiosqlite.Connection, activation_id: str, attempt: int,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM activations WHERE activation_id = ? AND attempt = ?",
        (activation_id, attempt),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ActivationServiceError(
            f"Unknown activation attempt: {activation_id}#{attempt}"
        )
    return dict(row)


async def get_activation(activation_id: str, attempt: int) -> dict[str, Any]:
    """Read one activation ledger row."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_activation(connection, activation_id, attempt)


async def _load_dispatch_row(
    connection: aiosqlite.Connection, grant_id: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM activation_dispatch_outbox WHERE grant_id = ?",
        (grant_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ActivationServiceError(f"Unknown dispatch row: {grant_id}")
    return dict(row)


async def get_dispatch_row(grant_id: str) -> dict[str, Any]:
    """Read one activation dispatch outbox row."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_dispatch_row(connection, grant_id)


async def get_grant_row(grant_id: str) -> dict[str, Any]:
    """Read one stored activation grant row."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM activation_grants WHERE grant_id = ?", (grant_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ActivationServiceError(f"Unknown grant: {grant_id}")
        return dict(row)


async def get_lease(lease_id: str) -> dict[str, Any] | None:
    """Read one activation lease row."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM activation_leases WHERE lease_id = ?", (lease_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


def _operation(
    *,
    identity: dict[str, str],
    run_id: str,
    payload: dict[str, Any],
    idempotency_token: str,
    task_fence: str | None,
) -> journal.JournalOperation:
    return journal.JournalOperation(
        operation_type="activation_transition",
        task_id=identity["task_id"],
        run_id=run_id,
        runtime_id=identity["runtime_id"],
        runtime_contract_version=identity["runtime_contract_version"],
        payload=payload,
        idempotency_token=idempotency_token,
        task_fence=task_fence,
        tenant_id=identity["tenant_id"],
    )


# ── Activation creation and claims ───────────────────────────────────


async def create_activation(
    *,
    run_id: str,
    activation_id: str,
    attempt: int = 1,
    retry_of_attempt: int | None = None,
    reservation_id: str | None = None,
    request_digest: str | None = None,
    context_view_digest: str | None = None,
    retry_delay_ms: int | None = None,
    retry_jitter_ms: int | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Create one activation attempt in the queued state.

    An activation retry keeps the logical activation identifier and
    uses one new attempt number. The retry delay and its bounded
    jitter persist before the work returns to the queue.
    """
    if retry_of_attempt is not None:
        if retry_delay_ms is None or retry_jitter_ms is None:
            raise ActivationServiceError(
                "A retry persists its delay and bounded jitter first"
            )
        if retry_jitter_ms < 0 or retry_jitter_ms > retry_delay_ms:
            raise ActivationServiceError(
                "The retry jitter stays inside the declared delay bound"
            )

    identity = await run_identity(run_id)

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        await connection.execute(
            "INSERT INTO activations ("
            "activation_id, attempt, run_id, task_id, tenant_id, "
            "runtime_id, runtime_contract_version, retry_of_attempt, "
            "state, reservation_id, request_digest, context_view_digest, "
            "retry_delay_ms, retry_jitter_ms, task_fence, created_at, "
            "state_changed_at, journal_cursor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (
                activation_id,
                attempt,
                run_id,
                identity["task_id"],
                identity["tenant_id"],
                identity["runtime_id"],
                identity["runtime_contract_version"],
                retry_of_attempt,
                reservation_id,
                request_digest,
                context_view_digest,
                retry_delay_ms,
                retry_jitter_ms,
                task_fence,
                now,
                now,
                journal_cursor,
            ),
        )

    return await journal.commit_operation(
        _operation(
            identity=identity,
            run_id=run_id,
            payload={
                "activation_id": activation_id,
                "activation_state": "queued",
                "activation_attempt": attempt,
                "retry_of_attempt": retry_of_attempt,
            },
            idempotency_token=f"activation-create-{activation_id}-{attempt}",
            task_fence=task_fence,
        ),
        database_time=database_time,
        extra_writes=extra,
    )


async def claim_activation(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    owner: str,
    lease_ttl_seconds: float,
    purpose: str = "dispatch",
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Claim one queued activation with one atomic new lease.

    The claim serializes inside one unit-of-work transaction, so two
    workers can never both win, and the lease fence increases exactly
    once per claim.
    """
    identity = await run_identity(run_id)
    lease_id = f"activation-lease-{uuid.uuid4()}"
    claimed: dict[str, Any] = {}

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_activation(connection, activation_id, attempt)
        validate_activation_transition(str(row["state"]), "leased")
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(lease_fence), 0) AS fence "
            "FROM activation_leases WHERE activation_id = ? AND attempt = ?",
            (activation_id, attempt),
        )
        fence_row = await cursor.fetchone()
        assert fence_row is not None  # An aggregate query returns one row.
        lease_fence = int(fence_row["fence"]) + 1
        expires_at = db._shifted(now, lease_ttl_seconds)  # noqa: SLF001
        await connection.execute(
            "INSERT INTO activation_leases ("
            "lease_id, activation_id, attempt, run_id, owner, lease_fence, "
            "purpose, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lease_id,
                activation_id,
                attempt,
                run_id,
                owner,
                lease_fence,
                purpose,
                now,
                expires_at,
            ),
        )
        await connection.execute(
            "UPDATE activations SET state = 'leased', lease_id = ?, "
            "state_changed_at = ?, journal_cursor = ? "
            "WHERE activation_id = ? AND attempt = ?",
            (lease_id, now, journal_cursor, activation_id, attempt),
        )
        claimed.update(
            {
                "lease_id": lease_id,
                "lease_fence": lease_fence,
                "owner": owner,
                "expires_at": expires_at,
            },
        )

    record = await journal.commit_operation(
        _operation(
            identity=identity,
            run_id=run_id,
            payload={
                "activation_id": activation_id,
                "activation_state": "leased",
                "activation_attempt": attempt,
                "lease_purpose": purpose,
            },
            idempotency_token=f"activation-claim-{lease_id}",
            task_fence=task_fence,
        ),
        database_time=database_time,
        extra_writes=extra,
    )
    claimed["journal_cursor"] = record.journal_cursor
    return claimed


async def renew_activation_lease(
    *,
    lease_id: str,
    owner: str,
    lease_fence: int,
    run_id: str,
    task_fence: str,
    ttl_seconds: float,
    database_time: str | None = None,
) -> bool:
    """Renew one live activation lease under the exact owner and fences."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            now = await db._control_now(connection, database_time)  # noqa: SLF001
            cursor = await connection.execute(
                "SELECT task_fence FROM run_controls WHERE run_id = ?",
                (run_id,),
            )
            control = await cursor.fetchone()
            if control is None or str(control["task_fence"]) != task_fence:
                await connection.commit()
                return False
            update = await connection.execute(
                "UPDATE activation_leases SET expires_at = ? "
                "WHERE lease_id = ? AND owner = ? AND lease_fence = ? "
                "AND released = 0 AND expires_at > ?",
                (
                    db._shifted(now, ttl_seconds),  # noqa: SLF001
                    lease_id,
                    owner,
                    lease_fence,
                    now,
                ),
            )
            await connection.commit()
            return update.rowcount == 1
        except BaseException:
            await connection.rollback()
            raise


async def release_activation_lease(lease_id: str, owner: str) -> bool:
    """Release one activation lease held by its exact owner."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE activation_leases SET released = 1 "
            "WHERE lease_id = ? AND owner = ? AND released = 0",
            (lease_id, owner),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def requeue_expired_lease(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Return one leased activation to queued after lease expiry.

    The path exists only before dispatch queuing. After dispatch, no
    replacement dispatch starts before effect reconciliation; the
    lease expiry alone changes nothing.
    """
    identity = await run_identity(run_id)

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_activation(connection, activation_id, attempt)
        validate_activation_transition(str(row["state"]), "queued")
        lease_cursor = await connection.execute(
            "SELECT * FROM activation_leases WHERE lease_id = ?",
            (row["lease_id"],),
        )
        lease = await lease_cursor.fetchone()
        if lease is None:
            raise ActivationServiceError("The activation holds no lease")
        if not lease["released"] and str(lease["expires_at"]) > now:
            raise ActivationServiceError(
                "The lease is live; only an expired lease requeues"
            )
        grant_cursor = await connection.execute(
            "SELECT COUNT(*) AS grants FROM activation_grants "
            "WHERE activation_id = ? AND attempt = ?",
            (activation_id, attempt),
        )
        grants = await grant_cursor.fetchone()
        assert grants is not None  # An aggregate query returns one row.
        if int(grants["grants"]) > 0:
            raise ActivationServiceError(
                "The activation reached dispatch queuing; the expired "
                "lease cannot requeue it"
            )
        await connection.execute(
            "UPDATE activation_leases SET released = 1 WHERE lease_id = ?",
            (row["lease_id"],),
        )
        await connection.execute(
            "UPDATE activations SET state = 'queued', lease_id = NULL, "
            "state_changed_at = ?, journal_cursor = ? "
            "WHERE activation_id = ? AND attempt = ?",
            (now, journal_cursor, activation_id, attempt),
        )

    return await journal.commit_operation(
        _operation(
            identity=identity,
            run_id=run_id,
            payload={
                "activation_id": activation_id,
                "activation_state": "queued",
                "activation_attempt": attempt,
                "condition": "lease_expired_before_dispatch_queuing",
            },
            idempotency_token=(
                f"activation-requeue-{activation_id}-{attempt}-"
                f"{uuid.uuid4()}"
            ),
            task_fence=task_fence,
        ),
        database_time=database_time,
        extra_writes=extra,
    )


# ── Generic guarded transitions ──────────────────────────────────────

_WAIT_STATES = ("awaiting_gate", "awaiting_human")

_LEDGER_UPDATE_COLUMNS = (
    "proposal_digest",
    "raw_result_artifact_digest",
    "execution_envelope_digest",
    "usage",
    "wait_reason",
    "wait_policy_version",
    "required_approver",
    "reconciliation_reason",
    "terminal_reason",
    "effect_ids",
    "agent_protocol_version",
)


async def _count_unknown_effects(
    connection: aiosqlite.Connection, activation_id: str, attempt: int,
) -> int:
    cursor = await connection.execute(
        "SELECT COUNT(*) AS unknown FROM effect_attempts "
        "WHERE state = 'outcome_unknown' AND effect_operation_id IN ("
        "SELECT effect_operation_id FROM effect_operations "
        "WHERE activation_id = ? AND activation_attempt = ?)",
        (activation_id, attempt),
    )
    row = await cursor.fetchone()
    assert row is not None  # An aggregate query returns one row.
    return int(row["unknown"])


async def transition_activation(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    target_state: str,
    evidence: dict[str, Any] | None = None,
    ledger_updates: dict[str, Any] | None = None,
    registry: ActivationStateRegistry | None = None,
    task_fence: str | None = None,
    idempotency_token: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Commit one guarded activation transition through the journal.

    The pure table validates the pair. This function validates the
    durable evidence: a wait persists the proposal digest, the wait
    reason, the policy version, and the required approver before the
    transition; an abandonment is rejected while any linked effect
    stays ``outcome_unknown``.
    """
    identity = await run_identity(run_id)
    updates = dict(ledger_updates or {})
    unknown_columns = set(updates) - set(_LEDGER_UPDATE_COLUMNS)
    if unknown_columns:
        raise ActivationServiceError(
            f"Unknown ledger update columns: {sorted(unknown_columns)}"
        )

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_activation(connection, activation_id, attempt)
        current = str(row["state"])
        if registry is not None:
            registry.validate_transition(current, target_state)
        else:
            validate_activation_transition(current, target_state)
        if target_state == "abandoned":
            unknown = await _count_unknown_effects(
                connection, activation_id, attempt,
            )
            if unknown > 0:
                raise ActivationServiceError(
                    "An activation with an outcome_unknown effect stays "
                    "suspended until reconciliation proves an outcome"
                )
        if target_state in _WAIT_STATES:
            digest = updates.get("proposal_digest", row["proposal_digest"])
            required = (
                digest,
                updates.get("wait_reason"),
                updates.get("wait_policy_version"),
                updates.get("required_approver"),
            )
            if any(value is None for value in required):
                raise ActivationServiceError(
                    "A wait persists the proposal digest, wait reason, "
                    "policy version, and required approver first"
                )
        assignments = ["state = ?", "state_changed_at = ?",
                       "journal_cursor = ?"]
        values: list[Any] = [target_state, now, journal_cursor]
        for column, value in updates.items():
            assignments.append(f"{column} = ?")
            values.append(
                json.dumps(value, sort_keys=True)
                if isinstance(value, (dict, list))
                else value,
            )
        values.extend([activation_id, attempt])
        await connection.execute(
            f"UPDATE activations SET {', '.join(assignments)} "
            "WHERE activation_id = ? AND attempt = ?",
            values,
        )

    token = idempotency_token or (
        f"activation-transition-{activation_id}-{attempt}-{target_state}"
    )
    return await journal.commit_operation(
        _operation(
            identity=identity,
            run_id=run_id,
            payload={
                "activation_id": activation_id,
                "activation_state": target_state,
                "activation_attempt": attempt,
                "evidence": dict(evidence or {}),
            },
            idempotency_token=token,
            task_fence=task_fence,
        ),
        database_time=database_time,
        extra_writes=extra,
    )


async def enter_wait(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    wait_state: str,
    proposal_digest: str,
    wait_reason: str,
    wait_policy_version: str,
    required_approver: str,
    lease_id: str,
    owner: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Enter one durable wait state and release the active lease.

    The proposal digest, wait reason, policy version, and required
    approver persist in the same transaction as the transition. The
    lease releases only after that transaction commits.
    """
    if wait_state not in _WAIT_STATES:
        raise ActivationServiceError(f"Unknown wait state: {wait_state!r}")
    record = await transition_activation(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        target_state=wait_state,
        ledger_updates={
            "proposal_digest": proposal_digest,
            "wait_reason": wait_reason,
            "wait_policy_version": wait_policy_version,
            "required_approver": required_approver,
        },
        task_fence=task_fence,
        database_time=database_time,
    )
    await release_activation_lease(lease_id, owner)
    return record


async def revalidate_for_resume(
    *,
    run_id: str,
    expected_projection_version: int,
    schema_versions: dict[str, str],
    registered_schema_versions: dict[str, str],
    reservation_id: str | None = None,
    reservation_validator: ReservationValidator | None = None,
    invariant_checks: dict[str, bool] | None = None,
    database_time: str | None = None,
) -> None:
    """Recheck every authority after one resume.

    A resume never reuses a pre-wait authorization result. The recheck
    covers the state version, the schemas, the invariants, the
    cancellation state, the deadline, and the budget reservation.
    """
    failed: list[str] = []
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT projection_version FROM runs WHERE run_id = ?", (run_id,),
        )
        run_row = await cursor.fetchone()
        if run_row is None or (
            int(run_row["projection_version"]) != expected_projection_version
        ):
            failed.append("state_version")
        for name, version in schema_versions.items():
            if registered_schema_versions.get(name) != version:
                failed.append(f"schema:{name}")
        control_cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE run_id = ?", (run_id,),
        )
        control = await control_cursor.fetchone()
        if control is None:
            failed.append("run_control")
        else:
            if str(control["cancellation_state"]) != "active":
                failed.append("cancellation")
            deadline = control["deadline_at"]
            if control["deadline_expired"] or (
                deadline is not None and str(deadline) <= now
            ):
                failed.append("deadline")
            if control["clock_fault"]:
                failed.append("clock")
    if reservation_id is not None:
        validator = reservation_validator or budget_service.reservation_is_valid
        if not await validator(reservation_id):
            failed.append("budget")
    for name, holds in (invariant_checks or {}).items():
        if not holds:
            failed.append(f"invariant:{name}")
    if failed:
        raise ResumeRevalidationError(failed)


async def validation_resume(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    owner: str,
    lease_ttl_seconds: float,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Run one validation resume without any new dispatch.

    The path follows ``resume_queued``, ``leased``, and
    ``proposal_recorded``. It revalidates the persisted proposal
    digest and creates no activation or effect dispatch.
    """
    claim = await claim_activation(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        owner=owner,
        lease_ttl_seconds=lease_ttl_seconds,
        purpose="validation_resume",
        task_fence=task_fence,
        database_time=database_time,
    )
    row = await get_activation(activation_id, attempt)
    if row["proposal_digest"] is None:
        raise ActivationServiceError(
            "A validation resume requires the persisted proposal digest"
        )
    record = await transition_activation(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        target_state="proposal_recorded",
        evidence={
            "condition": "validation_resume_revalidates_proposal",
            "proposal_digest": row["proposal_digest"],
        },
        task_fence=task_fence,
        idempotency_token=(
            f"validation-resume-{activation_id}-{attempt}-"
            f"{claim['lease_id']}"
        ),
        database_time=database_time,
    )
    return {"claim": claim, "record": record}


# ── Activation dispatch: grant, outbox, claim, acknowledgement ───────


async def queue_activation_dispatch(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    agent_id: str,
    audience: str,
    agent_protocol_version: str,
    request_digest: str,
    context_view_digest: str,
    task_fence: str,
    lease_id: str,
    owner: str,
    reservation_id: str,
    daemon_private_key: Ed25519PrivateKey,
    key_id: str,
    key_registry: KeyRegistry,
    artifact_store: ArtifactStore,
    grant_ttl_seconds: float,
    reservation_validator: ReservationValidator | None = None,
    database_time: str | None = None,
    grant_id: str | None = None,
    grant_nonce: str | None = None,
) -> dict[str, Any]:
    """Create the signed activation grant and queue its dispatch row.

    The signed grant bytes persist as one protected immutable artifact
    before dispatch. One unit-of-work transaction moves the activation
    to ``dispatch_queued`` and creates the outbox row. The signing key
    validates against database time inside the creation path.
    """
    grant_id = grant_id or f"activation-grant-{uuid.uuid4()}"
    grant_nonce = grant_nonce or f"nonce-{uuid.uuid4()}"
    identity = await run_identity(run_id)

    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001

    key_registry.require_new_authority(
        key_id, owner_id=DAEMON_KEY_OWNER, purpose=DAEMON_KEY_PURPOSE, at=now,
    )

    lease = await get_lease(lease_id)
    if lease is None or lease["owner"] != owner or lease["released"]:
        raise ActivationServiceError(
            "Dispatch queuing requires the live activation lease"
        )
    if str(lease["expires_at"]) <= now:
        raise ActivationServiceError("The activation lease expired")
    validator = reservation_validator or budget_service.reservation_is_valid
    if not await validator(reservation_id):
        raise ActivationServiceError(
            "Dispatch queuing requires one valid reservation"
        )

    grant = sign_activation_grant(
        {
            "schema_version": "1",
            "activation_grant_id": grant_id,
            "task_id": identity["task_id"],
            "run_id": run_id,
            "runtime_key": RuntimeKey(
                identity["runtime_id"],
                identity["runtime_contract_version"],
            ),
            "activation_id": activation_id,
            "attempt": attempt,
            "request_digest": request_digest,
            "context_view_digest": context_view_digest,
            "task_fence": task_fence,
            "activation_fence": str(lease["lease_fence"]),
            "agent_id": agent_id,
            "agent_protocol_version": agent_protocol_version,
            "audience": audience,
            "not_before": now,
            "expires_at": db._shifted(now, grant_ttl_seconds),  # noqa: SLF001
            "grant_nonce": grant_nonce,
            "key_id": key_id,
        },
        daemon_private_key,
    )
    grant_bytes = grant.to_bytes()
    artifact_digest = persist_protected_artifact(
        artifact_store,
        grant_bytes,
        media_type="application/json",
        access_policy="foundation-grant",
        referenced_by=f"activation-dispatch-{grant_id}",
    )

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, txn_now: str,
    ) -> None:
        row = await _load_activation(connection, activation_id, attempt)
        validate_activation_transition(str(row["state"]), "dispatch_queued")
        if row["lease_id"] != lease_id:
            raise ActivationServiceError(
                "The dispatch claimant does not hold the activation lease"
            )
        await connection.execute(
            "UPDATE activations SET state = 'dispatch_queued', "
            "agent_protocol_version = ?, request_digest = ?, "
            "context_view_digest = ?, reservation_id = ?, task_fence = ?, "
            "state_changed_at = ?, journal_cursor = ? "
            "WHERE activation_id = ? AND attempt = ?",
            (
                agent_protocol_version,
                request_digest,
                context_view_digest,
                reservation_id,
                task_fence,
                txn_now,
                journal_cursor,
                activation_id,
                attempt,
            ),
        )
        await connection.execute(
            "INSERT INTO activation_grants ("
            "grant_id, activation_id, attempt, run_id, task_id, "
            "grant_artifact_digest, agent_id, audience, "
            "agent_protocol_version, request_digest, context_view_digest, "
            "task_fence, activation_fence, grant_nonce, not_before, "
            "expires_at, key_id, signature_algorithm, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (
                grant_id,
                activation_id,
                attempt,
                run_id,
                identity["task_id"],
                artifact_digest,
                agent_id,
                audience,
                agent_protocol_version,
                request_digest,
                context_view_digest,
                task_fence,
                str(lease["lease_fence"]),
                grant_nonce,
                grant.not_before,
                grant.expires_at,
                key_id,
                grant.signature_algorithm,
                txn_now,
            ),
        )
        await connection.execute(
            "INSERT INTO activation_dispatch_outbox ("
            "journal_cursor, run_id, activation_id, activation_attempt, "
            "grant_id, grant_artifact_digest, target_agent_id, audience, "
            "not_before, grant_expires_at, dispatch_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
            (
                journal_cursor,
                run_id,
                activation_id,
                attempt,
                grant_id,
                artifact_digest,
                agent_id,
                audience,
                grant.not_before,
                grant.expires_at,
            ),
        )

    record = await journal.commit_operation(
        _operation(
            identity=identity,
            run_id=run_id,
            payload={
                "activation_id": activation_id,
                "activation_state": "dispatch_queued",
                "activation_attempt": attempt,
                "dispatch_row": {
                    "grant_id": grant_id,
                    "dispatch_state": "queued",
                    "grant_artifact_digest": artifact_digest,
                },
            },
            idempotency_token=f"activation-dispatch-queue-{grant_id}",
            task_fence=task_fence,
        ),
        database_time=database_time,
        extra_writes=extra,
    )
    return {
        "grant": grant,
        "grant_bytes": grant_bytes,
        "grant_artifact_digest": artifact_digest,
        "record": record,
    }


async def _journal_dispatch_row_transition(
    *,
    run_id: str,
    grant_id: str,
    target_row_state: str,
    reason: str,
    row_updates: Callable[
        [aiosqlite.Connection, dict[str, Any], int, str], Awaitable[None],
    ],
    guard: Callable[
        [aiosqlite.Connection, dict[str, Any], str], Awaitable[None],
    ]
    | None = None,
    activation_state: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
    idempotency_token: str | None = None,
) -> journal.JournalRecord:
    """Journal one dispatch-row transition through the unit of work.

    The payload records the activation state that holds after the
    transaction, so journal replay rebuilds both the delivery
    projection and the activation state without the mutable outbox
    table.
    """
    identity = await run_identity(run_id)

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_dispatch_row(connection, grant_id)
        validate_activation_dispatch_transition(
            str(row["dispatch_state"]), target_row_state,
        )
        if guard is not None:
            await guard(connection, row, now)
        await row_updates(connection, row, journal_cursor, now)

    row_info = await get_dispatch_row(grant_id)
    if activation_state is None:
        current = await get_activation(
            str(row_info["activation_id"]),
            int(row_info["activation_attempt"]),
        )
        activation_state = str(current["state"])
    return await journal.commit_operation(
        _operation(
            identity=identity,
            run_id=run_id,
            payload={
                "activation_id": str(row_info["activation_id"]),
                "activation_state": activation_state,
                "activation_attempt": int(row_info["activation_attempt"]),
                "dispatch_row": {
                    "grant_id": grant_id,
                    "dispatch_state": target_row_state,
                    "reason": reason,
                },
            },
            idempotency_token=idempotency_token
            or f"dispatch-row-{grant_id}-{target_row_state}-{uuid.uuid4()}",
            task_fence=task_fence,
        ),
        database_time=database_time,
        extra_writes=extra,
    )


async def claim_activation_dispatch(
    *,
    grant_id: str,
    run_id: str,
    dispatcher: str,
    claim_ttl_seconds: float,
    key_registry: KeyRegistry,
    artifact_store: ArtifactStore,
    expected_target_agent_id: str,
    reservation_validator: ReservationValidator | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Claim one dispatch row through one durable claim lease.

    The claim transaction rechecks the live task and activation
    fences, the controls, the deadline, the reservation, the protocol,
    the target, the audience, the grant expiry, the artifact digest,
    and the grant-signing key status. The exact stored grant bytes
    return only after the claim commits.
    """
    validator = reservation_validator or budget_service.reservation_is_valid
    grant_row = await get_grant_row(grant_id)

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now: str,
    ) -> None:
        control_cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE run_id = ?", (row["run_id"],),
        )
        control = await control_cursor.fetchone()
        if control is None:
            raise DispatchClaimError("run_control")
        if str(control["task_fence"]) != str(grant_row["task_fence"]):
            raise DispatchClaimError("task_fence")
        if str(control["cancellation_state"]) != "active":
            raise DispatchClaimError("cancellation")
        if control["pause_state"] == "paused":
            raise DispatchClaimError("paused")
        deadline = control["deadline_at"]
        if control["deadline_expired"] or (
            deadline is not None and str(deadline) <= now
        ):
            raise DispatchClaimError("deadline")
        activation = await _load_activation(
            connection,
            str(row["activation_id"]),
            int(row["activation_attempt"]),
        )
        if str(activation["state"]) != "dispatch_queued":
            raise DispatchClaimError("activation_state")
        lease_cursor = await connection.execute(
            "SELECT * FROM activation_leases WHERE lease_id = ?",
            (activation["lease_id"],),
        )
        lease = await lease_cursor.fetchone()
        if (
            lease is None
            or lease["released"]
            or str(lease["expires_at"]) <= now
            or str(lease["lease_fence"]) != str(grant_row["activation_fence"])
        ):
            raise DispatchClaimError("activation_fence")
        if activation["reservation_id"] is None or not await validator(
            str(activation["reservation_id"]),
        ):
            raise DispatchClaimError("reservation")
        if (
            activation["agent_protocol_version"]
            != grant_row["agent_protocol_version"]
        ):
            raise DispatchClaimError("protocol")
        if row["target_agent_id"] != expected_target_agent_id:
            raise DispatchClaimError("target")
        if row["audience"] != grant_row["audience"]:
            raise DispatchClaimError("audience")
        if now >= str(row["grant_expires_at"]):
            raise DispatchClaimError("grant_expiry")
        if row["grant_artifact_digest"] != grant_row["grant_artifact_digest"]:
            raise DispatchClaimError("artifact_digest")
        if not artifact_store.has_object(str(row["grant_artifact_digest"])):
            raise DispatchClaimError("artifact_digest")
        key = key_registry.require(str(grant_row["key_id"]))
        if not key.valid_for_new_authority(now):
            raise DispatchClaimError("signing_key")

    async def updates(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        claim_fence = int(row["claim_fence"] or 0) + 1
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET dispatch_state = "
            "'claimed', claim_owner = ?, claim_fence = ?, claimed_at = ?, "
            "claim_expires_at = ?, delivery_count = delivery_count + 1, "
            "attempts = attempts + 1, last_attempt_at = ?, "
            "journal_cursor = ? WHERE grant_id = ?",
            (
                dispatcher,
                str(claim_fence),
                now,
                db._shifted(now, claim_ttl_seconds),  # noqa: SLF001
                now,
                journal_cursor,
                grant_id,
            ),
        )

    record = await _journal_dispatch_row_transition(
        run_id=run_id,
        grant_id=grant_id,
        target_row_state="claimed",
        reason="dispatcher_commits_owner_fence_and_expiry",
        row_updates=updates,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
    )
    # The exact stored grant bytes return only after the claim commits.
    stored = artifact_store.read_object(
        str(grant_row["grant_artifact_digest"]),
    )
    row = await get_dispatch_row(grant_id)
    return {
        "record": record,
        "grant_bytes": stored["payload"],
        "claim_owner": row["claim_owner"],
        "claim_fence": row["claim_fence"],
        "claim_expires_at": row["claim_expires_at"],
        "delivery_count": row["delivery_count"],
    }


async def record_send_start(
    *,
    grant_id: str,
    claim_owner: str,
    claim_fence: str,
    database_time: str | None = None,
) -> bool:
    """Persist the send-start marker before transport can write bytes."""
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE activation_dispatch_outbox SET send_started_at = ? "
            "WHERE grant_id = ? AND claim_owner = ? AND claim_fence = ? "
            "AND dispatch_state = 'claimed' AND send_started_at IS NULL",
            (now, grant_id, claim_owner, claim_fence),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def recover_expired_claim(
    *,
    grant_id: str,
    run_id: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> str:
    """Recover one claimed row after its claim lease expired.

    A crash before the send-start marker returns the row to
    ``queued``. A crash after that marker moves the row to
    ``delivery_unknown``, because recovery cannot prove whether the
    transport wrote bytes.
    """
    row = await get_dispatch_row(grant_id)
    if str(row["dispatch_state"]) != "claimed":
        raise ActivationServiceError("Only a claimed row recovers here")

    async def guard(
        connection: aiosqlite.Connection, live: dict[str, Any], now: str,
    ) -> None:
        if str(live["claim_expires_at"] or "") > now:
            raise ActivationServiceError("The claim lease is still live")

    target = (
        "delivery_unknown" if row["send_started_at"] is not None else "queued"
    )

    async def updates(
        connection: aiosqlite.Connection,
        live: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        if target == "queued":
            await connection.execute(
                "UPDATE activation_dispatch_outbox SET dispatch_state = "
                "'queued', claim_owner = NULL, claimed_at = NULL, "
                "claim_expires_at = NULL, journal_cursor = ? "
                "WHERE grant_id = ?",
                (journal_cursor, grant_id),
            )
        else:
            await connection.execute(
                "UPDATE activation_dispatch_outbox SET dispatch_state = "
                "'delivery_unknown', journal_cursor = ? WHERE grant_id = ?",
                (journal_cursor, grant_id),
            )

    await _journal_dispatch_row_transition(
        run_id=run_id,
        grant_id=grant_id,
        target_row_state=target,
        reason=(
            "claim_expired_after_send_start_marker"
            if target == "delivery_unknown"
            else "claim_expired_without_send_start_marker"
        ),
        row_updates=updates,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
    )
    return target


async def redeliver_from_delivery_unknown(
    *,
    grant_id: str,
    run_id: str,
    dispatcher: str,
    claim_ttl_seconds: float,
    key_registry: KeyRegistry,
    artifact_store: ArtifactStore,
    expected_target_agent_id: str,
    reservation_validator: ReservationValidator | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Claim one same-byte redelivery from ``delivery_unknown``.

    Recovery sends the same signed bytes with the same grant
    identifier and nonce. It never creates a new grant for the same
    activation attempt; grant delivery can repeat because the grant
    authorizes no external effect.
    """
    row = await get_dispatch_row(grant_id)
    if str(row["dispatch_state"]) != "delivery_unknown":
        raise ActivationServiceError(
            "Redelivery starts only from delivery_unknown"
        )
    return await claim_activation_dispatch(
        grant_id=grant_id,
        run_id=run_id,
        dispatcher=dispatcher,
        claim_ttl_seconds=claim_ttl_seconds,
        key_registry=key_registry,
        artifact_store=artifact_store,
        expected_target_agent_id=expected_target_agent_id,
        reservation_validator=reservation_validator,
        task_fence=task_fence,
        database_time=database_time,
    )


async def cancel_activation_dispatch(
    *,
    grant_id: str,
    run_id: str,
    reason: str,
    claimant: str | None = None,
    agent_lookup_proves_non_acceptance: bool = False,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Cancel one dispatch row under the registered conditions."""

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now: str,
    ) -> None:
        control_cursor = await connection.execute(
            "SELECT cancellation_state FROM run_controls WHERE run_id = ?",
            (row["run_id"],),
        )
        control = await control_cursor.fetchone()
        cancellation_live = control is not None and (
            str(control["cancellation_state"]) != "active"
        )
        if not cancellation_live:
            raise ActivationServiceError(
                "Dispatch cancellation requires a live cancellation"
            )
        state = str(row["dispatch_state"])
        if state == "queued":
            if row["claim_owner"] is not None or (
                row["send_started_at"] is not None
            ):
                raise ActivationServiceError(
                    "A queued row cancels only without a claim or "
                    "send-start marker"
                )
        elif state == "claimed":
            if row["send_started_at"] is not None:
                raise ActivationServiceError(
                    "The claimant cannot prove that transport did not start"
                )
            if claimant is None or row["claim_owner"] != claimant:
                raise ActivationServiceError(
                    "Only the current claimant proves non-start"
                )
        elif state == "delivery_unknown":
            if not agent_lookup_proves_non_acceptance:
                raise ActivationServiceError(
                    "delivery_unknown cancels only after an authenticated "
                    "agent lookup proves non-acceptance"
                )
            effect_cursor = await connection.execute(
                "SELECT COUNT(*) AS child_effects FROM effect_operations "
                "WHERE activation_id = ? AND activation_attempt = ?",
                (row["activation_id"], row["activation_attempt"]),
            )
            effects = await effect_cursor.fetchone()
            assert effects is not None  # An aggregate query returns one row.
            if int(effects["child_effects"]) > 0:
                raise ActivationServiceError(
                    "A child effect exists; the row cannot cancel"
                )

    async def updates(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET dispatch_state = "
            "'cancelled', terminal_reason = ?, journal_cursor = ? "
            "WHERE grant_id = ?",
            (reason, journal_cursor, grant_id),
        )

    return await _journal_dispatch_row_transition(
        run_id=run_id,
        grant_id=grant_id,
        target_row_state="cancelled",
        reason=reason,
        row_updates=updates,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
    )


async def dead_letter_activation_dispatch(
    *,
    grant_id: str,
    run_id: str,
    reason: str,
    recovery_policy: str = "dead_letter",
    authenticated_recovery_decision: bool = False,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Move one dispatch row to the dead letter with an exact reason."""
    if recovery_policy not in DISPATCH_RECOVERY_POLICIES:
        raise ActivationServiceError(
            f"Unknown recovery policy: {recovery_policy!r}"
        )

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now: str,
    ) -> None:
        state = str(row["dispatch_state"])
        if state == "queued":
            if now < str(row["grant_expires_at"]):
                raise ActivationServiceError(
                    "A queued row dead-letters only after grant expiry"
                )
            if recovery_policy != "dead_letter":
                raise ActivationServiceError(
                    "The recovery policy permits a new delivery"
                )
        elif state == "delivery_unknown":
            if not authenticated_recovery_decision:
                raise ActivationServiceError(
                    "delivery_unknown dead-letters only through a valid "
                    "rejection or an authenticated recovery decision"
                )
            if now < str(row["grant_expires_at"]):
                raise ActivationServiceError(
                    "An authenticated recovery decision applies after "
                    "grant expiry"
                )

    async def updates(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET dispatch_state = "
            "'dead_letter', terminal_reason = ?, journal_cursor = ? "
            "WHERE grant_id = ?",
            (reason, journal_cursor, grant_id),
        )

    return await _journal_dispatch_row_transition(
        run_id=run_id,
        grant_id=grant_id,
        target_row_state="dead_letter",
        reason=reason,
        row_updates=updates,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
    )


async def _record_protected_observation(
    connection: aiosqlite.Connection,
    *,
    kind: str,
    now: str,
    run_id: str | None = None,
    activation_id: str | None = None,
    effect_id: str | None = None,
    dispatch_ref: str | None = None,
    grant_id: str | None = None,
    artifact_digest: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    observation_id = f"observation-{uuid.uuid4()}"
    await connection.execute(
        "INSERT INTO protected_observations ("
        "observation_id, kind, run_id, activation_id, effect_id, "
        "dispatch_ref, grant_id, artifact_digest, payload, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            observation_id,
            kind,
            run_id,
            activation_id,
            effect_id,
            dispatch_ref,
            grant_id,
            artifact_digest,
            json.dumps(payload or {}, sort_keys=True),
            now,
        ),
    )
    return observation_id


async def process_acknowledgement(
    *,
    text: str,
    key_registry: KeyRegistry,
    expected_capability_digest: str | None = None,
    tenant_id: str = "tenant-default",
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Validate and commit one activation acknowledgement.

    The daemon validates the exact grant binding, the audience, the
    protocol, both fences, the nonce, the capability digest, the key
    validity, and the signature. The daemon database receipt time is
    the trusted time. An exact duplicate returns the stored result.
    One accepted acknowledgement moves the outbox row to
    ``acknowledged`` and the activation to ``dispatched`` in one
    transaction; one rejected acknowledgement moves the row to
    ``dead_letter`` and the activation to ``abandoned``, and releases
    the reservation when no linked effect exists.
    """
    acknowledgement = parse_acknowledgement(text)
    grant_row = await get_grant_row(acknowledgement.activation_grant_id)
    dispatch_row = await get_dispatch_row(
        acknowledgement.activation_grant_id,
    )
    identity = await run_identity(str(grant_row["run_id"]))

    _validate_acknowledgement_binding(
        acknowledgement,
        grant_row=grant_row,
        dispatch_row=dispatch_row,
        identity=identity,
        expected_capability_digest=expected_capability_digest,
    )

    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001

    key_registry.require_new_authority(
        acknowledgement.key_id,
        owner_id=acknowledgement.agent_id,
        purpose=AGENT_KEY_PURPOSE,
        at=now,
    )
    verify_acknowledgement_signature(acknowledgement, key_registry)

    digest = acknowledgement.canonical_digest()
    stored = await _find_acknowledgement(
        tenant_id, acknowledgement.acknowledgement_id,
    )
    if stored is not None:
        if str(stored["acknowledgement_digest"]) != digest:
            raise AcknowledgementRejectedError(
                "The acknowledgement identifier arrived with a different "
                "digest"
            )
        return {"status": "duplicate", "stored": stored}

    accepted = await _find_accepted_for_grant(
        acknowledgement.activation_grant_id,
    )
    if accepted is not None and acknowledgement.decision == "accepted":
        if str(accepted["acknowledgement_digest"]) != digest:
            raise AcknowledgementRejectedError(
                "A second accepted acknowledgement must match the stored "
                "bytes exactly"
            )
        return {"status": "duplicate", "stored": accepted}

    late = now >= str(grant_row["expires_at"]) or (
        str(dispatch_row["dispatch_state"])
        in ACTIVATION_DISPATCH_TERMINAL_STATES
    )
    if late:
        async with db._connect() as connection:  # noqa: SLF001
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await _insert_acknowledgement(
                    connection,
                    acknowledgement,
                    tenant_id=tenant_id,
                    digest=digest,
                    now=now,
                    late=True,
                    journal_cursor=None,
                )
                observation_id = await _record_protected_observation(
                    connection,
                    kind="late_acknowledgement",
                    now=now,
                    run_id=str(grant_row["run_id"]),
                    activation_id=str(grant_row["activation_id"]),
                    grant_id=acknowledgement.activation_grant_id,
                    payload={"decision": acknowledgement.decision},
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return {"status": "late_observation", "observation_id": observation_id}

    if acknowledgement.decision == "accepted":
        target_row_state = "acknowledged"
        target_activation_state = "dispatched"
    else:
        target_row_state = "dead_letter"
        target_activation_state = "abandoned"

    activation = await get_activation(
        acknowledgement.activation_id, acknowledgement.attempt,
    )

    async def updates(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        txn_now: str,
    ) -> None:
        validate_activation_transition(
            str(activation["state"]), target_activation_state,
        )
        await _insert_acknowledgement(
            connection,
            acknowledgement,
            tenant_id=tenant_id,
            digest=digest,
            now=txn_now,
            late=False,
            journal_cursor=journal_cursor,
        )
        await connection.execute(
            "UPDATE activation_dispatch_outbox SET dispatch_state = ?, "
            "acknowledgement_id = ?, terminal_reason = ?, "
            "journal_cursor = ? WHERE grant_id = ?",
            (
                target_row_state,
                acknowledgement.acknowledgement_id,
                acknowledgement.decision_reason_code,
                journal_cursor,
                acknowledgement.activation_grant_id,
            ),
        )
        await connection.execute(
            "UPDATE activations SET state = ?, terminal_reason = ?, "
            "state_changed_at = ?, journal_cursor = ? "
            "WHERE activation_id = ? AND attempt = ?",
            (
                target_activation_state,
                acknowledgement.decision_reason_code
                if target_activation_state == "abandoned"
                else None,
                txn_now,
                journal_cursor,
                acknowledgement.activation_id,
                acknowledgement.attempt,
            ),
        )
        if target_activation_state == "abandoned":
            effect_cursor = await connection.execute(
                "SELECT COUNT(*) AS child_effects FROM effect_operations "
                "WHERE activation_id = ? AND activation_attempt = ?",
                (acknowledgement.activation_id, acknowledgement.attempt),
            )
            effects = await effect_cursor.fetchone()
            assert effects is not None  # An aggregate query returns one row.
            reservation_id = activation["reservation_id"]
            if int(effects["child_effects"]) == 0 and (
                reservation_id is not None
            ):
                await budget_service.release_in_transaction(
                    connection,
                    str(reservation_id),
                    database_time=txn_now,
                )

    record = await _journal_dispatch_row_transition(
        run_id=str(grant_row["run_id"]),
        grant_id=acknowledgement.activation_grant_id,
        target_row_state=target_row_state,
        reason=acknowledgement.decision_reason_code,
        row_updates=updates,
        activation_state=target_activation_state,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=(
            f"acknowledgement-{tenant_id}-"
            f"{acknowledgement.acknowledgement_id}"
        ),
    )
    return {
        "status": acknowledgement.decision,
        "record": record,
        "acknowledgement_digest": digest,
    }


def _validate_acknowledgement_binding(
    acknowledgement: ActivationAcknowledgement,
    *,
    grant_row: dict[str, Any],
    dispatch_row: dict[str, Any],
    identity: dict[str, str],
    expected_capability_digest: str | None,
) -> None:
    checks: tuple[tuple[str, Any, Any], ...] = (
        (
            "grant_digest",
            acknowledgement.activation_grant_digest,
            str(grant_row["grant_artifact_digest"]),
        ),
        ("task", acknowledgement.task_id, str(grant_row["task_id"])),
        ("run", acknowledgement.run_id, str(grant_row["run_id"])),
        (
            "runtime_pair",
            acknowledgement.runtime_key.to_dict(),
            {
                "runtime_id": identity["runtime_id"],
                "runtime_contract_version": (
                    identity["runtime_contract_version"]
                ),
            },
        ),
        (
            "activation",
            acknowledgement.activation_id,
            str(grant_row["activation_id"]),
        ),
        ("attempt", acknowledgement.attempt, int(grant_row["attempt"])),
        (
            "task_fence",
            acknowledgement.task_fence,
            str(grant_row["task_fence"]),
        ),
        (
            "activation_fence",
            acknowledgement.activation_fence,
            str(grant_row["activation_fence"]),
        ),
        ("agent", acknowledgement.agent_id, str(grant_row["agent_id"])),
        ("audience", acknowledgement.audience, str(grant_row["audience"])),
        (
            "protocol",
            acknowledgement.agent_protocol_version,
            str(grant_row["agent_protocol_version"]),
        ),
        ("nonce", acknowledgement.grant_nonce, str(grant_row["grant_nonce"])),
    )
    for name, observed, expected in checks:
        if observed != expected:
            raise AcknowledgementRejectedError(
                f"The acknowledgement binds a different {name}"
            )
    if acknowledgement.decision_reason_code not in (
        ACKNOWLEDGEMENT_REASON_CODES
    ):
        raise AcknowledgementRejectedError(
            "The acknowledgement uses an unregistered reason code"
        )
    if expected_capability_digest is not None and (
        acknowledgement.capability_digest != expected_capability_digest
    ):
        raise AcknowledgementRejectedError(
            "The acknowledgement binds a different capability_digest"
        )


async def _find_acknowledgement(
    tenant_id: str, acknowledgement_id: str,
) -> dict[str, Any] | None:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM activation_acknowledgements "
            "WHERE tenant_id = ? AND acknowledgement_id = ?",
            (tenant_id, acknowledgement_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def _find_accepted_for_grant(grant_id: str) -> dict[str, Any] | None:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM activation_acknowledgements "
            "WHERE activation_grant_id = ? AND decision = 'accepted' "
            "AND late_observation = 0",
            (grant_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def _insert_acknowledgement(
    connection: aiosqlite.Connection,
    acknowledgement: ActivationAcknowledgement,
    *,
    tenant_id: str,
    digest: str,
    now: str,
    late: bool,
    journal_cursor: int | None,
) -> None:
    await connection.execute(
        "INSERT INTO activation_acknowledgements ("
        "acknowledgement_id, tenant_id, activation_grant_id, "
        "acknowledgement_digest, decision, decision_reason_code, agent_id, "
        "key_id, stored_bytes, late_observation, agent_observed_at, "
        "received_at, journal_cursor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            acknowledgement.acknowledgement_id,
            tenant_id,
            acknowledgement.activation_grant_id,
            digest,
            acknowledgement.decision,
            acknowledgement.decision_reason_code,
            acknowledgement.agent_id,
            acknowledgement.key_id,
            acknowledgement.to_bytes().decode("utf-8"),
            1 if late else 0,
            acknowledgement.agent_observed_at,
            now,
            journal_cursor,
        ),
    )


# ── Proposal eligibility and decisions ───────────────────────────────


async def validate_proposal_eligibility(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    proposal_digest: str,
    request_digest: str,
    effect_id: str | None = None,
    reservation_validator: ReservationValidator | None = None,
    database_time: str | None = None,
) -> None:
    """Validate every proposal eligibility condition or fail closed.

    An ineligible proposal commits zero runtime state decisions; its
    policy-compliant audit artifacts stay available.
    """
    validator = reservation_validator or budget_service.reservation_is_valid
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        activation = await _load_activation(connection, activation_id, attempt)
        if activation["raw_result_artifact_digest"] is None:
            raise ProposalEligibilityError("protected_observation")
        if activation["proposal_digest"] is None or (
            str(activation["proposal_digest"]) != proposal_digest
        ):
            raise ProposalEligibilityError("proposal_parse")
        if str(activation["request_digest"] or "") != request_digest:
            raise ProposalEligibilityError("request_match")
        if str(activation["run_id"]) != run_id:
            raise ProposalEligibilityError("activation_identity")
        if effect_id is not None:
            effect_ids = json.loads(str(activation["effect_ids"]))
            if effect_id not in effect_ids:
                raise ProposalEligibilityError("effect_reference")
        control_cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE run_id = ?", (run_id,),
        )
        control = await control_cursor.fetchone()
        if control is None or (
            str(control["task_fence"]) != str(activation["task_fence"])
        ):
            raise ProposalEligibilityError("task_fence")
        lease_cursor = await connection.execute(
            "SELECT * FROM activation_leases WHERE lease_id = ?",
            (activation["lease_id"],),
        )
        lease = await lease_cursor.fetchone()
        if lease is None or lease["released"] or (
            str(lease["expires_at"]) <= now
        ):
            raise ProposalEligibilityError("activation_fence")
        if str(control["cancellation_state"]) != "active":
            raise ProposalEligibilityError("cancellation")
        deadline = control["deadline_at"]
        if control["deadline_expired"] or (
            deadline is not None and str(deadline) <= now
        ):
            raise ProposalEligibilityError("deadline")
        reservation_id = activation["reservation_id"]
    if reservation_id is None or not await validator(str(reservation_id)):
        raise ProposalEligibilityError("budget")


async def commit_proposal_decision(
    *,
    run_id: str,
    activation_id: str,
    attempt: int,
    decision: str,
    proposal_digest: str,
    request_digest: str,
    execution_envelope_digest: str,
    projection_changes: dict[str, Any] | None = None,
    checkpoint_digest: str = "",
    budget: dict[str, int] | None = None,
    effect_id: str | None = None,
    reservation_validator: ReservationValidator | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Commit exactly one proposal decision for one eligible proposal."""
    await validate_proposal_eligibility(
        run_id=run_id,
        activation_id=activation_id,
        attempt=attempt,
        proposal_digest=proposal_digest,
        request_digest=request_digest,
        effect_id=effect_id,
        reservation_validator=reservation_validator,
        database_time=database_time,
    )
    identity = await run_identity(run_id)

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_activation(connection, activation_id, attempt)
        validate_activation_transition(str(row["state"]), "committed")
        await connection.execute(
            "UPDATE activations SET state = 'committed', "
            "execution_envelope_digest = ?, state_changed_at = ?, "
            "journal_cursor = ? WHERE activation_id = ? AND attempt = ?",
            (
                execution_envelope_digest,
                now,
                journal_cursor,
                activation_id,
                attempt,
            ),
        )

    return await journal.commit_operation(
        journal.JournalOperation(
            operation_type="proposal_decision",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload={
                "decision": decision,
                "proposal_digest": proposal_digest,
                "execution_envelope_digest": execution_envelope_digest,
                "projection_changes": dict(projection_changes or {}),
                "checkpoint_digest": checkpoint_digest,
                "circuit_state": "closed",
                "circuit_decision": "allow",
                "activation_id": activation_id,
                "activation_state": "committed",
                "activation_attempt": attempt,
                "budget": dict(budget or {"reserved": 0, "consumed": 0}),
                "trace_event": "proposal.decision",
                "effect_id": effect_id,
            },
            idempotency_token=(
                f"proposal-decision-{activation_id}-{attempt}"
            ),
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra,
    )
