"""Foundation durable external-effect operations and attempts.

One ``EffectOperation`` names a logical external action. One immutable
``EffectAttempt`` row exists for each possible transport start. Every
attempt owns its own effect identifier, reservation, dispatch
reference, grant, and receipt chain. A transport retry creates a new
attempt; no attempt ever returns to a pre-transport state after
transport could have started.

The service guarantees one durable intent and complete attempt
evidence. It does not promise exactly-once external execution. An
unknown effect keeps its reservation open or applies the pessimistic
charge; it never appears as a zero-cost cancellation.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import budget_service
import database as db
import runtime_journal as journal
from activation_service import (
    _load_activation,
    _record_protected_observation,
    persist_protected_artifact,
    run_identity,
)
from agent_protocol import (
    AgentAttemptReceipt,
    ReceiptError,
    sign_effect_grant,
    verify_attempt_receipt_signature,
)
from core.activation_states import (
    validate_effect_transition,
)
from core.asset_store import DataClass
from core.failpoints import failpoint

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    from core.asset_store import ArtifactStore
    from core.signing import KeyRegistry

EFFECT_KINDS = (
    "provider",
    "tool",
    "environment",
    "import",
    "benchmark_admission",
    "judge",
)

RETRY_SAFETY_LEVELS = ("safe", "conditional", "unsafe")

EFFECT_FAILPOINTS = (
    "effect.before_transport_start_marker",
    "effect.after_transport_start_marker",
    "effect.before_raw_persist",
    "effect.after_raw_persist",
    "effect.after_parse",
)


class EffectServiceError(ValueError):
    """One effect service rule failed closed."""


class EffectConflictError(EffectServiceError):
    """An idempotency key was reused with a different request."""


class EffectDispatchError(EffectServiceError):
    """A dispatch eligibility condition failed before transport."""


class RetryPolicyError(EffectServiceError):
    """A retry path violated its declared safety level."""


@dataclass(frozen=True)
class AdapterCapabilities:
    """The versioned capability declaration of one adapter."""

    adapter_id: str
    adapter_version: str
    idempotency_key_scope: str
    idempotency_retention: str
    provider_run_lookup: bool
    result_retrieval: bool
    cancellation_semantics: str
    compensation_support: str
    provider_receipt_support: bool
    usage_finalization: str
    retry_safety: str

    def __post_init__(self) -> None:
        if self.retry_safety not in RETRY_SAFETY_LEVELS:
            raise EffectServiceError(
                f"Unknown retry safety: {self.retry_safety!r}"
            )


class AdapterRegistry:
    """Versioned adapter capability records."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], AdapterCapabilities] = {}

    def register(self, capabilities: AdapterCapabilities) -> None:
        key = (capabilities.adapter_id, capabilities.adapter_version)
        self._records[key] = capabilities

    def require(
        self, adapter_id: str, adapter_version: str,
    ) -> AdapterCapabilities:
        record = self._records.get((adapter_id, adapter_version))
        if record is None:
            raise EffectServiceError(
                f"Unknown adapter: {adapter_id}@{adapter_version}"
            )
        return record


# ── Row access ───────────────────────────────────────────────────────


