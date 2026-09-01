"""Foundation shared typed indexes over opaque runtime payloads.

State splits into two layers. The runtime payload stays opaque to the
shared host: the runtime owns its schema and scheduling meaning, and
the host stores exact bytes with one content digest and never parses
the payload semantics. The shared typed indexes hold host-defined
records for claims and evidence, goals, budget and resource use,
assets and artifacts, activations and effects, and trace events and
controls.

The runtime proposes index updates through the unit of work. The host
validates only the shared index contracts. Every index is a read
projection of journal transactions, never a second authority: replay
from the journal rebuilds each index, and no index update can carry
runtime-owned state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import database as db
import runtime_journal as journal
from core.digest_profile import digest_bytes

RUNTIME_PAYLOAD_DIGEST_DOMAIN = "runtime-payload"

TRACE_ENVELOPE_SCHEMA_VERSION = "1"

# The shared index kinds the host defines.
INDEX_KINDS = (
    "claims_evidence",
    "goals",
    "budget",
    "assets_artifacts",
    "activations_effects",
    "traces_controls",
)

# Runtime-owned concepts that no shared index update can carry. The
# runtime payload and its scheduling meaning stay with the runtime.
RUNTIME_OWNED_FIELDS = frozenset(
    {"runtime_payload", "runtime_state", "scheduling", "payload_semantics"},
)

# The host contract for each proposed index update kind.
INDEX_UPDATE_CONTRACTS: dict[str, tuple[str, ...]] = {
    "claims_evidence": ("claim_id", "evidence_state"),
    "goals": ("goal_id", "goal_state"),
    "budget": ("reservation_id", "consumed_usd_millionths"),
    "assets_artifacts": ("asset_id", "content_digest"),
    "activations_effects": ("activation_id", "activation_state"),
    "traces_controls": ("event_type", "payload_schema"),
}


class IndexContractError(ValueError):
    """One shared index contract failed closed."""


@dataclass(frozen=True)
class OpaqueRuntimePayload:
    """One runtime payload the host never interprets.

    The host sees exact bytes, one content digest, and one byte
    count. It never parses the payload, so runtime semantics cannot
    leak into host decisions.
    """

    payload_text: str

    def digest(self) -> str:
        return digest_bytes(
            RUNTIME_PAYLOAD_DIGEST_DOMAIN,
            self.payload_text.encode("utf-8"),
        )

    def byte_size(self) -> int:
        return len(self.payload_text.encode("utf-8"))


def validate_index_update(kind: str, record: dict[str, Any]) -> None:
    """Validate one proposed index update against the host contract.

    The host validates only the shared contract: the kind must be a
    registered index kind, the required fields must exist, and no
    runtime-owned field can ride along.
    """
    contract = INDEX_UPDATE_CONTRACTS.get(kind)
    if contract is None:
        raise IndexContractError(f"Unknown shared index kind: {kind!r}")
    missing = [name for name in contract if name not in record]
    if missing:
        raise IndexContractError(
            f"The {kind} index update misses {missing}"
        )
    owned = sorted(RUNTIME_OWNED_FIELDS & set(record))
    if owned:
        raise IndexContractError(
            "A shared index never carries runtime-owned state: "
            f"{owned}"
        )


# ── The trace event envelope ─────────────────────────────────────────


@dataclass(frozen=True)
class TraceEnvelope:
    """One shared trace event envelope.

    The trace service builds these read projections from journal
    transactions. The projection is not a second append-only
    authority; replay rebuilds it completely.
    """

    envelope_id: str
    schema_version: str
    journal_cursor: int
    run_sequence: int
    run_id: str
    task_id: str
    runtime_id: str
    runtime_contract_version: str
    activation_id: str | None
    activation_attempt: int | None
    effect_id: str | None
    event_type: str
    payload_schema: dict[str, str]
    correlation_id: str | None
    causation_id: str | None
    producer: str
    authority_type: str
    data_classification: str
    redaction_policy_version: str
    protected_artifact_refs: tuple[str, ...]
    trusted_timestamp: str


def envelope_from_journal_record(
    record: journal.JournalRecord,
) -> TraceEnvelope:
    """Build one trace envelope from one committed journal record."""
    payload = record.payload
    artifact_refs = []
    for name in (
        "raw_response_artifact_digest",
        "grant_artifact_digest",
        "proposal_digest",
        "execution_envelope_digest",
    ):
        value = payload.get(name)
        if value:
            artifact_refs.append(str(value))
    dispatch_row = payload.get("dispatch_row")
    if isinstance(dispatch_row, dict):
        nested = dispatch_row.get("grant_artifact_digest")
        if nested:
            artifact_refs.append(str(nested))
    return TraceEnvelope(
        envelope_id=f"trace-{record.transaction_id}",
        schema_version=TRACE_ENVELOPE_SCHEMA_VERSION,
        journal_cursor=record.journal_cursor,
        run_sequence=record.run_sequence,
        run_id=record.run_id,
        task_id=record.task_id,
        runtime_id=record.runtime_id,
        runtime_contract_version=record.runtime_contract_version,
        activation_id=payload.get("activation_id"),
        activation_attempt=payload.get("activation_attempt"),
        effect_id=payload.get("effect_id"),
        event_type=record.operation_type,
        payload_schema=dict(record.payload_schema_versions),
        correlation_id=record.correlation_id,
        causation_id=record.causation_id,
        producer=record.producer,
        authority_type=record.authority_type,
        data_classification=record.data_classification,
        redaction_policy_version=record.redaction_policy_version,
        protected_artifact_refs=tuple(artifact_refs),
        trusted_timestamp=record.recorded_at,
    )


async def trace_projection(run_id: str) -> list[TraceEnvelope]:
    """Build the trace read projection of one run from the journal."""
    records = await journal.read_journal(run_id=run_id)
    return [envelope_from_journal_record(record) for record in records]


# ── Shared index reads and journal rebuilds ──────────────────────────


async def read_shared_indexes(run_id: str) -> dict[str, Any]:
    """Read every shared typed index projection of one run."""
    indexes: dict[str, Any] = {
        "claims_evidence": {"claims": {}, "decisions": {}},
        "goals": {},
        "budget": {},
        "activations_effects": {"activations": {}, "effects": {}},
    }
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM claim_index WHERE run_id = ?", (run_id,),
        )
        for row in await cursor.fetchall():
            indexes["claims_evidence"]["claims"][str(row["claim_id"])] = {
                "state": str(row["state"]),
                "supported": bool(row["supported"]),
                "revalidation_required": bool(row["revalidation_required"]),
                "derived_from": json.loads(str(row["derived_from"])),
            }
        cursor = await connection.execute(
            "SELECT decision_id, claim_id, revoked FROM evidence_decisions "
            "WHERE run_id = ?",
            (run_id,),
        )
        for row in await cursor.fetchall():
            indexes["claims_evidence"]["decisions"][
                str(row["decision_id"])
            ] = {
                "claim_id": str(row["claim_id"]),
                "revoked": bool(row["revoked"]),
            }
        cursor = await connection.execute(
            "SELECT * FROM goal_index WHERE run_id = ?", (run_id,),
        )
        for row in await cursor.fetchall():
            indexes["goals"][str(row["goal_id"])] = {
                "state": str(row["state"]),
                "version": int(row["version"]),
                "alias_of": row["alias_of"],
                "revalidation_required": bool(row["revalidation_required"]),
            }
        cursor = await connection.execute(
            "SELECT reservation_id, state, consumed_amount_nanos "
            "FROM budget_reservations WHERE run_id = ?",
            (run_id,),
        )
        for row in await cursor.fetchall():
            indexes["budget"][str(row["reservation_id"])] = {
                "state": str(row["state"]),
                "consumed_amount_nanos": int(row["consumed_amount_nanos"]),
            }
        cursor = await connection.execute(
            "SELECT activation_id, attempt, state FROM activations "
            "WHERE run_id = ?",
            (run_id,),
        )
        for row in await cursor.fetchall():
            indexes["activations_effects"]["activations"][
                f"{row['activation_id']}#{row['attempt']}"
            ] = str(row["state"])
        cursor = await connection.execute(
            "SELECT effect_id, state FROM effect_attempts WHERE run_id = ?",
            (run_id,),
        )
        for row in await cursor.fetchall():
            indexes["activations_effects"]["effects"][
                str(row["effect_id"])
            ] = str(row["state"])
    return indexes


async def rebuild_claim_and_goal_indexes(run_id: str) -> dict[str, Any]:
    """Rebuild the claim and goal index projections from the journal.

    The durable index rows must equal this rebuild; an index that
    diverged would be a second authority, and that never happens.
    """
    state = journal.empty_projection_state()
    for record in await journal.read_journal(run_id=run_id):
        journal.apply_record_to_state(state, record)
    return {
        "claims": state["claim_support"],
        "goals": state["goal_index"],
    }


async def assert_indexes_match_journal(run_id: str) -> None:
    """Fail closed when a durable index diverges from journal replay."""
    rebuilt = await rebuild_claim_and_goal_indexes(run_id)
    live = await read_shared_indexes(run_id)
    if rebuilt["claims"] != live["claims_evidence"]["claims"]:
        raise IndexContractError(
            "The claim index diverged from journal replay"
        )
    if rebuilt["goals"] != live["goals"]:
        raise IndexContractError(
            "The goal index diverged from journal replay"
        )
