"""Foundation Stage 0D: the immutable runtime journal and unit of work.

The runtime journal is the one append-only replay authority for new
contract transactions. Every typed operation writes one journal
transaction together with its changed projections and its outbox
obligations, inside one ``BEGIN IMMEDIATE`` SQLite transaction. Mutable
delivery state lives in separate tables and never touches an authority
row.

Each run owns one digest chain. The genesis record uses a declared
domain constant as its previous digest, every later record binds the
previous record digest, and the run row stores the chain head, so tail
truncation is detectable. Snapshots are verified replay accelerators:
replay verifies a snapshot digest and its journal head before use, and
falls back to full replay from cursor zero on any mismatch.

The journal connection commits with ``synchronous=FULL`` (and macOS
``fullfsync``), because an authoritative journal must not lose an
acknowledged append on power loss.
"""
from __future__ import annotations

import json
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import database as db
from core.digest_profile import digest_hex
from core.failpoints import failpoint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import aiosqlite

    ExtraWrites = Callable[
        ["aiosqlite.Connection", int, str], Awaitable[None],
    ]

RUNTIME_JOURNAL_SCHEMA_VERSION = "1"

# The declared genesis constant: the previous digest of the first
# record in every chain epoch.
GENESIS_PREVIOUS_DIGEST = digest_hex("journal-chain-genesis", "genesis")

PAYLOAD_DIGEST_DOMAIN = "journal-payload"
TRANSACTION_DIGEST_DOMAIN = "journal-transaction"
IDEMPOTENCY_DIGEST_DOMAIN = "journal-idempotency"
PROJECTION_DIGEST_DOMAIN = "projection-state"
SNAPSHOT_DIGEST_DOMAIN = "journal-snapshot"

OPERATION_TYPES = (
    "admission_identity",
    "activation_transition",
    "effect_transition",
    "proposal_decision",
    "terminal_outcome",
    "post_terminal_invalidation",
    "human_control",
    "evidence_update",
    "goal_update",
    "budget_reconciliation",
)

CHAIN_COMPACTION_OPERATION = "chain_compaction"

TERMINAL_STATE_FOR_CLASS = {
    "success": "completed",
    "substantive_failure": "failed",
    "infrastructure_failure": "failed",
    "cancellation": "cancelled",
}

# Every named crash site inside the unit of work, in commit order.
JOURNAL_FAILPOINTS = (
    "journal.before_transaction",
    "journal.after_idempotency_check",
    "journal.before_journal_insert",
    "journal.after_journal_insert",
    "journal.before_projection_write",
    "journal.after_projection_write",
    "journal.before_outbox_write",
    "journal.after_outbox_write",
    "journal.before_resource_write",
    "journal.after_resource_write",
    "journal.before_commit",
    "journal.after_commit",
)


class JournalError(ValueError):
    """A unit-of-work rule was violated."""


class JournalConflictError(JournalError):
    """The idempotency key was reused with a different payload."""


class JournalFenceError(JournalError):
    """The task fence did not match the live run-control row."""


class JournalIntegrityError(JournalError):
    """The journal chain failed verification. Replay must stop."""


class SnapshotVerificationError(JournalError):
    """The snapshot failed verification and cannot accelerate replay."""