async def _load_operation(
    connection: aiosqlite.Connection, effect_operation_id: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM effect_operations WHERE effect_operation_id = ?",
        (effect_operation_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise EffectServiceError(
            f"Unknown effect operation: {effect_operation_id}"
        )
    return dict(row)


async def get_operation(effect_operation_id: str) -> dict[str, Any]:
    """Read one effect operation row."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_operation(connection, effect_operation_id)


async def _load_attempt(
    connection: aiosqlite.Connection, effect_id: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM effect_attempts WHERE effect_id = ?", (effect_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise EffectServiceError(f"Unknown effect attempt: {effect_id}")
    return dict(row)


async def get_attempt(effect_id: str) -> dict[str, Any]:
    """Read one immutable effect attempt row."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_attempt(connection, effect_id)


async def _load_effect_dispatch(
    connection: aiosqlite.Connection, dispatch_ref: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM effect_dispatch_outbox WHERE dispatch_ref = ?",
        (dispatch_ref,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise EffectServiceError(f"Unknown effect dispatch: {dispatch_ref}")
    return dict(row)


async def get_effect_dispatch(dispatch_ref: str) -> dict[str, Any]:
    """Read one effect dispatch outbox row."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_effect_dispatch(connection, dispatch_ref)


async def get_effect_grant_row(token_id: str) -> dict[str, Any]:
    """Read one stored effect grant row."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM effect_grants WHERE token_id = ?", (token_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise EffectServiceError(f"Unknown effect grant: {token_id}")
        return dict(row)


def _effect_operation_payload(
    operation: dict[str, Any] | None,
) -> dict[str, Any]:
    if operation is None:
        return {}
    return {
        "effect_operation_id": operation["effect_operation_id"],
        "authoritative_result_effect_id": operation.get(
            "authoritative_result_effect_id",
        ),
    }


async def _journal_effect_transition(
    *,
    run_id: str,
    effect_id: str,
    target_state: str,
    payload_extra: dict[str, Any] | None = None,
    extra_writes: Callable[
        [aiosqlite.Connection, dict[str, Any], int, str], Awaitable[None],
    ]
    | None = None,
    guard: Callable[
        [aiosqlite.Connection, dict[str, Any], str], Awaitable[None],
    ]
    | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
    idempotency_token: str | None = None,
) -> journal.JournalRecord:
    """Journal one effect attempt transition through the unit of work."""
    identity = await run_identity(run_id)

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_attempt(connection, effect_id)
        validate_effect_transition(str(row["state"]), target_state)
        if guard is not None:
            await guard(connection, row, now)
        await connection.execute(
            "UPDATE effect_attempts SET state = ?, state_changed_at = ?, "
            "journal_cursor = ? WHERE effect_id = ?",
            (target_state, now, journal_cursor, effect_id),
        )
        if extra_writes is not None:
            await extra_writes(connection, row, journal_cursor, now)

    attempt = await get_attempt(effect_id)
    operation = await get_operation(str(attempt["effect_operation_id"]))
    payload = {
        "effect_id": effect_id,
        "effect_state": target_state,
        "effect_attempt_number": int(attempt["effect_attempt_number"]),
        **_effect_operation_payload(operation),
        **(payload_extra or {}),
    }
    return await journal.commit_operation(
        journal.JournalOperation(
            operation_type="effect_transition",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload=payload,
            idempotency_token=idempotency_token
            or f"effect-{effect_id}-{target_state}-{uuid.uuid4()}",
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra,
    )


# ── Intent, approval, and dispatch queuing ───────────────────────────


async def create_effect_intent(
    *,
    run_id: str,
    activation_id: str,
    activation_attempt: int,
    kind: str,
    request_digest: str,
    idempotency_scope: str,
    child_idempotency_key: str,
    reservation_id: str,
    retry_safety: str,
    provider_operation_key: str | None = None,
    retry_of_effect_id: str | None = None,
    approval_id: str | None = None,
    lookup_evidence: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Create one effect operation link and one attempt in ``intent``.

    A duplicate child idempotency key with an equal request digest
    returns the existing operation and its open attempt. A different
    request digest under the same key is a conflict; a changed request
    creates a new effect operation under a new key.
    """
    if kind not in EFFECT_KINDS:
        raise EffectServiceError(f"Unknown effect kind: {kind!r}")
    if retry_safety not in RETRY_SAFETY_LEVELS:
        raise EffectServiceError(f"Unknown retry safety: {retry_safety!r}")
    identity = await run_identity(run_id)

    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM effect_operations WHERE idempotency_scope = ? "
            "AND child_idempotency_key = ?",
            (idempotency_scope, child_idempotency_key),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            if str(existing["request_digest"]) != request_digest:
                raise EffectConflictError(
                    "The child idempotency key was reused with a "
                    "different request digest"
                )
            attempt_cursor = await connection.execute(
                "SELECT * FROM effect_attempts WHERE effect_operation_id "
                "= ? ORDER BY effect_attempt_number DESC LIMIT 1",
                (existing["effect_operation_id"],),
            )
            attempt_row = await attempt_cursor.fetchone()
            return {
                "effect_operation_id": str(existing["effect_operation_id"]),
                "effect_id": str(attempt_row["effect_id"]),
                "dispatch_ref": str(attempt_row["dispatch_ref"]),
                "effect_attempt_number": int(
                    attempt_row["effect_attempt_number"],
                ),
                "duplicate": True,
            }

    operation_id = f"effect-operation-{uuid.uuid4()}"
    effect_id = f"effect-{uuid.uuid4()}"
    dispatch_ref = f"effect-dispatch-{uuid.uuid4()}"
    if retry_of_effect_id is not None:
        predecessor = await get_attempt(retry_of_effect_id)
        operation_id = str(predecessor["effect_operation_id"])
        if str(predecessor["request_digest"]) != request_digest:
            raise EffectConflictError(
                "Every retry attempt uses the same logical request digest; "
                "a changed request creates a new effect operation"
            )

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        attempt_number = 1
        if retry_of_effect_id is None:
            await connection.execute(
                "INSERT INTO effect_operations ("
                "effect_operation_id, run_id, task_id, tenant_id, "
                "activation_id, activation_attempt, kind, request_digest, "
                "idempotency_scope, child_idempotency_key, created_at, "
                "journal_cursor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    run_id,
                    identity["task_id"],
                    identity["tenant_id"],
                    activation_id,
                    activation_attempt,
                    kind,
                    request_digest,
                    idempotency_scope,
                    child_idempotency_key,
                    now,
                    journal_cursor,
                ),
            )
        else:
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(effect_attempt_number), 0) AS last "
                "FROM effect_attempts WHERE effect_operation_id = ?",
                (operation_id,),
            )
            row = await cursor.fetchone()
            assert row is not None  # An aggregate query returns one row.
            attempt_number = int(row["last"]) + 1
        await connection.execute(
            "INSERT INTO effect_attempts ("
            "effect_id, effect_operation_id, effect_attempt_number, "
            "retry_of_effect_id, request_digest, provider_operation_key, "
            "reservation_id, dispatch_ref, run_id, task_id, state, "
            "retry_safety, approval_id, lookup_evidence, created_at, "
            "state_changed_at, journal_cursor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?, ?, ?, ?, "
            "?, ?)",
            (
                effect_id,
                operation_id,
                attempt_number,
                retry_of_effect_id,
                request_digest,
                provider_operation_key,
                reservation_id,
                dispatch_ref,
                run_id,
                identity["task_id"],
                retry_safety,
                approval_id,
                lookup_evidence,
                now,
                now,
                journal_cursor,
            ),
        )

    record = await journal.commit_operation(
        journal.JournalOperation(
            operation_type="effect_transition",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload={
                "effect_id": effect_id,
                "effect_state": "intent",
                "effect_operation_id": operation_id,
                "retry_of_effect_id": retry_of_effect_id,
            },
            idempotency_token=f"effect-intent-{effect_id}",
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra,
    )
    attempt = await get_attempt(effect_id)
    return {
        "effect_operation_id": operation_id,
        "effect_id": effect_id,
        "dispatch_ref": dispatch_ref,
        "effect_attempt_number": int(attempt["effect_attempt_number"]),
        "duplicate": False,
        "record": record,
    }


async def approve_effect(
    *,
    run_id: str,
    effect_id: str,
    reservation_validator: Callable[[str], Awaitable[bool]] | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Approve one intent after the policy and budget checks pass."""
    attempt = await get_attempt(effect_id)
    validator = reservation_validator or budget_service.reservation_is_valid
    if not await validator(str(attempt["reservation_id"])):
        raise EffectServiceError(
            "Approval requires one valid budget reservation"
        )
    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="approved",
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-approve-{effect_id}",
    )


async def deny_effect(
    *,
    run_id: str,
    effect_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Deny one intent before any dispatch obligation exists."""

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_attempts SET terminal_reason = ? "
            "WHERE effect_id = ?",
            (reason, effect_id),
        )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="denied",
        payload_extra={"reason": reason},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-deny-{effect_id}",
    )


async def cancel_effect(
    *,
    run_id: str,
    effect_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Cancel one effect attempt before any transport could start.

    The pure table permits cancellation from ``intent``, ``approved``,
    and ``dispatch_queued`` only. The guard proves that no dispatch
    claim or transport-start marker exists.
    """

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now: str,
    ) -> None:
        cursor = await connection.execute(
            "SELECT * FROM effect_dispatch_outbox WHERE dispatch_ref = ?",
            (row["dispatch_ref"],),
        )
        dispatch = await cursor.fetchone()
        if dispatch is not None and (
            dispatch["claim_owner"] is not None
            or dispatch["transport_started_at"] is not None
        ):
            raise EffectServiceError(
                "A claimed or started dispatch cannot cancel here"
            )

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_attempts SET terminal_reason = ? "
            "WHERE effect_id = ?",
            (reason, effect_id),
        )
        await connection.execute(
            "UPDATE effect_dispatch_outbox SET dispatch_state = "
            "'cancelled', journal_cursor = ? WHERE dispatch_ref = ? "
            "AND dispatch_state IN ('queued', 'claimed')",
            (journal_cursor, row["dispatch_ref"]),
        )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="cancelled",
        payload_extra={"reason": reason},
        extra_writes=extra,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-cancel-{effect_id}",
    )


async def queue_effect_dispatch(
    *,
    run_id: str,
    effect_id: str,
    target: str,
    dispatch_policy: str = "default",
    not_before: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Create the dispatch outbox row in the approval unit of work."""

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "INSERT INTO effect_dispatch_outbox ("
            "journal_cursor, run_id, effect_id, dispatch_ref, "
            "effect_operation_id, effect_attempt_number, target, "
            "request_digest, not_before, dispatch_policy, dispatch_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
            (
                journal_cursor,
                run_id,
                effect_id,
                row["dispatch_ref"],
                row["effect_operation_id"],
                row["effect_attempt_number"],
                target,
                row["request_digest"],
                not_before or now,
                dispatch_policy,
            ),
        )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="dispatch_queued",
        payload_extra={"target": target},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-dispatch-queue-{effect_id}",
    )


# ── Dispatch claim, effect grant, and transport markers ──────────────


async def claim_effect_dispatch(
    *,
    run_id: str,
    effect_id: str,
    dispatcher: str,
    claim_ttl_seconds: float,
    grant_ttl_seconds: float,
    daemon_private_key: Ed25519PrivateKey,
    key_id: str,
    key_registry: KeyRegistry,
    artifact_store: ArtifactStore,
    agent_id: str,
    audience: str,
    protocol_version: str,
    capability_digest: str,
    operation: str,
    max_authorized_amount_nanos: int,
    provider: str | None = None,
    model: str | None = None,
    tool: str | None = None,
    expected_target: str | None = None,
    reservation_validator: Callable[[str], Awaitable[bool]] | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Claim one queued effect dispatch and store its signed grant.

    The signed ``EffectGrant`` precomputes before the claim
    transaction. The claim rechecks the live fences, the controls, the
    deadline, the reservation, the target, and the request digest, and
    stores the grant digest, nonce, dispatcher, dispatch fence, and
    expiry while it moves the effect to ``dispatch_claimed``. The
    exact stored grant bytes return only after the claim commits.
    """
    attempt = await get_attempt(effect_id)
    operation_row = await get_operation(str(attempt["effect_operation_id"]))
    activation = await get_activation_for_operation(operation_row)

    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
    key_registry.require_new_authority(
        key_id, owner_id="daemon", purpose="daemon-grant", at=now,
    )

    token_id = f"effect-token-{uuid.uuid4()}"
    grant_nonce = f"nonce-{uuid.uuid4()}"
    dispatch_fence = f"dispatch-fence-{uuid.uuid4()}"
    grant = sign_effect_grant(
        {
            "schema_version": "1",
            "token_id": token_id,
            "task_id": str(attempt["task_id"]),
            "run_id": run_id,
            "activation_id": str(operation_row["activation_id"]),
            "activation_attempt": int(operation_row["activation_attempt"]),
            "effect_operation_id": str(attempt["effect_operation_id"]),
            "effect_id": effect_id,
            "effect_attempt_number": int(attempt["effect_attempt_number"]),
            "dispatch_ref": str(attempt["dispatch_ref"]),
            "task_fence": str(activation["task_fence"] or ""),
            "lease_ref": str(activation["lease_id"] or ""),
            "request_digest": str(attempt["request_digest"]),
            "reservation_id": str(attempt["reservation_id"]),
            "max_authorized_amount_nanos": max_authorized_amount_nanos,
            "provider": provider,
            "model": model,
            "tool": tool,
            "operation": operation,
            "capability_digest": capability_digest,
            "agent_id": agent_id,
            "audience": audience,
            "issued_at": now,
            "expires_at": db._shifted(now, grant_ttl_seconds),  # noqa: SLF001
            "grant_nonce": grant_nonce,
            "protocol_version": protocol_version,
            "key_id": key_id,
        },
        daemon_private_key,
    )
    grant_bytes = grant.to_bytes()
    grant_artifact_digest = persist_protected_artifact(
        artifact_store,
        grant_bytes,
        media_type="application/json",
        access_policy="foundation-grant",
        referenced_by=f"effect-dispatch-{attempt['dispatch_ref']}",
    )

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now_txn: str,
    ) -> None:
        dispatch = await _load_effect_dispatch(
            connection, str(row["dispatch_ref"]),
        )
        if str(dispatch["dispatch_state"]) != "queued":
            raise EffectDispatchError("dispatch_row")
        if expected_target is not None and (
            dispatch["target"] != expected_target
        ):
            raise EffectDispatchError("target")
        if str(dispatch["request_digest"]) != str(row["request_digest"]):
            raise EffectDispatchError("request_digest")
        control_cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE run_id = ?", (run_id,),
        )
        control = await control_cursor.fetchone()
        if control is None:
            raise EffectDispatchError("run_control")
        if str(control["cancellation_state"]) != "active":
            raise EffectDispatchError("cancellation")
        deadline = control["deadline_at"]
        if control["deadline_expired"] or (
            deadline is not None and str(deadline) <= now_txn
        ):
            raise EffectDispatchError("deadline")
        live_activation = await _load_activation(
            connection,
            str(operation_row["activation_id"]),
            int(operation_row["activation_attempt"]),
        )
        if str(live_activation["state"]) != "dispatched":
            raise EffectDispatchError("activation_state")
        if str(control["task_fence"]) != str(
            live_activation["task_fence"] or "",
        ):
            raise EffectDispatchError("task_fence")
        lease_cursor = await connection.execute(
            "SELECT * FROM activation_leases WHERE lease_id = ?",
            (live_activation["lease_id"],),
        )
        lease = await lease_cursor.fetchone()
        if lease is None or lease["released"] or (
            str(lease["expires_at"]) <= now_txn
        ):
            raise EffectDispatchError("activation_fence")
        if not await _reservation_valid_in_connection(
            connection, str(row["reservation_id"]),
        ):
            raise EffectDispatchError("reservation")

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now_txn: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_dispatch_outbox SET dispatch_state = 'claimed', "
            "claim_owner = ?, dispatch_fence = ?, claimed_at = ?, "
            "claim_expires_at = ?, grant_token_id = ?, grant_digest = ?, "
            "grant_nonce = ?, grant_expires_at = ?, attempts = attempts + "
            "1, last_attempt_at = ?, journal_cursor = ? "
            "WHERE dispatch_ref = ?",
            (
                dispatcher,
                dispatch_fence,
                now_txn,
                db._shifted(now_txn, claim_ttl_seconds),  # noqa: SLF001
                token_id,
                grant_artifact_digest,
                grant_nonce,
                grant.expires_at,
                now_txn,
                journal_cursor,
                row["dispatch_ref"],
            ),
        )
        await connection.execute(
            "INSERT INTO effect_grants ("
            "token_id, effect_operation_id, effect_id, "
            "effect_attempt_number, dispatch_ref, activation_id, "
            "activation_attempt, run_id, task_id, request_digest, "
            "task_fence, lease_ref, reservation_id, "
            "max_authorized_amount_nanos, provider, model, tool, "
            "operation, capability_digest, agent_id, audience, issued_at, "
            "expires_at, grant_nonce, protocol_version, schema_version, "
            "digest_profile, signature_algorithm, key_id, "
            "grant_artifact_digest, dispatcher, dispatch_fence, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                grant.effect_operation_id,
                effect_id,
                grant.effect_attempt_number,
                grant.dispatch_ref,
                grant.activation_id,
                grant.activation_attempt,
                run_id,
                grant.task_id,
                grant.request_digest,
                grant.task_fence,
                grant.lease_ref,
                grant.reservation_id,
                grant.max_authorized_amount_nanos,
                grant.provider,
                grant.model,
                grant.tool,
                grant.operation,
                grant.capability_digest,
                grant.agent_id,
                grant.audience,
                grant.issued_at,
                grant.expires_at,
                grant.grant_nonce,
                grant.protocol_version,
                grant.schema_version,
                grant.digest_profile,
                grant.signature_algorithm,
                grant.key_id,
                grant_artifact_digest,
                dispatcher,
                dispatch_fence,
                now_txn,
            ),
        )

    record = await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="dispatch_claimed",
        payload_extra={
            "dispatch_ref": str(attempt["dispatch_ref"]),
            "token_id": token_id,
        },
        extra_writes=extra,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-claim-{effect_id}-{token_id}",
    )
    stored = artifact_store.read_object(grant_artifact_digest)
    return {
        "record": record,
        "grant": grant,
        "grant_bytes": stored["payload"],
        "token_id": token_id,
        "dispatch_fence": dispatch_fence,
    }


