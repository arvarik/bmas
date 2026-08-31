"""Foundation evidence authority: decisions, policies, and invalidation.

An evidence decision records the verifier identity, capability,
implementation version, independence group, policy version, input
digests, verdict, confidence, times, and revocation state. Decision
history is immutable; only the revocation fields change, so the
complete prior history survives every invalidation.

A policy defines the required verifier combination. One verifier
never always makes a claim supported: independence counts distinct
independence groups, and two decisions from one group count once.

When evidence expires or becomes revoked, derived support decisions
become invalid. Dependent claims, goals, and actions receive durable
revalidation markers, and revocation propagates through every level
of a derived-claim chain.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import database as db
import runtime_journal as journal
from activation_service import run_identity

if TYPE_CHECKING:
    import aiosqlite

CLAIM_STATES = ("proposed", "supported", "unsupported", "invalidated")
VERDICTS = ("supported", "refuted", "inconclusive")


class EvidenceServiceError(ValueError):
    """One evidence authority rule failed closed."""


@dataclass(frozen=True)
class VerifierPolicy:
    """One registered verifier-combination policy."""

    policy_id: str
    version: str
    required_independence_groups: int
    required_capabilities: tuple[str, ...]
    require_human_approval: bool

    def __post_init__(self) -> None:
        if self.required_independence_groups < 1:
            raise EvidenceServiceError(
                "A policy requires at least one independence group"
            )


# The three registered verifier combinations.
REGISTERED_POLICIES: dict[str, VerifierPolicy] = {
    "deterministic-single": VerifierPolicy(
        policy_id="deterministic-single",
        version="1",
        required_independence_groups=1,
        required_capabilities=("deterministic",),
        require_human_approval=False,
    ),
    "independent-model-families": VerifierPolicy(
        policy_id="independent-model-families",
        version="1",
        required_independence_groups=2,
        required_capabilities=("model_judge",),
        require_human_approval=False,
    ),
    "verifier-plus-human": VerifierPolicy(
        policy_id="verifier-plus-human",
        version="1",
        required_independence_groups=1,
        required_capabilities=("model_judge",),
        require_human_approval=True,
    ),
}


async def get_claim(claim_id: str) -> dict[str, Any]:
    """Read one claim index row."""
    async with db._connect() as connection:  # noqa: SLF001
        row = await _load_claim(connection, claim_id)
        return row


async def _load_claim(
    connection: aiosqlite.Connection, claim_id: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM claim_index WHERE claim_id = ?", (claim_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise EvidenceServiceError(f"Unknown claim: {claim_id}")
    return dict(row)


async def get_decision(decision_id: str) -> dict[str, Any]:
    """Read one immutable evidence decision row."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM evidence_decisions WHERE decision_id = ?",
            (decision_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise EvidenceServiceError(f"Unknown decision: {decision_id}")
        return dict(row)


async def list_decisions(claim_id: str) -> list[dict[str, Any]]:
    """List the complete decision history of one claim."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM evidence_decisions WHERE claim_id = ? "
            "ORDER BY created_at, decision_id",
            (claim_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


def _claim_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Return the journal payload projection of one claim row."""
    return {
        "state": str(row["state"]),
        "supported": bool(row["supported"]),
        "revalidation_required": bool(row["revalidation_required"]),
        "derived_from": json.loads(str(row["derived_from"])),
    }


async def _journal_claim_update(
    *,
    run_id: str,
    claim_id: str,
    evidence_state: str,
    payload_extra: dict[str, Any] | None = None,
    extra_writes: Any,
    task_fence: str | None = None,
    database_time: str | None = None,
    idempotency_token: str | None = None,
) -> journal.JournalRecord:
    identity = await run_identity(run_id)
    return await journal.commit_operation(
        journal.JournalOperation(
            operation_type="evidence_update",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload={
                "claim_id": claim_id,
                "evidence_state": evidence_state,
                **(payload_extra or {}),
            },
            idempotency_token=idempotency_token
            or f"evidence-{claim_id}-{uuid.uuid4()}",
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra_writes,
    )


async def register_claim(
    *,
    run_id: str,
    claim_id: str,
    statement_digest: str,
    policy: VerifierPolicy,
    derived_from: tuple[str, ...] = (),
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Register one claim under one verifier policy."""
    identity = await run_identity(run_id)
    projection = {
        "state": "proposed",
        "supported": False,
        "revalidation_required": False,
        "derived_from": list(derived_from),
    }

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        for parent in derived_from:
            await _load_claim(connection, parent)
        await connection.execute(
            "INSERT INTO claim_index ("
            "claim_id, tenant_id, run_id, task_id, statement_digest, "
            "state, supported, derived_from, policy_id, policy_version, "
            "created_at, updated_at, journal_cursor) "
            "VALUES (?, ?, ?, ?, ?, 'proposed', 0, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                identity["tenant_id"],
                run_id,
                identity["task_id"],
                statement_digest,
                json.dumps(list(derived_from)),
                policy.policy_id,
                policy.version,
                now,
                now,
                journal_cursor,
            ),
        )

    return await _journal_claim_update(
        run_id=run_id,
        claim_id=claim_id,
        evidence_state="proposed",
        payload_extra={"claim_index": projection},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"claim-register-{claim_id}",
    )


def _decision_is_active(row: dict[str, Any], now: str) -> bool:
    if int(row["revoked"]):
        return False
    expires = row["expires_at"]
    return not (expires is not None and str(expires) <= now)


def evaluate_policy(
    decisions: list[dict[str, Any]],
    policy: VerifierPolicy,
    *,
    now: str,
) -> bool:
    """Evaluate one verifier policy against active decisions.

    Independence counts distinct independence groups; two decisions
    from one group count once, so one verifier can never satisfy a
    multi-group policy alone.
    """
    active = [
        row
        for row in decisions
        if str(row["verdict"]) == "supported"
        and _decision_is_active(row, now)
    ]
    groups = {str(row["independence_group"]) for row in active}
    if len(groups) < policy.required_independence_groups:
        return False
    capabilities = {str(row["verifier_capability"]) for row in active}
    if any(
        required not in capabilities
        for required in policy.required_capabilities
    ):
        return False
    return not (
        policy.require_human_approval
        and not any(int(row["human_approval"]) for row in active)
    )


async def _decisions_in_connection(
    connection: aiosqlite.Connection, claim_id: str,
) -> list[dict[str, Any]]:
    cursor = await connection.execute(
        "SELECT * FROM evidence_decisions WHERE claim_id = ?", (claim_id,),
    )
    return [dict(row) for row in await cursor.fetchall()]


def _policy_for_claim(claim: dict[str, Any]) -> VerifierPolicy:
    policy = REGISTERED_POLICIES.get(str(claim["policy_id"]))
    if policy is None:
        raise EvidenceServiceError(
            f"The claim names an unregistered policy: {claim['policy_id']!r}"
        )
    return policy


async def record_decision(
    *,
    run_id: str,
    claim_id: str,
    verifier_id: str,
    verifier_capability: str,
    verifier_version: str,
    independence_group: str,
    verdict: str,
    confidence: int,
    input_digests: tuple[str, ...] = (),
    human_approval: bool = False,
    expires_at: str | None = None,
    decision_id: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Record one immutable evidence decision and re-evaluate support."""
    if verdict not in VERDICTS:
        raise EvidenceServiceError(f"Unknown verdict: {verdict!r}")
    decision_id = decision_id or f"decision-{uuid.uuid4()}"
    identity = await run_identity(run_id)
    outcome: dict[str, Any] = {}

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        claim = await _load_claim(connection, claim_id)
        policy = _policy_for_claim(claim)
        await connection.execute(
            "INSERT INTO evidence_decisions ("
            "decision_id, claim_id, tenant_id, run_id, verifier_id, "
            "verifier_capability, verifier_version, independence_group, "
            "policy_version, input_digests, verdict, confidence, "
            "human_approval, created_at, expires_at, journal_cursor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                claim_id,
                identity["tenant_id"],
                run_id,
                verifier_id,
                verifier_capability,
                verifier_version,
                independence_group,
                policy.version,
                json.dumps(list(input_digests)),
                verdict,
                confidence,
                1 if human_approval else 0,
                now,
                expires_at,
                journal_cursor,
            ),
        )
        decisions = await _decisions_in_connection(connection, claim_id)
        supported = evaluate_policy(decisions, policy, now=now)
        state = "supported" if supported else "unsupported"
        await connection.execute(
            "UPDATE claim_index SET state = ?, supported = ?, "
            "updated_at = ?, journal_cursor = ? WHERE claim_id = ?",
            (state, 1 if supported else 0, now, journal_cursor, claim_id),
        )
        outcome["supported"] = supported
        outcome["state"] = state

    record = await _journal_claim_update(
        run_id=run_id,
        claim_id=claim_id,
        evidence_state="decision_recorded",
        payload_extra={"decision_id": decision_id},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"decision-{decision_id}",
    )
    # Journal the resulting claim projection as its own index update.
    claim = await get_claim(claim_id)
    await _journal_claim_update(
        run_id=run_id,
        claim_id=claim_id,
        evidence_state=str(claim["state"]),
        payload_extra={"claim_index": _claim_projection(claim)},
        extra_writes=None,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"claim-projection-{decision_id}",
    )
    return {"decision_id": decision_id, "record": record, **outcome}


async def _mark_revalidation(
    connection: aiosqlite.Connection,
    *,
    tenant_id: str,
    run_id: str,
    target_kind: str,
    target_id: str,
    source_decision_id: str | None,
    reason: str,
    now: str,
    journal_cursor: int,
) -> str:
    marker_id = f"marker-{uuid.uuid4()}"
    await connection.execute(
        "INSERT INTO revalidation_markers ("
        "marker_id, tenant_id, run_id, target_kind, target_id, "
        "source_decision_id, reason, created_at, journal_cursor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            marker_id,
            tenant_id,
            run_id,
            target_kind,
            target_id,
            source_decision_id,
            reason,
            now,
            journal_cursor,
        ),
    )
    return marker_id


async def _dependent_claim_ids(
    connection: aiosqlite.Connection, claim_id: str,
) -> list[str]:
    cursor = await connection.execute(
        "SELECT claim_id, derived_from FROM claim_index",
    )
    dependents = []
    for row in await cursor.fetchall():
        if claim_id in json.loads(str(row["derived_from"])):
            dependents.append(str(row["claim_id"]))
    return dependents


async def reevaluate_claim(
    *,
    run_id: str,
    claim_id: str,
    source_decision_id: str | None = None,
    reason: str = "evidence_changed",
    dependent_action_ids: tuple[str, ...] = (),
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Re-evaluate one claim and invalidate lost derived support.

    When support falls, the claim becomes ``invalidated`` with a
    revalidation marker, every derived claim invalidates through the
    complete chain, dependent goals roll back or receive markers, and
    named dependent actions receive markers. The complete prior
    decision history stays readable.
    """
    identity = await run_identity(run_id)
    changed: dict[str, Any] = {"invalidated": [], "markers": []}

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        claim = await _load_claim(connection, claim_id)
        policy = _policy_for_claim(claim)
        decisions = await _decisions_in_connection(connection, claim_id)
        supported = evaluate_policy(decisions, policy, now=now)
        was_supported = bool(claim["supported"])
        if supported == was_supported:
            return
        if supported:
            await connection.execute(
                "UPDATE claim_index SET state = 'supported', supported = 1, "
                "updated_at = ?, journal_cursor = ? WHERE claim_id = ?",
                (now, journal_cursor, claim_id),
            )
            return
        # Support fell: invalidate this claim and the derived chain.
        queue = [claim_id]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            await connection.execute(
                "UPDATE claim_index SET state = 'invalidated', "
                "supported = 0, revalidation_required = 1, "
                "revalidation_reason = ?, updated_at = ?, "
                "journal_cursor = ? WHERE claim_id = ?",
                (reason, now, journal_cursor, current),
            )
            marker = await _mark_revalidation(
                connection,
                tenant_id=identity["tenant_id"],
                run_id=run_id,
                target_kind="claim",
                target_id=current,
                source_decision_id=source_decision_id,
                reason=reason,
                now=now,
                journal_cursor=journal_cursor,
            )
            changed["invalidated"].append(current)
            changed["markers"].append(marker)
            queue.extend(
                await _dependent_claim_ids(connection, current),
            )
        for action_id in dependent_action_ids:
            marker = await _mark_revalidation(
                connection,
                tenant_id=identity["tenant_id"],
                run_id=run_id,
                target_kind="action",
                target_id=action_id,
                source_decision_id=source_decision_id,
                reason=reason,
                now=now,
                journal_cursor=journal_cursor,
            )
            changed["markers"].append(marker)

    await _journal_claim_update(
        run_id=run_id,
        claim_id=claim_id,
        evidence_state="reevaluated",
        payload_extra={"reason": reason},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
    )
    refreshed = await get_claim(claim_id)
    await _journal_claim_update(
        run_id=run_id,
        claim_id=claim_id,
        evidence_state=str(refreshed["state"]),
        payload_extra={"claim_index": _claim_projection(refreshed)},
        extra_writes=None,
        task_fence=task_fence,
        database_time=database_time,
    )
    for invalidated_claim in changed["invalidated"]:
        claim = await get_claim(invalidated_claim)
        await _journal_claim_update(
            run_id=run_id,
            claim_id=invalidated_claim,
            evidence_state="invalidated",
            payload_extra={"claim_index": _claim_projection(claim)},
            extra_writes=None,
            task_fence=task_fence,
            database_time=database_time,
        )
    # Dependent completed goals roll back; other dependents get markers.
    import goal_service

    for invalidated_claim in changed["invalidated"]:
        rollbacks = await goal_service.rollback_goals_for_claim(
            run_id=run_id,
            claim_id=invalidated_claim,
            reason=reason,
            source_decision_id=source_decision_id,
            task_fence=task_fence,
            database_time=database_time,
        )
        changed.setdefault("goal_rollbacks", []).extend(rollbacks)
    return changed


async def revoke_decision(
    *,
    run_id: str,
    decision_id: str,
    reason: str,
    dependent_action_ids: tuple[str, ...] = (),
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Revoke one decision and invalidate derived support.

    Only the revocation fields change; the decision history stays
    immutable and complete.
    """
    decision = await get_decision(decision_id)
    claim_id = str(decision["claim_id"])

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        cursor = await connection.execute(
            "UPDATE evidence_decisions SET revoked = 1, revoked_at = ?, "
            "revocation_reason = ? WHERE decision_id = ? AND revoked = 0",
            (now, reason, decision_id),
        )
        if cursor.rowcount != 1:
            raise EvidenceServiceError(
                "The decision is unknown or already revoked"
            )

    await _journal_claim_update(
        run_id=run_id,
        claim_id=claim_id,
        evidence_state="decision_revoked",
        payload_extra={"decision_id": decision_id, "reason": reason},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"decision-revoke-{decision_id}",
    )
    return await reevaluate_claim(
        run_id=run_id,
        claim_id=claim_id,
        source_decision_id=decision_id,
        reason=f"revoked:{reason}",
        dependent_action_ids=dependent_action_ids,
        task_fence=task_fence,
        database_time=database_time,
    )


async def list_revalidation_markers(
    *, target_kind: str | None = None,
) -> list[dict[str, Any]]:
    """List durable revalidation markers, optionally by target kind."""
    async with db._connect() as connection:  # noqa: SLF001
        if target_kind is None:
            cursor = await connection.execute(
                "SELECT * FROM revalidation_markers ORDER BY marker_id",
            )
        else:
            cursor = await connection.execute(
                "SELECT * FROM revalidation_markers WHERE target_kind = ? "
                "ORDER BY marker_id",
                (target_kind,),
            )
        return [dict(row) for row in await cursor.fetchall()]