@asynccontextmanager
async def _journal_connect() -> AsyncIterator[aiosqlite.Connection]:
    """Open one journal connection with full durability settings."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("PRAGMA synchronous=FULL")
        if sys.platform == "darwin":
            await connection.execute("PRAGMA fullfsync=ON")
            await connection.execute("PRAGMA checkpoint_fullfsync=ON")
        yield connection


@dataclass(frozen=True)
class JournalOperation:
    """One typed unit-of-work operation before commit."""

    operation_type: str
    task_id: str
    run_id: str
    runtime_id: str
    runtime_contract_version: str
    payload: dict[str, Any]
    idempotency_token: str
    producer: str = "daemon"
    authority_type: str = "host"
    payload_schema_versions: dict[str, str] = field(
        default_factory=lambda: {"payload_schema_version": "1"},
    )
    correlation_id: str | None = None
    causation_id: str | None = None
    data_classification: str = "internal"
    redaction_policy_version: str = "1"
    task_fence: str | None = None
    expected_projection_version: int | None = None
    tenant_id: str = "tenant-default"
    outbox_targets: tuple[str, ...] = ("projection",)
    activation_dispatch_id: str | None = None
    effect_dispatch_id: str | None = None


@dataclass(frozen=True)
class JournalRecord:
    """One committed journal envelope."""

    journal_cursor: int
    transaction_id: str
    idempotency_key: str
    task_id: str
    run_id: str
    chain_epoch: int
    run_sequence: int
    previous_digest: str
    payload_digest: str
    transaction_digest: str
    runtime_id: str
    runtime_contract_version: str
    payload_schema_versions: dict[str, str]
    correlation_id: str | None
    causation_id: str | None
    producer: str
    authority_type: str
    recorded_at: str
    data_classification: str
    redaction_policy_version: str
    operation_type: str
    payload: dict[str, Any]


def _record_from_row(row: Any) -> JournalRecord:
    return JournalRecord(
        journal_cursor=int(row["journal_cursor"]),
        transaction_id=str(row["transaction_id"]),
        idempotency_key=str(row["idempotency_key"]),
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        chain_epoch=int(row["chain_epoch"]),
        run_sequence=int(row["run_sequence"]),
        previous_digest=str(row["previous_digest"]),
        payload_digest=str(row["payload_digest"]),
        transaction_digest=str(row["transaction_digest"]),
        runtime_id=str(row["runtime_id"]),
        runtime_contract_version=str(row["runtime_contract_version"]),
        payload_schema_versions=json.loads(row["payload_schema_versions"]),
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        producer=str(row["producer"]),
        authority_type=str(row["authority_type"]),
        recorded_at=str(row["recorded_at"]),
        data_classification=str(row["data_classification"]),
        redaction_policy_version=str(row["redaction_policy_version"]),
        operation_type=str(row["operation_type"]),
        payload=json.loads(row["payload"]),
    )


def _envelope_digest(
    *,
    transaction_id: str,
    task_id: str,
    run_id: str,
    chain_epoch: int,
    run_sequence: int,
    previous_digest: str,
    payload_digest: str,
    runtime_id: str,
    runtime_contract_version: str,
    payload_schema_versions: dict[str, str],
    correlation_id: str | None,
    causation_id: str | None,
    producer: str,
    authority_type: str,
    recorded_at: str,
    data_classification: str,
    redaction_policy_version: str,
    operation_type: str,
    payload: dict[str, Any],
) -> str:
    """Digest the canonical envelope and its payload.

    The digest binds the task identifier, run identifier, sequence,
    and runtime pair.
    """
    return digest_hex(
        TRANSACTION_DIGEST_DOMAIN,
        {
            "transaction_id": transaction_id,
            "task_id": task_id,
            "run_id": run_id,
            "chain_epoch": chain_epoch,
            "run_sequence": run_sequence,
            "previous_digest": previous_digest,
            "payload_digest": payload_digest,
            "runtime_id": runtime_id,
            "runtime_contract_version": runtime_contract_version,
            "payload_schema_versions": payload_schema_versions,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "producer": producer,
            "authority_type": authority_type,
            "recorded_at": recorded_at,
            "data_classification": data_classification,
            "redaction_policy_version": redaction_policy_version,
            "operation_type": operation_type,
            "payload": payload,
        },
    )


def record_transaction_digest(record: JournalRecord) -> str:
    """Recompute the transaction digest of one stored record."""
    return _envelope_digest(
        transaction_id=record.transaction_id,
        task_id=record.task_id,
        run_id=record.run_id,
        chain_epoch=record.chain_epoch,
        run_sequence=record.run_sequence,
        previous_digest=record.previous_digest,
        payload_digest=record.payload_digest,
        runtime_id=record.runtime_id,
        runtime_contract_version=record.runtime_contract_version,
        payload_schema_versions=record.payload_schema_versions,
        correlation_id=record.correlation_id,
        causation_id=record.causation_id,
        producer=record.producer,
        authority_type=record.authority_type,
        recorded_at=record.recorded_at,
        data_classification=record.data_classification,
        redaction_policy_version=record.redaction_policy_version,
        operation_type=record.operation_type,
        payload=record.payload,
    )


_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "admission_identity": (
        "admission_id",
        "version_set",
        "specification_digest",
        "capability_document_digest",
        "admission_digest",
    ),
    "activation_transition": ("activation_id", "activation_state"),
    "effect_transition": ("effect_id", "effect_state"),
    "proposal_decision": (
        "decision",
        "proposal_digest",
        "execution_envelope_digest",
        "projection_changes",
        "checkpoint_digest",
        "circuit_state",
        "circuit_decision",
        "activation_id",
        "activation_state",
        "budget",
        "trace_event",
    ),
    "terminal_outcome": (
        "outcome_id",
        "common_class",
        "reason_code",
        "mapping_version",
        "outcome_digest",
    ),
    "post_terminal_invalidation": (
        "invalidation_id",
        "outcome_id",
        "outcome_digest",
        "targets",
        "reason_code",
        "authority_id",
    ),
    "human_control": ("control_id", "operation", "actor_id", "reason"),
    "evidence_update": ("claim_id", "evidence_state"),
    "goal_update": ("goal_id", "goal_state"),
    "budget_reconciliation": ("reservation_id", "consumed_usd_millionths"),
}


def _validate_operation(operation: JournalOperation) -> None:
    if operation.operation_type not in OPERATION_TYPES:
        raise JournalError(
            f"Unknown operation type: {operation.operation_type!r}"
        )
    for name in ("task_id", "run_id", "runtime_id",
                 "runtime_contract_version", "idempotency_token", "producer",
                 "authority_type"):
        if not getattr(operation, name):
            raise JournalError(f"The operation requires {name}")
    missing = [
        required
        for required in _REQUIRED_PAYLOAD_FIELDS[operation.operation_type]
        if required not in operation.payload
    ]
    if missing:
        raise JournalError(
            f"The {operation.operation_type} payload misses {missing}"
        )
    if operation.operation_type == "proposal_decision" and (
        operation.payload["decision"] not in ("accepted", "rejected")
    ):
        raise JournalError("A proposal decision is accepted or rejected")


def _idempotency_key(operation: JournalOperation) -> str:
    return digest_hex(
        IDEMPOTENCY_DIGEST_DOMAIN,
        {
            "task_id": operation.task_id,
            "run_id": operation.run_id,
            "operation_type": operation.operation_type,
            "token": operation.idempotency_token,
        },
    )


async def _validate_task_fence(
    connection: aiosqlite.Connection, operation: JournalOperation,
) -> None:
    if operation.task_fence is None:
        return
    cursor = await connection.execute(
        "SELECT task_fence FROM run_controls WHERE run_id = ?",
        (operation.run_id,),
    )
    row = await cursor.fetchone()
    if row is None or str(row["task_fence"]) != operation.task_fence:
        raise JournalFenceError(
            "The task fence does not match the live run-control row"
        )


async def commit_operation(
    operation: JournalOperation,
    *,
    database_time: str | None = None,
    extra_writes: ExtraWrites | None = None,
) -> JournalRecord:
    """Commit one typed operation as one journal transaction.

    One transaction writes the journal record, the changed projections,
    and the outbox obligations together. ``extra_writes`` joins the
    same transaction, so run admission creates its budget and initial
    reservation atomically with the journal genesis. A crash at any
    point before the commit leaves no record; the commit itself is
    atomic.
    """
    _validate_operation(operation)
    failpoint("journal.before_transaction")
    idempotency_key = _idempotency_key(operation)
    payload_digest = digest_hex(PAYLOAD_DIGEST_DOMAIN, operation.payload)

    async with _journal_connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            existing_cursor = await connection.execute(
                "SELECT * FROM runtime_journal WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            existing = await existing_cursor.fetchone()
            if existing is not None:
                await connection.commit()
                if str(existing["payload_digest"]) != payload_digest:
                    raise JournalConflictError(
                        "The idempotency key was reused with a different "
                        "payload"
                    )
                return _record_from_row(existing)
            failpoint("journal.after_idempotency_check")

            now = await db._control_now(connection, database_time)  # noqa: SLF001
            await _validate_task_fence(connection, operation)

            run_cursor = await connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (operation.run_id,),
            )
            run_row = await run_cursor.fetchone()
            if run_row is None:
                if operation.operation_type != "admission_identity":
                    raise JournalError(
                        "The first journal record of a run is its admission"
                    )
                chain_epoch = 1
                run_sequence = 0
                previous_digest = GENESIS_PREVIOUS_DIGEST
                projection_version = 0
            else:
                if str(run_row["task_id"]) != operation.task_id:
                    raise JournalError(
                        "The run does not belong to the stated task"
                    )
                if operation.operation_type == "admission_identity":
                    raise JournalError(
                        "A run holds exactly one immutable admission"
                    )
                if str(run_row["state"]) in (
                    "completed", "failed", "cancelled",
                ) and operation.operation_type != "post_terminal_invalidation":
                    if operation.operation_type == "terminal_outcome":
                        raise JournalError(
                            "A run commits exactly one host-created "
                            "terminal outcome"
                        )
                    raise JournalError(
                        "A terminal run accepts post-terminal invalidation "
                        "only"
                    )
                chain_epoch = int(run_row["chain_epoch"])
                run_sequence = int(run_row["next_run_sequence"])
                previous_digest = str(run_row["chain_head_digest"])
                projection_version = int(run_row["projection_version"])
                expected = operation.expected_projection_version
                if expected is not None and expected != projection_version:
                    raise JournalError(
                        "The optimistic projection version does not match"
                    )

            if operation.operation_type == "terminal_outcome":
                await _validate_single_terminal_outcome(
                    connection, operation.run_id,
                )
            if operation.operation_type == "post_terminal_invalidation":
                await _validate_invalidation_target(
                    connection, operation, run_row,
                )

            transaction_id = f"journal-txn-{uuid.uuid4()}"
            schema_versions_json = json.dumps(
                operation.payload_schema_versions, sort_keys=True,
            )
            transaction_digest = _envelope_digest(
                transaction_id=transaction_id,
                task_id=operation.task_id,
                run_id=operation.run_id,
                chain_epoch=chain_epoch,
                run_sequence=run_sequence,
                previous_digest=previous_digest,
                payload_digest=payload_digest,
                runtime_id=operation.runtime_id,
                runtime_contract_version=operation.runtime_contract_version,
                payload_schema_versions=operation.payload_schema_versions,
                correlation_id=operation.correlation_id,
                causation_id=operation.causation_id,
                producer=operation.producer,
                authority_type=operation.authority_type,
                recorded_at=now,
                data_classification=operation.data_classification,
                redaction_policy_version=operation.redaction_policy_version,
                operation_type=operation.operation_type,
                payload=operation.payload,
            )

            failpoint("journal.before_journal_insert")
            insert_cursor = await connection.execute(
                "INSERT INTO runtime_journal ("
                "transaction_id, idempotency_key, task_id, run_id, "
                "chain_epoch, run_sequence, previous_digest, payload_digest, "
                "transaction_digest, runtime_id, runtime_contract_version, "
                "payload_schema_versions, correlation_id, causation_id, "
                "producer, authority_type, recorded_at, data_classification, "
                "redaction_policy_version, operation_type, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    transaction_id,
                    idempotency_key,
                    operation.task_id,
                    operation.run_id,
                    chain_epoch,
                    run_sequence,
                    previous_digest,
                    payload_digest,
                    transaction_digest,
                    operation.runtime_id,
                    operation.runtime_contract_version,
                    schema_versions_json,
                    operation.correlation_id,
                    operation.causation_id,
                    operation.producer,
                    operation.authority_type,
                    now,
                    operation.data_classification,
                    operation.redaction_policy_version,
                    operation.operation_type,
                    json.dumps(operation.payload, sort_keys=True),
                ),
            )
            journal_cursor = insert_cursor.lastrowid
            if journal_cursor is None:
                raise JournalError("The journal insert allocated no cursor")
            failpoint("journal.after_journal_insert")

            failpoint("journal.before_projection_write")
            await _apply_durable_projection(
                connection,
                operation,
                run_row=run_row,
                journal_cursor=journal_cursor,
                chain_epoch=chain_epoch,
                run_sequence=run_sequence,
                transaction_digest=transaction_digest,
                projection_version=projection_version,
            )
            failpoint("journal.after_projection_write")

            failpoint("journal.before_outbox_write")
            for target in operation.outbox_targets:
                await connection.execute(
                    "INSERT INTO journal_outbox "
                    "(journal_cursor, target, created_at) VALUES (?, ?, ?)",
                    (journal_cursor, target, now),
                )
            if operation.activation_dispatch_id is not None:
                await connection.execute(
                    "INSERT INTO activation_dispatch_outbox "
                    "(journal_cursor, run_id, activation_id) "
                    "VALUES (?, ?, ?)",
                    (
                        journal_cursor,
                        operation.run_id,
                        operation.activation_dispatch_id,
                    ),
                )
            if operation.effect_dispatch_id is not None:
                await connection.execute(
                    "INSERT INTO effect_dispatch_outbox "
                    "(journal_cursor, run_id, effect_id) VALUES (?, ?, ?)",
                    (
                        journal_cursor,
                        operation.run_id,
                        operation.effect_dispatch_id,
                    ),
                )
            failpoint("journal.after_outbox_write")

            failpoint("journal.before_resource_write")
            if extra_writes is not None:
                await extra_writes(connection, journal_cursor, now)
            failpoint("journal.after_resource_write")

            failpoint("journal.before_commit")
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        stored_cursor = await connection.execute(
            "SELECT * FROM runtime_journal WHERE journal_cursor = ?",
            (journal_cursor,),
        )
        stored = await stored_cursor.fetchone()
    failpoint("journal.after_commit")
    if stored is None:
        raise JournalError("The committed journal record is unreadable")
    return _record_from_row(stored)


async def _validate_single_terminal_outcome(
    connection: aiosqlite.Connection, run_id: str,
) -> None:
    cursor = await connection.execute(
        "SELECT COUNT(*) FROM runtime_journal "
        "WHERE run_id = ? AND operation_type = 'terminal_outcome'",
        (run_id,),
    )
    row = await cursor.fetchone()
    if row is not None and int(row[0]) > 0:
        raise JournalError(
            "A run commits exactly one host-created terminal outcome"
        )


async def _validate_invalidation_target(
    connection: aiosqlite.Connection,
    operation: JournalOperation,
    run_row: Any,
) -> None:
    if run_row is None or str(run_row["state"]) not in (
        "completed", "failed", "cancelled",
    ):
        raise JournalError(
            "Post-terminal invalidation requires a terminal run"
        )
    cursor = await connection.execute(
        "SELECT payload FROM runtime_journal "
        "WHERE run_id = ? AND operation_type = 'terminal_outcome'",
        (operation.run_id,),
    )
    rows = list(await cursor.fetchall())
    if len(rows) != 1:
        raise JournalError("The run holds no single terminal outcome")
    outcome = json.loads(rows[0]["payload"])
    if (
        outcome["outcome_id"] != operation.payload["outcome_id"]
        or outcome["outcome_digest"] != operation.payload["outcome_digest"]
    ):
        raise JournalError(
            "The invalidation names the wrong outcome or digest"
        )


async def _apply_durable_projection(
    connection: aiosqlite.Connection,
    operation: JournalOperation,
    *,
    run_row: Any,
    journal_cursor: int,
    chain_epoch: int,
    run_sequence: int,
    transaction_digest: str,
    projection_version: int,
) -> None:
    """Apply the durable projection changes of one operation.

    Every projection row records its authoritative journal cursor. The
    journal row itself never changes here; immutability triggers
    reject any such attempt.
    """
    payload = operation.payload
    if run_row is None:
        await connection.execute(
            "INSERT INTO runs ("
            "run_id, task_id, tenant_id, runtime_id, "
            "runtime_contract_version, state, projection_version, "
            "chain_epoch, next_run_sequence, chain_head_digest, "
            "journal_cursor) VALUES (?, ?, ?, ?, ?, 'admitted', 1, ?, ?, "
            "?, ?)",
            (
                operation.run_id,
                operation.task_id,
                operation.tenant_id,
                operation.runtime_id,
                operation.runtime_contract_version,
                chain_epoch,
                run_sequence + 1,
                transaction_digest,
                journal_cursor,
            ),
        )
        await connection.execute(
            "INSERT INTO runtime_admissions ("
            "admission_id, task_id, run_id, runtime_id, "
            "runtime_contract_version, version_set, specification_digest, "
            "capability_document_digest, policy_set_digest, "
            "asset_manifest_digest, admission_digest, asset_manifest_id, "
            "prompt_profile_digest, role_profile_digest, seed_policy, "
            "requested_seed, qualification_ids, run_budget_id, "
            "initial_reservation_id, journal_cursor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (
                payload["admission_id"],
                operation.task_id,
                operation.run_id,
                operation.runtime_id,
                operation.runtime_contract_version,
                json.dumps(payload["version_set"], sort_keys=True),
                payload["specification_digest"],
                payload["capability_document_digest"],
                payload.get("policy_set_digest"),
                payload.get("asset_manifest_digest"),
                payload["admission_digest"],
                payload.get("asset_manifest_id"),
                payload.get("prompt_profile_digest"),
                payload.get("role_profile_digest"),
                payload.get("seed_policy"),
                json.dumps(payload.get("requested_seed")),
                json.dumps(payload.get("qualification_ids", []),
                           sort_keys=True),
                payload.get("run_budget_id"),
                payload.get("initial_reservation_id"),
                journal_cursor,
            ),
        )
        await connection.execute(
            "INSERT INTO run_queue ("
            "run_id, admission_id, admission_digest, journal_cursor) "
            "VALUES (?, ?, ?, ?)",
            (
                operation.run_id,
                payload["admission_id"],
                payload["admission_digest"],
                journal_cursor,
            ),
        )
        return

    new_state = str(run_row["state"])
    attempt = int(run_row["attempt"])
    if operation.operation_type == "terminal_outcome":
        new_state = TERMINAL_STATE_FOR_CLASS[payload["common_class"]]
    elif operation.operation_type == "human_control":
        control_states = {
            "pause": "paused",
            "resume": "running",
            "cancel": "cancelling",
        }
        new_state = control_states.get(payload["operation"], new_state)
    elif operation.operation_type == "activation_transition":
        if payload["activation_state"] == "running" and new_state in (
            "admitted", "queued",
        ):
            new_state = "running"
        if payload.get("attempt") is not None:
            attempt = int(payload["attempt"])

    await connection.execute(
        "UPDATE runs SET state = ?, attempt = ?, "
        "projection_version = projection_version + 1, "
        "next_run_sequence = ?, chain_head_digest = ?, journal_cursor = ? "
        "WHERE run_id = ? AND projection_version = ?",
        (
            new_state,
            attempt,
            run_sequence + 1,
            transaction_digest,
            journal_cursor,
            operation.run_id,
            projection_version,
        ),
    )


# ── Replay, projections, and snapshots ───────────────────────────────────


def empty_projection_state() -> dict[str, Any]:
    """Return the empty typed projection state."""
    return {
        "runs": {},
        "admissions": {},
        "runtime_state": {},
        "checkpoints": {},
        "circuits": {},
        "activations": {},
        "activation_dispatch": {},
        "effects": {},
        "effect_operations": {},
        "budgets": {},
        "traces": {},
        "evidence": {},
        "goals": {},
        "outcomes": {},
        "invalidation_validity": {},
        "controls": {},
        "replay_status": {"status": "complete", "redactions": []},
    }


def apply_record_to_state(
    state: dict[str, Any], record: JournalRecord,
) -> dict[str, Any]:
    """Apply one journal record to the typed projection state.

    The reducer is deterministic: replay from cursor zero rebuilds
    every projection, and a rejected proposal changes only the
    declared shared projections.
    """
    payload = record.payload
    run_id = record.run_id
    if record.operation_type == "admission_identity":
        state["runs"][run_id] = {
            "task_id": record.task_id,
            "runtime_id": record.runtime_id,
            "runtime_contract_version": record.runtime_contract_version,
            "state": "admitted",
            "attempt": 0,
        }
        state["admissions"][run_id] = {
            "admission_id": payload["admission_id"],
            "admission_digest": payload["admission_digest"],
        }
    elif record.operation_type == "activation_transition":
        state["activations"].setdefault(run_id, {})[
            payload["activation_id"]
        ] = payload["activation_state"]
        run = state["runs"][run_id]
        if payload["activation_state"] == "running" and run["state"] in (
            "admitted", "queued",
        ):
            run["state"] = "running"
        if payload.get("attempt") is not None:
            run["attempt"] = int(payload["attempt"])
        dispatch_row = payload.get("dispatch_row")
        if dispatch_row is not None:
            state["activation_dispatch"][dispatch_row["grant_id"]] = {
                "activation_id": payload["activation_id"],
                "activation_attempt": payload.get("activation_attempt"),
                "dispatch_state": dispatch_row["dispatch_state"],
            }
        _append_trace(state, record, "activation")
    elif record.operation_type == "effect_transition":
        state["effects"].setdefault(run_id, {})[payload["effect_id"]] = (
            payload["effect_state"]
        )
        operation_id = payload.get("effect_operation_id")
        if operation_id is not None:
            operation = state["effect_operations"].setdefault(
                operation_id, {"authoritative_result_effect_id": None},
            )
            authoritative = payload.get("authoritative_result_effect_id")
            if authoritative is not None:
                operation["authoritative_result_effect_id"] = authoritative
        _append_trace(state, record, "effect")
    elif record.operation_type == "proposal_decision":
        accepted = payload["decision"] == "accepted"
        state["activations"].setdefault(run_id, {})[
            payload["activation_id"]
        ] = payload["activation_state"]
        state["circuits"][run_id] = {
            "state": payload["circuit_state"],
            "decision": payload["circuit_decision"],
        }
        _append_trace(state, record, "proposal")
        if accepted:
            runtime_state = state["runtime_state"].setdefault(run_id, {})
            runtime_state.update(payload["projection_changes"])
            state["checkpoints"][run_id] = payload["checkpoint_digest"]
            budget = payload["budget"]
            totals = state["budgets"].setdefault(
                run_id, {"reserved": 0, "consumed": 0},
            )
            totals["reserved"] += int(budget.get("reserved", 0))
            totals["consumed"] += int(budget.get("consumed", 0))
            if payload.get("effect_id") is not None:
                state["effects"].setdefault(run_id, {})[
                    payload["effect_id"]
                ] = payload.get("effect_state", "approved")
    elif record.operation_type == "terminal_outcome":
        state["outcomes"][run_id] = dict(payload)
        state["runs"][run_id]["state"] = TERMINAL_STATE_FOR_CLASS[
            payload["common_class"]
        ]
        _append_trace(state, record, "terminal")
    elif record.operation_type == "post_terminal_invalidation":
        for target in payload["targets"]:
            key = f"{target['kind']}:{target['reference']}"
            state["invalidation_validity"][key] = {
                "current": False,
                "superseded_by": payload["invalidation_id"],
            }
        _append_trace(state, record, "post_terminal.invalidated")
    elif record.operation_type == "human_control":
        state["controls"].setdefault(run_id, []).append(
            {
                "control_id": payload["control_id"],
                "operation": payload["operation"],
                "actor_id": payload["actor_id"],
            }
        )
        control_states = {
            "pause": "paused",
            "resume": "running",
            "cancel": "cancelling",
        }
        run = state["runs"][run_id]
        run["state"] = control_states.get(
            payload["operation"], run["state"],
        )
        _append_trace(state, record, "control")
    elif record.operation_type == "evidence_update":
        state["evidence"][payload["claim_id"]] = payload["evidence_state"]
        _append_trace(state, record, "evidence.updated")
    elif record.operation_type == "goal_update":
        state["goals"][payload["goal_id"]] = payload["goal_state"]
        _append_trace(state, record, "goal.updated")
    elif record.operation_type == "budget_reconciliation":
        totals = state["budgets"].setdefault(
            run_id, {"reserved": 0, "consumed": 0},
        )
        totals["consumed"] += int(payload["consumed_usd_millionths"])
        _append_trace(state, record, "budget")
    elif record.operation_type == CHAIN_COMPACTION_OPERATION:
        state["runs"].setdefault(
            run_id,
            {
                "task_id": record.task_id,
                "runtime_id": record.runtime_id,
                "runtime_contract_version": record.runtime_contract_version,
                "state": payload["retained_state"],
                "attempt": 0,
            },
        )
        state["runs"][run_id]["state"] = payload["retained_state"]
        state["replay_status"]["status"] = "partial"
        state["replay_status"]["redactions"].append(
            {
                "run_id": run_id,
                "reason": "redacted_by_policy",
                "erasure_manifest_digest": payload["erasure_manifest_digest"],
            }
        )
    else:
        raise JournalIntegrityError(
            f"Unknown journal operation: {record.operation_type!r}"
        )

    # An erased replay-critical artifact makes the replay partial.
    for reference in payload.get("replay_critical_artifacts", []):
        if reference.get("erased"):
            state["replay_status"]["status"] = "partial"
            state["replay_status"]["redactions"].append(
                {
                    "run_id": run_id,
                    "reason": "redacted_by_policy",
                    "artifact_digest": reference["content_digest"],
                }
            )
    return state


def _append_trace(
    state: dict[str, Any], record: JournalRecord, event_kind: str,
) -> None:
    state["traces"].setdefault(record.run_id, []).append(
        {
            "event": event_kind,
            "journal_cursor": record.journal_cursor,
            "transaction_digest": record.transaction_digest,
        }
    )


def projection_digest(state: dict[str, Any]) -> str:
    """Return the canonical digest of the typed projection state."""
    return digest_hex(PROJECTION_DIGEST_DOMAIN, state)


async def read_journal(from_cursor: int = 0) -> list[JournalRecord]:
    """Read every journal record after one cursor, in order."""
    async with _journal_connect() as connection:
        cursor = await connection.execute(
            "SELECT * FROM runtime_journal WHERE journal_cursor > ? "
            "ORDER BY journal_cursor",
            (from_cursor,),
        )
        rows = await cursor.fetchall()
    return [_record_from_row(row) for row in rows]


def verify_chain(records: list[JournalRecord]) -> None:
    """Verify every digest chain or stop with an integrity error."""
    heads: dict[tuple[str, int], tuple[int, str]] = {}
    for record in records:
        recomputed = record_transaction_digest(record)
        if recomputed != record.transaction_digest:
            raise JournalIntegrityError(
                f"Journal cursor {record.journal_cursor} fails its "
                "transaction digest"
            )
        if digest_hex(PAYLOAD_DIGEST_DOMAIN, record.payload) != (
            record.payload_digest
        ):
            raise JournalIntegrityError(
                f"Journal cursor {record.journal_cursor} fails its "
                "payload digest"
            )
        chain = (record.run_id, record.chain_epoch)
        if chain not in heads:
            expected_previous = GENESIS_PREVIOUS_DIGEST
            expected_sequence = 0
        else:
            last_sequence, last_digest = heads[chain]
            expected_previous = last_digest
            expected_sequence = last_sequence + 1
        if record.run_sequence != expected_sequence:
            raise JournalIntegrityError(
                f"Journal cursor {record.journal_cursor} breaks the run "
                "sequence"
            )
        if record.previous_digest != expected_previous:
            raise JournalIntegrityError(
                f"Journal cursor {record.journal_cursor} breaks the digest "
                "chain"
            )
        heads[chain] = (record.run_sequence, record.transaction_digest)


@dataclass(frozen=True)
class ReplayResult:
    """The verified result of one journal replay."""

    state: dict[str, Any]
    last_cursor: int
    state_digest: str
    status: str
    used_snapshot: bool


async def replay(
    *, snapshot: dict[str, Any] | None = None,
) -> ReplayResult:
    """Rebuild every projection from the journal.

    A verified snapshot accelerates the replay; a corrupted snapshot
    falls back to full replay from cursor zero.
    """
    used_snapshot = False
    state = empty_projection_state()
    from_cursor = 0
    if snapshot is not None:
        try:
            verify_snapshot(snapshot)
            await _verify_snapshot_head(snapshot)
            state = json.loads(json.dumps(snapshot["state"]))
            from_cursor = int(snapshot["last_journal_cursor"])
            used_snapshot = True
        except SnapshotVerificationError:
            state = empty_projection_state()
            from_cursor = 0
            used_snapshot = False
    records = await read_journal(from_cursor)
    if from_cursor == 0:
        verify_chain(records)
    for record in records:
        state = apply_record_to_state(state, record)
    last_cursor = records[-1].journal_cursor if records else from_cursor
    return ReplayResult(
        state=state,
        last_cursor=last_cursor,
        state_digest=projection_digest(state),
        status=str(state["replay_status"]["status"]),
        used_snapshot=used_snapshot,
    )


async def verify_durable_projections() -> None:
    """Compare the durable run projections against a full replay."""
    result = await replay()
    async with _journal_connect() as connection:
        cursor = await connection.execute("SELECT * FROM runs")
        rows = await cursor.fetchall()
    durable = {
        str(row["run_id"]): {
            "task_id": str(row["task_id"]),
            "runtime_id": str(row["runtime_id"]),
            "runtime_contract_version": str(
                row["runtime_contract_version"]
            ),
            "state": str(row["state"]),
            "attempt": int(row["attempt"]),
        }
        for row in rows
    }
    if durable != result.state["runs"]:
        raise JournalIntegrityError(
            "The durable run projection disagrees with journal replay"
        )


def create_snapshot(result: ReplayResult) -> dict[str, Any]:
    """Create one verified replay accelerator from a replay result."""
    body = {
        "projection_schema_version": RUNTIME_JOURNAL_SCHEMA_VERSION,
        "last_journal_cursor": result.last_cursor,
        "state": result.state,
        "state_digest": result.state_digest,
    }
    return {**body, "snapshot_digest": digest_hex(SNAPSHOT_DIGEST_DOMAIN, body)}


def verify_snapshot(snapshot: dict[str, Any]) -> None:
    """Verify one snapshot before replay uses the snapshot."""
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_digest"
    }
    if digest_hex(SNAPSHOT_DIGEST_DOMAIN, body) != snapshot.get(
        "snapshot_digest",
    ):
        raise SnapshotVerificationError("The snapshot digest does not match")
    if projection_digest(snapshot["state"]) != snapshot["state_digest"]:
        raise SnapshotVerificationError(
            "The snapshot state digest does not match"
        )


async def _verify_snapshot_head(snapshot: dict[str, Any]) -> None:
    """Verify the snapshot cursor against the stored journal."""
    cursor_value = int(snapshot["last_journal_cursor"])
    if cursor_value == 0:
        return
    async with _journal_connect() as connection:
        cursor = await connection.execute(
            "SELECT journal_cursor FROM runtime_journal "
            "WHERE journal_cursor = ?",
            (cursor_value,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise SnapshotVerificationError(
            "The snapshot names a journal cursor that does not exist"
        )


# ── Mutable delivery state ───────────────────────────────────────────────


async def publish_journal_delivery(
    journal_cursor: int, *, database_time: str | None = None,
) -> dict[str, Any]:
    """Publish or retry one journal delivery.

    Only the mutable delivery projection changes; the journal row
    stays immutable.
    """
    async with _journal_connect() as connection:
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        await connection.execute(
            "INSERT INTO journal_delivery "
            "(journal_cursor, delivery_state, attempts, published_at) "
            "VALUES (?, 'published', 1, ?) "
            "ON CONFLICT(journal_cursor) DO UPDATE SET "
            "delivery_state = 'published', attempts = attempts + 1, "
            "published_at = excluded.published_at",
            (journal_cursor, now),
        )
        await connection.commit()
        cursor = await connection.execute(
            "SELECT * FROM journal_delivery WHERE journal_cursor = ?",
            (journal_cursor,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise JournalError("The delivery row is unreadable")
    return dict(row)


async def record_task_tombstone(
    task_id: str,
    *,
    erasure_state: str = "recorded",
    journal_cursor: int | None = None,
    database_time: str | None = None,
) -> None:
    """Record one task tombstone instead of deleting authority rows."""
    async with _journal_connect() as connection:
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        await connection.execute(
            "INSERT INTO task_tombstones "
            "(task_id, deleted_at, erasure_state, journal_cursor) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "erasure_state = excluded.erasure_state",
            (task_id, now, erasure_state, journal_cursor),
        )
        await connection.commit()


async def compact_chain(
    run_id: str,
    *,
    approver_ids: tuple[str, str],
    erasure_manifest: dict[str, Any],
    database_time: str | None = None,
) -> JournalRecord:
    """Run one privileged chain compaction for a policy-required erasure.

    Two distinct approvers authorize the compaction. The migration
    removes the old chain epoch inside one transaction, starts a new
    epoch with a retained erasure manifest, and later replays return
    ``redacted_by_policy`` for the missing content.
    """
    first, second = approver_ids
    if not first or not second or first == second:
        raise JournalError(
            "Chain compaction requires two distinct approvers"
        )
    async with _journal_connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            run_cursor = await connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,),
            )
            run_row = await run_cursor.fetchone()
            if run_row is None:
                raise JournalError(f"Unknown run: {run_id}")
            now = await db._control_now(connection, database_time)  # noqa: SLF001
            new_epoch = int(run_row["chain_epoch"]) + 1
            manifest_digest = digest_hex(
                "journal-erasure-manifest", erasure_manifest,
            )
            payload = {
                "retained_state": str(run_row["state"]),
                "erasure_manifest": erasure_manifest,
                "erasure_manifest_digest": manifest_digest,
                "approver_ids": sorted(approver_ids),
                "compacted_epoch": int(run_row["chain_epoch"]),
            }
            payload_digest = digest_hex(PAYLOAD_DIGEST_DOMAIN, payload)
            transaction_id = f"journal-txn-{uuid.uuid4()}"
            schema_versions = {"payload_schema_version": "1"}
            transaction_digest = _envelope_digest(
                transaction_id=transaction_id,
                task_id=str(run_row["task_id"]),
                run_id=run_id,
                chain_epoch=new_epoch,
                run_sequence=0,
                previous_digest=GENESIS_PREVIOUS_DIGEST,
                payload_digest=payload_digest,
                runtime_id=str(run_row["runtime_id"]),
                runtime_contract_version=str(
                    run_row["runtime_contract_version"]
                ),
                payload_schema_versions=schema_versions,
                correlation_id=None,
                causation_id=None,
                producer="operator",
                authority_type="privileged_migration",
                recorded_at=now,
                data_classification="internal",
                redaction_policy_version="1",
                operation_type=CHAIN_COMPACTION_OPERATION,
                payload=payload,
            )
            # The privileged migration alone may remove authority rows:
            # it lifts the immutability trigger inside this transaction.
            await connection.execute(
                "DROP TRIGGER runtime_journal_immutable_delete"
            )
            await connection.execute(
                "DELETE FROM runtime_journal WHERE run_id = ? "
                "AND chain_epoch < ?",
                (run_id, new_epoch),
            )
            await connection.execute(
                "CREATE TRIGGER runtime_journal_immutable_delete "
                "BEFORE DELETE ON runtime_journal BEGIN "
                "SELECT RAISE(ABORT, 'runtime_journal rows are immutable'); "
                "END"
            )
            insert_cursor = await connection.execute(
                "INSERT INTO runtime_journal ("
                "transaction_id, idempotency_key, task_id, run_id, "
                "chain_epoch, run_sequence, previous_digest, payload_digest, "
                "transaction_digest, runtime_id, runtime_contract_version, "
                "payload_schema_versions, producer, authority_type, "
                "recorded_at, data_classification, "
                "redaction_policy_version, operation_type, payload) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'operator', "
                "'privileged_migration', ?, 'internal', '1', ?, ?)",
                (
                    transaction_id,
                    digest_hex(
                        IDEMPOTENCY_DIGEST_DOMAIN,
                        {"compaction": run_id, "epoch": new_epoch},
                    ),
                    str(run_row["task_id"]),
                    run_id,
                    new_epoch,
                    GENESIS_PREVIOUS_DIGEST,
                    payload_digest,
                    transaction_digest,
                    str(run_row["runtime_id"]),
                    str(run_row["runtime_contract_version"]),
                    json.dumps(schema_versions, sort_keys=True),
                    now,
                    CHAIN_COMPACTION_OPERATION,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            journal_cursor = insert_cursor.lastrowid
            await connection.execute(
                "UPDATE runs SET chain_epoch = ?, next_run_sequence = 1, "
                "chain_head_digest = ?, journal_cursor = ?, "
                "projection_version = projection_version + 1 "
                "WHERE run_id = ?",
                (new_epoch, transaction_digest, journal_cursor, run_id),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        stored_cursor = await connection.execute(
            "SELECT * FROM runtime_journal WHERE journal_cursor = ?",
            (journal_cursor,),
        )
        stored = await stored_cursor.fetchone()
    if stored is None:
        raise JournalError("The compaction record is unreadable")
    return _record_from_row(stored)