async def get_activation_for_operation(
    operation_row: dict[str, Any],
) -> dict[str, Any]:
    """Read the activation attempt that owns one effect operation."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_activation(
            connection,
            str(operation_row["activation_id"]),
            int(operation_row["activation_attempt"]),
        )


async def _reservation_valid_in_connection(
    connection: aiosqlite.Connection, reservation_id: str,
) -> bool:
    cursor = await connection.execute(
        "SELECT state FROM budget_reservations WHERE reservation_id = ?",
        (reservation_id,),
    )
    row = await cursor.fetchone()
    return row is not None and str(row["state"]) in ("requested", "reserved")


async def validate_before_transport(
    *,
    dispatch_ref: str,
    dispatcher: str,
    database_time: str | None = None,
) -> None:
    """Validate every live authority immediately before transport.

    The check covers live cancellation, the deadline, the fences, the
    grant expiry, and the reservation. Any failed check rejects the
    transport before the first byte.
    """
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        dispatch = await _load_effect_dispatch(connection, dispatch_ref)
        if str(dispatch["dispatch_state"]) != "claimed" or (
            dispatch["claim_owner"] != dispatcher
        ):
            raise EffectDispatchError("dispatch_row")
        attempt = await _load_attempt(connection, str(dispatch["effect_id"]))
        if str(attempt["state"]) != "dispatch_claimed":
            raise EffectDispatchError("effect_state")
        if str(dispatch["grant_expires_at"] or "") <= now:
            raise EffectDispatchError("grant_expiry")
        control_cursor = await connection.execute(
            "SELECT * FROM run_controls WHERE run_id = ?",
            (dispatch["run_id"],),
        )
        control = await control_cursor.fetchone()
        if control is None:
            raise EffectDispatchError("run_control")
        if str(control["cancellation_state"]) != "active":
            raise EffectDispatchError("cancellation")
        deadline = control["deadline_at"]
        if control["deadline_expired"] or (
            deadline is not None and str(deadline) <= now
        ):
            raise EffectDispatchError("deadline")
        operation_row = await _load_operation(
            connection, str(attempt["effect_operation_id"]),
        )
        activation = await _load_activation(
            connection,
            str(operation_row["activation_id"]),
            int(operation_row["activation_attempt"]),
        )
        if str(control["task_fence"]) != str(activation["task_fence"] or ""):
            raise EffectDispatchError("task_fence")
        if not await _reservation_valid_in_connection(
            connection, str(attempt["reservation_id"]),
        ):
            raise EffectDispatchError("reservation")


async def record_transport_start(
    *,
    dispatch_ref: str,
    dispatcher: str,
    database_time: str | None = None,
) -> bool:
    """Persist the durable transport-start marker for one dispatch."""
    failpoint("effect.before_transport_start_marker")
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE effect_dispatch_outbox SET transport_started_at = ? "
            "WHERE dispatch_ref = ? AND claim_owner = ? "
            "AND dispatch_state = 'claimed' "
            "AND transport_started_at IS NULL",
            (now, dispatch_ref, dispatcher),
        )
        await connection.commit()
        started = cursor.rowcount == 1
    failpoint("effect.after_transport_start_marker")
    return started


async def unclaim_unstarted_dispatch(
    *,
    run_id: str,
    effect_id: str,
    dispatcher: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Return one provably unstarted claim to ``dispatch_queued``.

    Only the current dispatcher proves that no transport-start marker
    exists. This recovers one unstarted claim; it is not a transport
    retry.
    """

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now: str,
    ) -> None:
        dispatch = await _load_effect_dispatch(
            connection, str(row["dispatch_ref"]),
        )
        if dispatch["claim_owner"] != dispatcher:
            raise EffectServiceError(
                "Only the current dispatcher recovers its unstarted claim"
            )
        if dispatch["transport_started_at"] is not None:
            raise EffectServiceError(
                "A transport-start marker exists; the claim cannot return "
                "to dispatch_queued"
            )

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_dispatch_outbox SET dispatch_state = 'queued', "
            "claim_owner = NULL, claimed_at = NULL, claim_expires_at = "
            "NULL, grant_token_id = NULL, grant_digest = NULL, "
            "grant_nonce = NULL, grant_expires_at = NULL, "
            "journal_cursor = ? WHERE dispatch_ref = ?",
            (journal_cursor, row["dispatch_ref"]),
        )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="dispatch_queued",
        payload_extra={"reason": "proven_unstarted_claim"},
        extra_writes=extra,
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
    )


async def mark_outcome_unknown(
    *,
    run_id: str,
    effect_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Mark one claimed dispatch as ``outcome_unknown``.

    Recovery cannot infer that a claimed dispatch failed before
    transport. Without a proven outcome, the attempt stays visible as
    uncertain, and its reservation stays open or charges
    pessimistically.
    """

    async def guard(
        connection: aiosqlite.Connection, row: dict[str, Any], now: str,
    ) -> None:
        dispatch = await _load_effect_dispatch(
            connection, str(row["dispatch_ref"]),
        )
        if dispatch["claim_owner"] is None:
            raise EffectServiceError(
                "Only a claimed dispatch can reach outcome_unknown"
            )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="outcome_unknown",
        payload_extra={"reason": reason},
        guard=guard,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-unknown-{effect_id}",
    )


# ── Receipts ─────────────────────────────────────────────────────────


async def record_attempt_receipt(
    *,
    receipt: AgentAttemptReceipt,
    key_registry: KeyRegistry,
    tenant_id: str = "tenant-default",
    database_time: str | None = None,
) -> dict[str, Any]:
    """Verify and store one signed attempt receipt.

    The daemon validates every binding against the stored grant and
    attempt, requires a monotonic receipt sequence within the
    dispatch, and rejects a replayed receipt identifier. The daemon
    database receipt time is the trusted time.
    """
    verify_attempt_receipt_signature(receipt, key_registry)
    grant_row = await get_effect_grant_row(receipt.token_id)
    attempt = await get_attempt(receipt.effect_id)
    bindings: tuple[tuple[str, Any, Any], ...] = (
        (
            "effect_operation",
            receipt.effect_operation_id,
            str(grant_row["effect_operation_id"]),
        ),
        ("effect", receipt.effect_id, str(grant_row["effect_id"])),
        (
            "effect_attempt_number",
            receipt.effect_attempt_number,
            int(grant_row["effect_attempt_number"]),
        ),
        ("dispatch", receipt.dispatch_ref, str(grant_row["dispatch_ref"])),
        (
            "request_digest",
            receipt.request_digest,
            str(attempt["request_digest"]),
        ),
        ("provider", receipt.provider, grant_row["provider"]),
        ("agent", receipt.agent_id, str(grant_row["agent_id"])),
        (
            "activation",
            receipt.activation_id,
            str(grant_row["activation_id"]),
        ),
        (
            "activation_attempt",
            receipt.activation_attempt,
            int(grant_row["activation_attempt"]),
        ),
    )
    for name, observed, expected in bindings:
        if observed != expected:
            raise ReceiptError(f"The receipt binds a different {name}")

    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        key_registry.require_new_authority(
            receipt.key_id,
            owner_id=receipt.agent_id,
            purpose="agent-receipt",
            at=now,
        )
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS replayed FROM attempt_receipts "
                "WHERE receipt_id = ?",
                (receipt.receipt_id,),
            )
            replayed = await cursor.fetchone()
            if int(replayed["replayed"]) > 0:
                raise ReceiptError(
                    "The receipt identifier was already recorded"
                )
            sequence_cursor = await connection.execute(
                "SELECT COALESCE(MAX(receipt_sequence), 0) AS last "
                "FROM attempt_receipts WHERE dispatch_ref = ?",
                (receipt.dispatch_ref,),
            )
            sequence_row = await sequence_cursor.fetchone()
            expected_sequence = int(sequence_row["last"]) + 1
            if receipt.receipt_sequence != expected_sequence:
                raise ReceiptError(
                    "The receipt sequence is not monotonic within the "
                    "dispatch"
                )
            await connection.execute(
                "INSERT INTO attempt_receipts ("
                "receipt_id, tenant_id, effect_operation_id, effect_id, "
                "effect_attempt_number, dispatch_ref, token_id, "
                "activation_id, activation_attempt, receipt_sequence, "
                "stage, request_digest, provider, model, tool, operation, "
                "transport_observation, provider_run_id, provider_receipt, "
                "raw_response_digest, usage, agent_id, protocol_version, "
                "key_id, signature, agent_observed_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    tenant_id,
                    receipt.effect_operation_id,
                    receipt.effect_id,
                    receipt.effect_attempt_number,
                    receipt.dispatch_ref,
                    receipt.token_id,
                    receipt.activation_id,
                    receipt.activation_attempt,
                    receipt.receipt_sequence,
                    receipt.stage,
                    receipt.request_digest,
                    receipt.provider,
                    receipt.model,
                    receipt.tool,
                    receipt.operation,
                    receipt.transport_observation,
                    receipt.provider_run_id,
                    receipt.provider_receipt,
                    receipt.raw_response_digest,
                    json.dumps(receipt.usage, sort_keys=True)
                    if receipt.usage is not None
                    else None,
                    receipt.agent_id,
                    receipt.protocol_version,
                    receipt.key_id,
                    receipt.signature,
                    receipt.agent_observed_at,
                    now,
                ),
            )
            if receipt.stage == "transport_starting":
                await connection.execute(
                    "UPDATE effect_dispatch_outbox SET "
                    "transport_started_at = COALESCE("
                    "transport_started_at, ?) WHERE dispatch_ref = ?",
                    (now, receipt.dispatch_ref),
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return {"receipt_id": receipt.receipt_id, "received_at": now}


# ── Observation, reconciliation, and late results ────────────────────


async def observe_response(
    *,
    run_id: str,
    effect_id: str,
    raw_response: bytes,
    artifact_store: ArtifactStore,
    outcome: str,
    data_class: DataClass = DataClass.INTERNAL,
    redactor: Callable[[bytes], bytes] | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Persist the protected raw response, then observe the outcome.

    Security classification and required redaction run before the raw
    artifact commit; prohibited content never persists. The raw
    artifact commits before any semantic parsing or grading.
    """
    if data_class is DataClass.PROHIBITED:
        raise EffectServiceError(
            "Prohibited persistence data never reaches the raw artifact"
        )
    filtered = redactor(raw_response) if redactor is not None else (
        raw_response
    )
    failpoint("effect.before_raw_persist")
    raw_digest = persist_protected_artifact(
        artifact_store,
        filtered,
        media_type="application/octet-stream",
        access_policy="foundation-raw-response",
        data_class=data_class,
        referenced_by=f"effect-{effect_id}",
    )
    failpoint("effect.after_raw_persist")

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_attempts SET raw_response_artifact_digest = ?, "
            "observed_outcome = ? WHERE effect_id = ?",
            (raw_digest, outcome, effect_id),
        )
        await connection.execute(
            "UPDATE effect_dispatch_outbox SET dispatch_state = "
            "'completed', journal_cursor = ? WHERE dispatch_ref = ? "
            "AND dispatch_state = 'claimed'",
            (journal_cursor, row["dispatch_ref"]),
        )

    record = await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="observed",
        payload_extra={
            "raw_response_artifact_digest": raw_digest,
            "outcome": outcome,
        },
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-observe-{effect_id}",
    )
    return {"record": record, "raw_response_artifact_digest": raw_digest}


async def observe_via_lookup(
    *,
    run_id: str,
    effect_id: str,
    lookup_evidence: str,
    outcome: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Prove one unknown outcome through an authenticated lookup."""

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_attempts SET observed_outcome = ? "
            "WHERE effect_id = ?",
            (outcome, effect_id),
        )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="observed",
        payload_extra={
            "lookup_evidence": lookup_evidence,
            "outcome": outcome,
        },
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-lookup-observe-{effect_id}",
    )


async def reconcile_effect(
    *,
    run_id: str,
    effect_id: str,
    usage: dict[str, int] | None,
    set_authoritative: bool = True,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Finish outcome and usage reconciliation for one observed attempt.

    Usage reconciles against this attempt's own original reservation.
    Missing usage consumes the pessimistic reservation; it never
    becomes a zero-cost cancellation. The unit of work sets the
    authoritative result exactly once when this attempt supplies the
    logical result.
    """
    attempt = await get_attempt(effect_id)
    await budget_service.reconcile(
        str(attempt["reservation_id"]),
        reconciliation_key=f"effect-reconcile-{effect_id}",
        actual_resources=usage,
        database_time=database_time,
    )

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_attempts SET usage = ?, reconciliation_reason "
            "= 'reconciled' WHERE effect_id = ?",
            (
                json.dumps(usage, sort_keys=True)
                if usage is not None
                else None,
                effect_id,
            ),
        )
        if set_authoritative:
            operation = await _load_operation(
                connection, str(row["effect_operation_id"]),
            )
            if operation["authoritative_result_effect_id"] is None:
                await connection.execute(
                    "UPDATE effect_operations SET "
                    "authoritative_result_effect_id = ? "
                    "WHERE effect_operation_id = ?",
                    (effect_id, row["effect_operation_id"]),
                )

    record = await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="reconciled",
        payload_extra={
            "usage": usage,
            "authoritative_result_effect_id": effect_id
            if set_authoritative
            else None,
        },
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-reconcile-{effect_id}",
    )
    return record


async def operator_reconcile_unknown(
    *,
    run_id: str,
    effect_id: str,
    operator_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Record one irrecoverable unknown outcome as an operator decision.

    The reservation applies its pessimistic charge; the unknown effect
    never appears as a zero-cost cancellation.
    """
    attempt = await get_attempt(effect_id)
    await budget_service.reconcile(
        str(attempt["reservation_id"]),
        reconciliation_key=f"effect-operator-reconcile-{effect_id}",
        actual_resources=None,
        database_time=database_time,
    )

    async def extra(
        connection: aiosqlite.Connection,
        row: dict[str, Any],
        journal_cursor: int,
        now: str,
    ) -> None:
        await connection.execute(
            "UPDATE effect_attempts SET reconciliation_reason = ?, "
            "terminal_reason = ? WHERE effect_id = ?",
            (f"operator:{operator_id}", reason, effect_id),
        )

    return await _journal_effect_transition(
        run_id=run_id,
        effect_id=effect_id,
        target_state="reconciled",
        payload_extra={
            "operator_id": operator_id,
            "reason": reason,
        },
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"effect-operator-reconcile-{effect_id}",
    )


# ── Retry paths ──────────────────────────────────────────────────────


async def approve_unsafe_retry(
    *,
    run_id: str,
    effect_operation_id: str,
    retry_of_effect_id: str,
    requested_by: str,
    approved_by: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Record the separated approval for one unsafe irreversible retry.

    The requester cannot approve the retry alone. The decision
    journals as one human control record before any new attempt or
    dispatch exists.
    """
    if requested_by == approved_by:
        raise RetryPolicyError(
            "The requester cannot approve an unsafe retry alone"
        )
    identity = await run_identity(run_id)
    approval_id = f"retry-approval-{uuid.uuid4()}"

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        await connection.execute(
            "INSERT INTO effect_retry_approvals ("
            "approval_id, effect_operation_id, retry_of_effect_id, "
            "requested_by, approved_by, reason, recorded_at, "
            "journal_cursor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id,
                effect_operation_id,
                retry_of_effect_id,
                requested_by,
                approved_by,
                reason,
                now,
                journal_cursor,
            ),
        )

    record = await journal.commit_operation(
        journal.JournalOperation(
            operation_type="human_control",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload={
                "control_id": approval_id,
                "operation": "approve_unsafe_retry",
                "actor_id": approved_by,
                "reason": reason,
                "effect_operation_id": effect_operation_id,
                "retry_of_effect_id": retry_of_effect_id,
                "requested_by": requested_by,
            },
            idempotency_token=approval_id,
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra,
    )
    return {"approval_id": approval_id, "record": record}


async def retry_effect(
    *,
    run_id: str,
    predecessor_effect_id: str,
    reservation_id: str,
    adapter_capabilities: AdapterCapabilities,
    requested_by: str,
    lookup_proves_non_acceptance: bool | None = None,
    approval_id: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Create one new transport retry attempt under the safety policy.

    A safe retry reuses the stable provider operation key. A
    conditional retry starts only after lookup proves non-acceptance.
    An unsafe retry stores the separated approval decision. Every
    retry allocates a new effect identifier, attempt number,
    reservation, dispatch reference, grant, and receipt chain, and
    never reopens the predecessor.
    """
    predecessor = await get_attempt(predecessor_effect_id)
    operation_id = str(predecessor["effect_operation_id"])
    safety = adapter_capabilities.retry_safety
    lookup_evidence: str | None = None
    if safety == "conditional":
        if not adapter_capabilities.provider_run_lookup:
            raise RetryPolicyError(
                "A conditional adapter performs lookup before a retry"
            )
        if lookup_proves_non_acceptance is not True:
            raise RetryPolicyError(
                "A conditional retry starts only after lookup proves "
                "no accepted request"
            )
        lookup_evidence = "lookup_proves_non_acceptance"
    elif safety == "unsafe":
        if approval_id is None:
            raise RetryPolicyError(
                "An unsafe irreversible retry requires the separated "
                "operator approval"
            )
        approval = await _find_retry_approval(approval_id)
        if approval is None or (
            str(approval["effect_operation_id"]) != operation_id
            or str(approval["retry_of_effect_id"]) != predecessor_effect_id
        ):
            raise RetryPolicyError(
                "The approval does not cover this retry"
            )
        if str(approval["approved_by"]) == requested_by:
            raise RetryPolicyError(
                "The requester cannot approve an unsafe retry alone"
            )

    operation_row = await get_operation(operation_id)
    return await create_effect_intent(
        run_id=run_id,
        activation_id=str(operation_row["activation_id"]),
        activation_attempt=int(operation_row["activation_attempt"]),
        kind=str(operation_row["kind"]),
        request_digest=str(predecessor["request_digest"]),
        idempotency_scope=str(operation_row["idempotency_scope"]),
        child_idempotency_key=f"retry-{uuid.uuid4()}",
        reservation_id=reservation_id,
        retry_safety=safety,
        provider_operation_key=predecessor["provider_operation_key"],
        retry_of_effect_id=predecessor_effect_id,
        approval_id=approval_id,
        lookup_evidence=lookup_evidence,
        task_fence=task_fence,
        database_time=database_time,
    )


async def _find_retry_approval(approval_id: str) -> dict[str, Any] | None:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM effect_retry_approvals WHERE approval_id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def record_late_result(
    *,
    run_id: str,
    effect_id: str,
    dispatch_ref: str,
    raw_response: bytes,
    artifact_store: ArtifactStore,
    outcome: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Route one late raw result to its original attempt.

    The protected artifact always persists. The late result reconciles
    its own attempt; it cannot replace the logical authoritative
    result or create another domain-state commit.
    """
    attempt = await get_attempt(effect_id)
    if str(attempt["dispatch_ref"]) != dispatch_ref:
        raise EffectServiceError(
            "The late result routes by effect and dispatch identifiers"
        )
    raw_digest = persist_protected_artifact(
        artifact_store,
        raw_response,
        media_type="application/octet-stream",
        access_policy="foundation-raw-response",
        referenced_by=f"late-result-{effect_id}",
    )
    operation = await get_operation(str(attempt["effect_operation_id"]))
    authoritative = operation["authoritative_result_effect_id"]
    superseded = authoritative is not None and (
        str(authoritative) != effect_id
    )
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            observation_id = await _record_protected_observation(
                connection,
                kind="late_result",
                now=now,
                run_id=run_id,
                effect_id=effect_id,
                dispatch_ref=dispatch_ref,
                artifact_digest=raw_digest,
                payload={"outcome": outcome, "superseded": superseded},
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result: dict[str, Any] = {
        "observation_id": observation_id,
        "raw_response_artifact_digest": raw_digest,
        "superseded": superseded,
        "proposal_accepted": False,
    }
    if str(attempt["state"]) == "outcome_unknown":
        await observe_via_lookup(
            run_id=run_id,
            effect_id=effect_id,
            lookup_evidence=f"late-result:{observation_id}",
            outcome=outcome,
            task_fence=task_fence,
            database_time=database_time,
        )
        record = await reconcile_effect(
            run_id=run_id,
            effect_id=effect_id,
            usage=None,
            set_authoritative=not superseded,
            task_fence=task_fence,
            database_time=database_time,
        )
        result["record"] = record
        result["proposal_accepted"] = not superseded
    return result


async def record_late_usage(
    *,
    effect_id: str,
    usage: dict[str, int],
    database_time: str | None = None,
) -> dict[str, Any]:
    """Reconcile late authoritative usage against the original reservation."""
    attempt = await get_attempt(effect_id)
    return await budget_service.reconcile(
        str(attempt["reservation_id"]),
        reconciliation_key=f"effect-late-usage-{effect_id}",
        actual_resources=usage,
        database_time=database_time,
    )


# ── The nested-call boundary ─────────────────────────────────────────


async def request_child_effect_grant(
    *,
    run_id: str,
    parent_grant_id: str,
    kind: str,
    request_digest: str,
    child_idempotency_key: str,
    reservation_id: str,
    retry_safety: str,
    target: str,
    claim_arguments: dict[str, Any],
    provider_operation_key: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Serve one agent request for a nested provider or tool grant.

    The request names the parent activation grant and a stable child
    idempotency key. The daemon denies the request before the accepted
    acknowledgement commits. Otherwise it creates the operation link,
    the intent, the approval, the ``dispatch_queued`` state, and the
    outbox row, then claims the exact row and returns the stored
    grant. Each nested call owns its own effect and dispatch
    identifiers.
    """
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM activation_grants WHERE grant_id = ?",
            (parent_grant_id,),
        )
        grant_row = await cursor.fetchone()
        if grant_row is None:
            raise EffectServiceError(
                f"Unknown parent grant: {parent_grant_id}"
            )
        activation = await _load_activation(
            connection,
            str(grant_row["activation_id"]),
            int(grant_row["attempt"]),
        )
    if str(activation["state"]) != "dispatched":
        raise EffectServiceError(
            "A child effect requires the committed accepted "
            "acknowledgement of its parent activation grant"
        )
    intent = await create_effect_intent(
        run_id=run_id,
        activation_id=str(grant_row["activation_id"]),
        activation_attempt=int(grant_row["attempt"]),
        kind=kind,
        request_digest=request_digest,
        idempotency_scope=f"activation-{grant_row['activation_id']}-"
        f"{grant_row['attempt']}",
        child_idempotency_key=child_idempotency_key,
        reservation_id=reservation_id,
        retry_safety=retry_safety,
        provider_operation_key=provider_operation_key,
        task_fence=task_fence,
        database_time=database_time,
    )
    if intent["duplicate"]:
        return intent
    await approve_effect(
        run_id=run_id,
        effect_id=intent["effect_id"],
        task_fence=task_fence,
        database_time=database_time,
    )
    await queue_effect_dispatch(
        run_id=run_id,
        effect_id=intent["effect_id"],
        target=target,
        task_fence=task_fence,
        database_time=database_time,
    )
    claim = await claim_effect_dispatch(
        run_id=run_id,
        effect_id=intent["effect_id"],
        expected_target=target,
        task_fence=task_fence,
        database_time=database_time,
        **claim_arguments,
    )
    return {**intent, "claim": claim}
