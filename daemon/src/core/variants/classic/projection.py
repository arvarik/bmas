"""The journal-backed board projection of the Classic native pair.

The native pair stores its board as a projection of ``proposal_decision``
journal records. ``NativeBoardGateway`` keeps every validation and
permission rule of the legacy gateway. Instead of writing the board
store first, it returns one validated ``BoardMutation`` and hands it to
the run's committer, which validates the live run authority, promotes
the entry body as an artifact, commits the journal record with the
projection rows in the same transaction, and only then lets the
gateway apply the change to the hot board store and emit its events.

The checkpoint of a native run is a verified snapshot of the journal
state: the board projection, its content digest, and the engine's
control metadata. A resume verifies the snapshot against the journal
before it trusts one value, and it restores the hot board store from
the projection rows and the promoted bodies.
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import database as db
import runtime_journal as journal
from core.asset_store import (
    ARTIFACT_CONTENT_DIGEST_DOMAIN,
    ArtifactStore,
    DataClass,
    RetentionClass,
)
from core.board_store import make_event
from core.capabilities import AuthorizationError, authorize_remove
from core.digest_profile import digest_bytes, digest_hex, plain_json
from core.entry import BoardEntry, entry_to_dict
from core.failpoints import failpoint
from core.gateway import BoardGateway, EntryRejected
from core.protocol import (
    EVENT_BOARD_ENTRY,
    EVENT_ENTRY_REMOVED,
    EVENT_ENTRY_STATUS_CHANGED,
)

if TYPE_CHECKING:
    from core.board_store import BoardStore
    from core.event_emitter import EventEmitter
    from core.gateway import CommitGuard, RecomputeHook

logger = logging.getLogger("bmas.classic.projection")

BOARD_PROJECTION_DIGEST_DOMAIN = "classic-board-projection"
BOARD_MUTATION_DIGEST_DOMAIN = "classic-board-mutation"
HOST_TURN_ENVELOPE_DOMAIN = "classic-host-turn"
CHECKPOINT_SCHEMA_VERSION = "1"
BODY_MEDIA_TYPE = "text/markdown"
BOARD_ACCESS_POLICY = "task-scope"
ARCHIVED_STATUS = "archived"
REMOVED_STATUS = "removed"
ARCHIVE_REASON = "space archived"

# The projection row fields that the journal transaction assigns. The
# content digest of the board excludes them, so a runtime digests the
# board it is about to commit before the commit allocates a cursor.
VOLATILE_ROW_FIELDS = frozenset({
    "created_cursor", "journal_cursor", "created_at", "updated_at", "removed_at", "task_id",
})

# The control metadata keys that never enter the checkpoint snapshot:
# the checkpoint itself, and the static configuration envelopes the
# admission already binds by digest.
CONTROL_META_EXCLUDED = frozenset({
    "variant_checkpoint", "effective_configuration", "effective_task_config",
    "effective_routing", "effective_registry",
})


class ClassicIntegrityError(RuntimeError):
    """A verified integrity failure: the journal, a snapshot, or an artifact."""


class ClassicFenceError(RuntimeError):
    """The stored checkpoint belongs to another fence epoch of the run."""


class RunCancelledError(RuntimeError):
    """The live run-control row rejected a mutation with a cancellation."""


class RunDeadlineError(RuntimeError):
    """The live run-control row rejected a mutation after the deadline."""


# ── Artifacts ─────────────────────────────────────────────────────────


def board_artifact_store(tenant_id: str = "tenant-default") -> ArtifactStore:
    """The artifact store that holds every board body of the native pair."""
    return ArtifactStore(Path(db.DB_PATH).parent / "classic-board", tenant_id)


def body_digest(body: str) -> str:
    """The artifact digest of one entry body."""
    return digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, body.encode("utf-8"))


def promote_text(store: ArtifactStore, text: str, *, referenced_by: str, cleaner: bool = False) -> str:
    """Promote one text as an immutable artifact and return its digest."""
    payload = text.encode("utf-8")
    if cleaner:
        failpoint("cleaner.before_artifact_stage")
    staged = store.stage(
        payload,
        declared_digest=digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, payload),
        declared_size=len(payload),
        media_type=BODY_MEDIA_TYPE,
        scanner_result="clean",
        data_class=DataClass.INTERNAL,
        access_policy=BOARD_ACCESS_POLICY,
        retention_class=RetentionClass.REPLAY_REQUIRED,
    )
    if cleaner:
        failpoint("cleaner.after_artifact_stage")
        failpoint("cleaner.before_artifact_promotion")
    digest = store.promote(staged)
    if cleaner:
        failpoint("cleaner.after_artifact_promotion")
        failpoint("cleaner.before_artifact_reference")
    store.commit_reference(digest, referenced_by=referenced_by)
    if cleaner:
        failpoint("cleaner.after_artifact_reference")
    return digest


def read_text(store: ArtifactStore, content_digest: str) -> str:
    """Read one promoted body, or its erasure marker."""
    record = store.read_object(content_digest)
    if record.get("redacted"):
        return f"[erased: {record.get('reason', 'policy')}]"
    return bytes(record["payload"]).decode("utf-8")


# ── The board content state and its digest ────────────────────────────


def board_content(board: dict[str, Any]) -> dict[str, Any]:
    """The content view of one board projection without the volatile fields."""
    return {
        "entries": {
            entry_id: {
                key: value for key, value in row.items() if key not in VOLATILE_ROW_FIELDS
            }
            for entry_id, row in sorted(board.get("entries", {}).items())
        },
        "tombstones": {
            entry_id: {
                key: value for key, value in row.items() if key not in VOLATILE_ROW_FIELDS
            }
            for entry_id, row in sorted(board.get("tombstones", {}).items())
        },
    }


def board_projection_digest(board: dict[str, Any]) -> str:
    """The content digest of one board projection.

    The digest covers every entry row and every tombstone without the
    journal cursors and the transaction times, so a replay from cursor
    zero and the runtime's pre-commit view reach the same value.
    """
    return digest_hex(BOARD_PROJECTION_DIGEST_DOMAIN, plain_json(board_content(board)))


def confidence_text(value: Any) -> str:
    """The exact text of one confidence value, as the digest profile stores it."""
    return str(plain_json(float(value)))


def entry_projection(entry: BoardEntry) -> dict[str, Any]:
    """The projection row content of one validated board entry."""
    return {
        "entry_id": entry.id,
        "entry_type": entry.type,
        "author": entry.author,
        "activation_id": entry.created_by_turn,
        "round": int(entry.round),
        "space": entry.space,
        "title": entry.title,
        "body_digest": body_digest(entry.body),
        "refs": [str(ref) for ref in entry.refs],
        "sources": [str(source) for source in entry.sources],
        "confidence": confidence_text(entry.confidence),
        "status": entry.status,
    }


async def replay_run_board(
    run_id: str, *, until_cursor: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Replay one run's journal from cursor zero.

    Returns the run's board projection, the run's projection row, and
    the last cursor the replay applied. The chain verifies before any
    record applies.
    """
    records = await journal.read_journal(run_id=run_id)
    journal.verify_chain(records)
    state = journal.empty_projection_state()
    last_cursor = 0
    for record in records:
        if until_cursor is not None and record.journal_cursor > until_cursor:
            break
        state = journal.apply_record_to_state(state, record)
        last_cursor = record.journal_cursor
    board = state["board"].get(run_id) or journal.empty_board_state()
    run_state = state["runs"].get(run_id) or {}
    return board, run_state, last_cursor


# ── Mutations ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BoardMutation:
    """One validated board mutation before its commit.

    ``section`` is the board section the journal payload carries for an
    accepted mutation. ``bodies`` holds the entry bodies the committer
    promotes before the journal write; the payload keeps digests only.
    """

    kind: str
    decision: str
    task_id: str
    actor: str
    activation_id: str | None
    round: int
    token: str
    proposal: dict[str, Any]
    section: dict[str, Any] | None = None
    bodies: dict[str, str] = field(default_factory=dict)
    reason: str | None = None

    def proposal_digest(self) -> str:
        return digest_hex(BOARD_MUTATION_DIGEST_DOMAIN, plain_json(self.proposal))

    def envelope_digest(self) -> str:
        """The digest of the host turn that carried this mutation.

        The host still dispatches every activation on the runtime's
        behalf, so the envelope names the host turn. Work package 5B
        replaces it with the sealed execution envelope of the
        activation.
        """
        return digest_hex(HOST_TURN_ENVELOPE_DOMAIN, {
            "kind": "host_turn",
            "task_id": self.task_id,
            "actor": self.actor,
            "activation_id": self.activation_id,
            "round": int(self.round),
        })

    def entry_ids(self) -> list[str]:
        section = self.section or {}
        ids = [str(entry["entry_id"]) for entry in section.get("entries") or []]
        ids.extend(str(change["entry_id"]) for change in section.get("status_changes") or [])
        ids.extend(str(tomb["entry_id"]) for tomb in section.get("tombstones") or [])
        return ids


class BoardMutationCommitter(Protocol):
    """Commit one validated mutation through the unit of work."""

    async def authorize(self) -> dict[str, Any]:
        """Validate the live run authority or raise."""
        ...

    async def commit(self, mutation: BoardMutation) -> journal.JournalRecord:
        """Commit one mutation as one proposal decision."""
        ...

    artifacts: Any

    async def load_board(self) -> None:
        """Reload the durable board projection."""
        ...

    def board_state(self) -> dict[str, Any]:
        """The current board projection the committer tracks."""
        ...


def _section(
    kind: str,
    *,
    actor: str,
    activation_id: str | None,
    round_no: int,
    mutation_id: str | None,
    entries: list[dict[str, Any]] | None = None,
    status_changes: list[dict[str, Any]] | None = None,
    tombstones: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": journal.BOARD_SECTION_SCHEMA_VERSION,
        "kind": kind,
        "actor": actor,
        "activation_id": activation_id,
        "round": int(round_no),
        "mutation_id": mutation_id,
        "entries": list(entries or []),
        "status_changes": list(status_changes or []),
        "tombstones": list(tombstones or []),
    }


class NativeBoardGateway(BoardGateway):
    """The board gateway of the native pair.

    Every mutating call validates the live run authority through the
    committer. A board content change commits one ``proposal_decision``
    before the hot store changes, and a rejected proposal commits one
    rejected decision before the rejection event.
    """

    def __init__(
        self,
        board_store: BoardStore,
        event_emitter: EventEmitter,
        *,
        committer: BoardMutationCommitter,
        recompute_hooks: list[RecomputeHook] | None = None,
        commit_guard: CommitGuard | None = None,
        max_title_len: int | None = None,
        max_body_len: int | None = None,
    ) -> None:
        arguments: dict[str, Any] = {}
        if max_title_len is not None:
            arguments["max_title_len"] = max_title_len
        if max_body_len is not None:
            arguments["max_body_len"] = max_body_len
        super().__init__(
            board_store, event_emitter,
            recompute_hooks=recompute_hooks, commit_guard=commit_guard, **arguments,
        )
        self._committer = committer

    @property
    def committer(self) -> BoardMutationCommitter:
        return self._committer

    # ── Content mutations ────────────────────────────────────────────

    async def apply_condensation(self, *, task_id: str, actor: str, capabilities: list[str],
                                 proposal: dict[str, Any], turn_id: str, attempt: int,
                                 round_no: int, space: str = "public") -> list[BoardEntry]:
        """Validate and commit one complete cleaner decision under the board lock."""
        import activation_service
        from core.variants.classic.activations import reconcile_call
        from core.variants.classic.cleaner import CondensationError, CondensationPlan
        from core.variants.classic.compiler import load_specification, specification_store
        from execution_envelope import ModelProposal

        async with self._task_lock(task_id):
            await self._assert_commit_allowed(task_id)
            activation = await activation_service.get_activation(turn_id, attempt)
            run_id = str(activation["run_id"])
            if activation["task_id"] != task_id:
                raise CondensationError("The cleaner activation belongs to another task")
            content = ModelProposal(schema_version="classic-proposal/1", content=plain_json(proposal))
            if content.digest() != activation["proposal_digest"]:
                raise CondensationError("The proposal differs from the verified cleaner response")
            stored = await db.get_classic_specification(run_id)
            if stored is None:
                raise CondensationError("The cleaner requires the native specification")
            spec = load_specification(specification_store(), str(stored["artifact_digest"]))
            binding = self._committer
            async with db._connect() as connection:  # noqa: SLF001
                versions = await connection.execute_fetchall("SELECT projection_version FROM runs WHERE run_id = ?", (run_id,))
            # Reload the authority after a crash or another gateway's write.
            await binding.load_board()
            if activation["state"] == "committed":
                await restore_board_store(self._store, task_id, binding.board_state(), binding.artifacts)
                await reconcile_call(run_id=run_id, activation_id=turn_id, attempt=attempt)
                return []
            snapshot = {}
            for key, row in binding.board_state()["entries"].items():
                snapshot[key] = BoardEntry(id=key, task_id=task_id, type=row["entry_type"],
                    author=row["author"], body=read_text(binding.artifacts, row["body_digest"]),
                    refs=list(row["refs"]), sources=list(row["sources"]), status=row["status"],
                    round=int(row["round"]), space=str(row["space"]))
            async with db._connect() as connection:  # noqa: SLF001
                claims = await connection.execute_fetchall("SELECT claim_id FROM claim_index WHERE run_id = ?", (run_id,))
                evidence = await connection.execute_fetchall(
                    "SELECT decision_id FROM evidence_decisions WHERE run_id = ?", (run_id,))
            rejection = None
            entry = None
            plan = None
            try:
                if not spec.cleaner.enabled:
                    raise CondensationError("The immutable specification disables the cleaner")
                plan = CondensationPlan.from_proposal(content.content)
                plan.validate(snapshot, capabilities=capabilities,
                    max_body=spec.board.max_entry_body_characters, max_title=spec.board.max_title_characters,
                    max_entries=spec.cleaner.entry_threshold, max_tokens=spec.cleaner.token_threshold,
                    recent_rounds=spec.cleaner.retain_recent_rounds,
                    claim_ids={str(row["claim_id"]) for row in claims},
                    evidence_ids={str(row["decision_id"]) for row in evidence},
                    tombstone_ids=set(binding.board_state()["tombstones"]), space=space)
                raw = plan.summary
                entry = BoardEntry(id=f"condensed-{turn_id}-{attempt}", task_id=task_id,
                    type="condensed_finding", author=actor, body=raw["body"], title=raw.get("title"),
                    refs=list(raw["refs"]), sources=list(raw["sources"]),
                    confidence=float(raw.get("confidence", 0.5)), round=round_no, space=space,
                    created_by_turn=turn_id)
            except (CondensationError, AuthorizationError) as exc:
                rejection = str(exc)
            section = None if entry is None or plan is None else _section("condensation",
                actor=actor, activation_id=turn_id, round_no=round_no,
                mutation_id=f"condensation-{turn_id}-{attempt}", entries=[entry_projection(entry)],
                tombstones=[{**item, "status": "removed"} for item in plan.removals])
            record = await binding.commit(BoardMutation(kind="condensation",
                decision="rejected" if rejection else "accepted", task_id=task_id, actor=actor,
                activation_id=turn_id, round=round_no, token=f"proposal-decision-{turn_id}-{attempt}",
                proposal={"activation_attempt": attempt,
                          "expected_projection_version": int(versions[0]["projection_version"])},
                reason=rejection, section=section, bodies={entry.id: entry.body} if entry else {}))
            await restore_board_store(self._store, task_id, binding.board_state(), binding.artifacts)
            if rejection:
                await self._emit(task_id, "entry_rejected", {"actor": actor, "reason": rejection,
                    "activation_id": turn_id, "event": "cleaner.activation_failed"})
            elif entry is not None and plan is not None:
                entry.created_at = record.recorded_at
                entry.updated_at = record.recorded_at
                await self._emit(task_id, EVENT_BOARD_ENTRY, {**entry_to_dict(entry), "journal_cursor": record.journal_cursor})
                for removal in plan.removals:
                    await self._emit(task_id, EVENT_ENTRY_REMOVED, {**removal, "by": actor,
                        "journal_cursor": record.journal_cursor})
            await reconcile_call(run_id=run_id, activation_id=turn_id, attempt=attempt)
            return [entry] if entry is not None else []

    async def apply_proposal(self, *, task_id: str, actor: str, capabilities: list[str],
                             proposed: list[dict[str, Any]], turn_id: str, attempt: int,
                             round_no: int, space: str = "public") -> list[BoardEntry]:
        """Validate all entries and commit exactly one model proposal decision."""
        import activation_service
        from core.variants.classic.activations import reconcile_call

        async with self._task_lock(task_id):
            await self._assert_commit_allowed(task_id)
            activation = await activation_service.get_activation(turn_id, attempt)
            if activation["state"] == "committed":
                await reconcile_call(run_id=str(activation["run_id"]), activation_id=turn_id, attempt=attempt)
                return []
            entries = []
            rejection = None
            try:
                for raw in proposed:
                    entries.append(await self._prepare_entry(
                        raw, task_id, actor, turn_id, round_no, space, capabilities))
            except EntryRejected as exc:
                rejection = exc.reason
                entries = []
            record = await self._committer.commit(BoardMutation(
                kind="model_proposal", decision="rejected" if rejection else "accepted",
                task_id=task_id, actor=actor, activation_id=turn_id, round=round_no,
                token=f"proposal-decision-{turn_id}-{attempt}",
                proposal={"activation_attempt": attempt}, reason=rejection,
                section=_section("append", actor=actor, activation_id=turn_id, round_no=round_no,
                    mutation_id=f"proposal-{turn_id}-{attempt}", entries=[entry_projection(e) for e in entries]),
                bodies={e.id: e.body for e in entries},
            ))
            for entry in entries:
                entry.created_at = record.recorded_at
                entry.updated_at = record.recorded_at
                await self._commit(task_id, entry, actor, turn_id, round_no,
                                   mutation_id=f"{turn_id}:{entry.id}")
                await self._emit(task_id, EVENT_BOARD_ENTRY, entry_to_dict(entry))
            if entries:
                await self._recompute_derived(task_id)
            await reconcile_call(run_id=str(activation["run_id"]), activation_id=turn_id, attempt=attempt)
            return entries

    async def append(
        self,
        task_id: str,
        actor: str,
        capabilities: list[str],
        proposed: list[dict[str, Any]],
        turn_id: str,
        round_no: int = 0,
        space: str = "public",
    ) -> list[BoardEntry]:
        committed: list[BoardEntry] = []
        new_entries: list[tuple[BoardEntry, str | None]] = []

        async with self._task_lock(task_id):
            for raw in proposed:
                await self._assert_commit_allowed(task_id)
                mutation_id = str(raw.get("_mutation_id", "")) or None
                if mutation_id and await self._replay_recorded_append(
                    task_id, raw, actor, mutation_id, committed,
                ):
                    continue
                try:
                    entry = await self._prepare_entry(
                        raw, task_id, actor, turn_id, round_no, space, capabilities,
                    )
                except EntryRejected as rejected:
                    await self._committer.commit(BoardMutation(
                        kind="append", decision="rejected", task_id=task_id, actor=actor,
                        activation_id=turn_id, round=int(round_no),
                        token=mutation_id or f"{turn_id}:rejected:{rejected.entry.get('id', '')}",
                        proposal={"entry": raw, "mutation_id": mutation_id},
                        reason=rejected.reason,
                    ))
                    await self._log_rejection(
                        task_id, raw, actor, rejected.reason, turn_id, round_no,
                        mutation_id=mutation_id,
                    )
                    continue
                row = entry_projection(entry)
                record = await self._committer.commit(BoardMutation(
                    kind="append", decision="accepted", task_id=task_id, actor=actor,
                    activation_id=turn_id, round=int(round_no),
                    token=mutation_id or f"{turn_id}:append:{entry.id}",
                    proposal={"entry": raw, "mutation_id": mutation_id, "entry_id": entry.id},
                    section=_section(
                        "append", actor=actor, activation_id=turn_id, round_no=round_no,
                        mutation_id=mutation_id, entries=[row],
                    ),
                    bodies={entry.id: entry.body},
                ))
                entry.created_at = record.recorded_at
                entry.updated_at = record.recorded_at
                await self._commit(
                    task_id, entry, actor, turn_id, round_no, mutation_id=mutation_id,
                )
                committed.append(entry)
                new_entries.append((entry, mutation_id))

            if committed:
                await self._recompute_derived(task_id)
            for entry, mutation_id in new_entries:
                await self._emit(
                    task_id, EVENT_BOARD_ENTRY, entry_to_dict(entry), mutation_id=mutation_id,
                )

        return committed

    async def remove(
        self,
        task_id: str,
        actor: str,
        capabilities: list[str],
        entry_ids: list[str],
        reason: str,
        turn_id: str | None = None,
        round_no: int = 0,
        mutation_id: str | None = None,
    ) -> list[str]:
        removed: list[str] = []

        async with self._task_lock(task_id):
            for entry_id in entry_ids:
                await self._assert_commit_allowed(task_id)
                entry_mutation_id = f"{mutation_id}:{entry_id}" if mutation_id else None
                if entry_mutation_id and await self._replay_recorded_remove(
                    task_id, entry_id, actor, reason, entry_mutation_id, removed,
                ):
                    continue
                entry = await self._store.get_entry(task_id, entry_id)
                if entry is None:
                    logger.warning("remove: entry %s not found in task %s", entry_id, task_id)
                    continue
                token = entry_mutation_id or f"{turn_id or actor}:remove:{entry_id}"
                try:
                    authorize_remove(capabilities, entry.type)
                except AuthorizationError as denied:
                    await self._committer.commit(BoardMutation(
                        kind="remove", decision="rejected", task_id=task_id, actor=actor,
                        activation_id=turn_id, round=int(round_no), token=token,
                        proposal={"entry_id": entry_id, "reason": reason, "action": "remove"},
                        reason=denied.reason,
                    ))
                    await self._log_rejection(
                        task_id, {"entry_id": entry_id, "action": "remove"}, actor,
                        denied.reason, turn_id, round_no, mutation_id=entry_mutation_id,
                    )
                    continue
                if entry.status == REMOVED_STATUS:
                    removed.append(entry_id)
                    continue
                await self._committer.commit(BoardMutation(
                    kind="remove", decision="accepted", task_id=task_id, actor=actor,
                    activation_id=turn_id, round=int(round_no), token=token,
                    proposal={"entry_id": entry_id, "reason": reason, "action": "remove"},
                    section=_section(
                        "remove", actor=actor, activation_id=turn_id, round_no=round_no,
                        mutation_id=entry_mutation_id,
                        tombstones=[{"entry_id": entry_id, "reason": reason, "status": REMOVED_STATUS}],
                    ),
                ))
                seq = await self._store.get_next_seq(task_id)
                event = make_event(
                    task_id=task_id, seq=seq, actor=actor, event_type="entry_removed",
                    entry_id=entry_id,
                    payload={"entry_id": entry_id, "reason": reason, "_mutation_id": entry_mutation_id},
                    round_no=round_no, turn_id=turn_id,
                )
                await self._store.append_event(task_id, event)
                await self._store.remove_entry(task_id, entry_id)
                removed.append(entry_id)
                await self._emit(task_id, EVENT_ENTRY_REMOVED, {
                    "entry_id": entry_id, "by": actor, "reason": reason,
                }, mutation_id=entry_mutation_id)

            if removed:
                await self._recompute_derived(task_id)

        return removed

    async def set_status(
        self,
        task_id: str,
        entry_id: str,
        status: str,
        actor: str,
        mutation_id: str | None = None,
    ) -> None:
        async with self._task_lock(task_id):
            await self._assert_commit_allowed(task_id)
            if mutation_id and await self._replay_recorded_status(
                task_id, entry_id, status, actor, mutation_id,
            ):
                return
            entry = await self._store.get_entry(task_id, entry_id)
            if entry is None:
                logger.warning("set_status: entry %s not found in task %s", entry_id, task_id)
                return
            old_status = entry.status
            if old_status == status:
                return
            record = await self._committer.commit(BoardMutation(
                kind="status", decision="accepted", task_id=task_id, actor=actor,
                activation_id=None, round=int(entry.round),
                token=mutation_id or f"{actor}:status:{entry_id}:{status}",
                proposal={"entry_id": entry_id, "status": status, "old_status": old_status},
                section=_section(
                    "status", actor=actor, activation_id=None, round_no=entry.round,
                    mutation_id=mutation_id,
                    status_changes=[{"entry_id": entry_id, "status": status}],
                ),
            ))
            seq = await self._store.get_next_seq(task_id)
            event = make_event(
                task_id=task_id, seq=seq, actor=actor, event_type="entry_status_changed",
                entry_id=entry_id,
                payload={
                    "entry_id": entry_id, "old_status": old_status, "status": status,
                    "_mutation_id": mutation_id,
                },
            )
            await self._store.append_event(task_id, event)
            entry.status = status
            entry.updated_at = record.recorded_at
            await self._store.upsert_entry(task_id, entry)
            await self._emit(task_id, EVENT_ENTRY_STATUS_CHANGED, {
                "entry_id": entry_id, "by": actor, "old_status": old_status, "status": status,
            }, mutation_id=mutation_id)
            await self._recompute_derived(task_id)

    async def archive_space(
        self,
        task_id: str,
        space: str,
        mutation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._task_lock(task_id):
            await self._assert_commit_allowed(task_id)
            if mutation_id and await self._find_mutation(task_id, mutation_id) is not None:
                return []
            snapshot = await self._store.get_snapshot(task_id)
            already = set(self._committer.board_state().get("tombstones", {}))
            tombstones = [
                {"entry_id": entry.id, "reason": ARCHIVE_REASON, "status": ARCHIVED_STATUS}
                for entry in sorted(snapshot.values(), key=lambda item: item.id)
                if entry.space == space and entry.id not in already
            ]
            if not tombstones:
                # Nothing to record: the space is empty, or a restore
                # already tombstoned every entry of it.
                archived = await self._store.archive_space(task_id, space, mutation_id=mutation_id)
                await self._recompute_derived(task_id)
                return archived
            await self._committer.commit(BoardMutation(
                kind="archive", decision="accepted", task_id=task_id, actor="control_unit",
                activation_id=None, round=0,
                token=mutation_id or f"archive:{space}",
                proposal={"space": space, "entry_ids": [tomb["entry_id"] for tomb in tombstones]},
                section=_section(
                    "archive", actor="control_unit", activation_id=None, round_no=0,
                    mutation_id=mutation_id, tombstones=tombstones,
                ),
            ))
            archived = await self._store.archive_space(task_id, space, mutation_id=mutation_id)
            await self._recompute_derived(task_id)
            return archived

    # ── Derived and control mutations ────────────────────────────────

    async def set_salience(self, task_id: str, entry_id: str, salience: float, actor: str) -> float:
        await self._committer.authorize()
        return await super().set_salience(task_id, entry_id, salience, actor)

    async def boost_salience(
        self, task_id: str, entry_id: str, actor: str, factor: float | None = None,
    ) -> float:
        await self._committer.authorize()
        if factor is None:
            return await super().boost_salience(task_id, entry_id, actor)
        return await super().boost_salience(task_id, entry_id, actor, factor)

    async def set_meta(self, task_id: str, **fields: Any) -> None:
        await self._committer.authorize()
        await super().set_meta(task_id, **fields)

    async def refresh(self, task_id: str) -> None:
        await self._committer.authorize()
        await super().refresh(task_id)


# ── Checkpoints ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifiedCheckpoint:
    """One native checkpoint after every verification passed."""

    snapshot: dict[str, Any]
    board: dict[str, Any]
    control: dict[str, Any]
    last_cursor: int
    board_digest: str
    task_fence: str


def build_checkpoint(
    *,
    run_id: str,
    task_fence: str,
    policy_set_digest: str,
    board: dict[str, Any],
    run_state: dict[str, Any],
    control_meta: dict[str, Any],
    last_cursor: int,
) -> dict[str, Any]:
    """Build one verified snapshot of the native run state.

    The snapshot state holds the board projection with its content
    digest, the run projection, and the engine's control metadata.
    ``create_snapshot`` seals it, so a resume verifies every value
    before it trusts one.
    """
    # The control metadata keeps its exact JSON values, floats included,
    # so it travels as one canonical JSON text inside the digested state.
    control = json.dumps(
        {key: value for key, value in control_meta.items() if key not in CONTROL_META_EXCLUDED},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    state = plain_json({
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "run_id": run_id,
        "task_fence": task_fence,
        "policy_set_digest": policy_set_digest,
        "board": board,
        "board_digest": board_projection_digest(board),
        "run": run_state,
        "control": control,
    })
    result = journal.ReplayResult(
        state=state,
        last_cursor=int(last_cursor),
        state_digest=journal.projection_digest(state),
        status="complete",
        used_snapshot=False,
    )
    return {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "snapshot": journal.create_snapshot(result),
    }


async def read_checkpoint(
    checkpoint: dict[str, Any], *, run_id: str, task_fence: str,
) -> VerifiedCheckpoint:
    """Verify one stored checkpoint against its digests and the journal.

    The snapshot digest and the state digest must hold, the snapshot
    must name this run and the live task fence, and a replay of the
    run's journal up to the snapshot cursor must rebuild the same
    board. Any mismatch is an integrity failure; a fence mismatch is a
    fence failure, because the checkpoint belongs to another epoch.
    """
    snapshot = checkpoint.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ClassicIntegrityError("The stored checkpoint holds no snapshot")
    try:
        journal.verify_snapshot(snapshot)
    except journal.SnapshotVerificationError as exc:
        raise ClassicIntegrityError(f"The checkpoint snapshot failed verification: {exc}") from exc
    state = snapshot["state"]
    if str(state.get("checkpoint_schema_version")) != CHECKPOINT_SCHEMA_VERSION:
        raise ClassicIntegrityError("The checkpoint carries an unknown schema version")
    if str(state.get("run_id")) != run_id:
        raise ClassicIntegrityError("The checkpoint belongs to another run")
    if str(state.get("task_fence")) != task_fence:
        raise ClassicFenceError("The checkpoint belongs to another fence epoch of the run")
    last_cursor = int(snapshot["last_journal_cursor"])
    board, _run_state, replayed_cursor = await replay_run_board(run_id, until_cursor=last_cursor)
    if replayed_cursor != last_cursor:
        raise ClassicIntegrityError("The checkpoint names a journal cursor the run never reached")
    digest = board_projection_digest(board)
    if digest != state.get("board_digest") or plain_json(board) != state.get("board"):
        raise ClassicIntegrityError("The checkpoint board disagrees with the journal replay")
    try:
        control = json.loads(str(state.get("control") or "{}"))
    except ValueError as exc:
        raise ClassicIntegrityError("The checkpoint control metadata is unreadable") from exc
    if not isinstance(control, dict):
        raise ClassicIntegrityError("The checkpoint control metadata is not an object")
    return VerifiedCheckpoint(
        snapshot=snapshot,
        board=board,
        control=control,
        last_cursor=last_cursor,
        board_digest=digest,
        task_fence=task_fence,
    )


def _entry_number(entry_id: str) -> int:
    prefix, _separator, number = entry_id.partition("-")
    if prefix == "e" and number.isdigit():
        return int(number)
    return 0


async def board_mutation_ids(run_id: str) -> dict[str, str]:
    """The engine mutation identifier of every entry a run appended.

    A restored entry keeps the identifier the engine used, so a
    replayed engine mutation finds its recorded event in the store and
    commits no second record.
    """
    identifiers: dict[str, str] = {}
    for record in await journal.read_journal(run_id=run_id):
        if record.operation_type != "proposal_decision":
            continue
        section = record.payload.get("board")
        if not isinstance(section, dict) or not section.get("mutation_id"):
            continue
        for entry in section.get("entries") or []:
            identifiers[str(entry["entry_id"])] = str(section["mutation_id"])
    return identifiers


async def restore_board_store(
    store: BoardStore,
    task_id: str,
    board: dict[str, Any],
    artifacts: ArtifactStore,
    *,
    mutation_ids: dict[str, str] | None = None,
) -> int:
    """Bring the hot board store up to the board projection.

    The projection is the authority of the native pair. An entry the
    store lacks is imported with its promoted body under its original
    mutation identifier, and an entry whose status lags the projection
    is updated. The sequence counter moves past every imported
    identifier. Returns the number of repairs.
    """
    mutation_ids = mutation_ids or {}
    snapshot = await store.get_snapshot(task_id)
    repaired = 0
    highest = 0
    rows = sorted(
        board.get("entries", {}).values(),
        key=lambda row: (int(row.get("created_cursor") or 0), str(row["entry_id"])),
    )
    for row in rows:
        entry_id = str(row["entry_id"])
        highest = max(highest, _entry_number(entry_id))
        existing = snapshot.get(entry_id)
        status = str(row["status"])
        if existing is None:
            if status == ARCHIVED_STATUS:
                continue
            entry = BoardEntry(
                id=entry_id,
                task_id=task_id,
                type=str(row["entry_type"]),
                author=str(row["author"]),
                body=read_text(artifacts, str(row["body_digest"])),
                title=row.get("title"),
                refs=list(row.get("refs") or []),
                sources=list(row.get("sources") or []),
                confidence=float(row["confidence"]),
                status=status,
                salience=0.0,
                round=int(row.get("round") or 0),
                space=str(row.get("space") or "public"),
                created_by_turn=row.get("activation_id"),
                created_at=str(row.get("created_at") or ""),
                updated_at=str(row.get("updated_at") or ""),
            )
            seq = await store.get_next_seq(task_id)
            await store.append_event(task_id, make_event(
                task_id=task_id, seq=seq, actor=entry.author, event_type="entry_added",
                entry_id=entry_id,
                payload={
                    **entry_to_dict(entry),
                    "_mutation_id": mutation_ids.get(entry_id, f"projection-restore:{entry_id}"),
                },
                round_no=entry.round, turn_id=entry.created_by_turn,
            ))
            await store.upsert_entry(task_id, entry)
            repaired += 1
        elif existing.status != status:
            seq = await store.get_next_seq(task_id)
            if status in (REMOVED_STATUS, ARCHIVED_STATUS):
                await store.append_event(task_id, make_event(
                    task_id=task_id, seq=seq, actor="control_unit", event_type="entry_removed",
                    entry_id=entry_id,
                    payload={"entry_id": entry_id, "reason": "projection restore",
                             "_mutation_id": f"projection-restore:{entry_id}:{status}"},
                ))
                await store.remove_entry(task_id, entry_id)
            else:
                await store.append_event(task_id, make_event(
                    task_id=task_id, seq=seq, actor="control_unit",
                    event_type="entry_status_changed", entry_id=entry_id,
                    payload={"entry_id": entry_id, "old_status": existing.status, "status": status,
                             "_mutation_id": f"projection-restore:{entry_id}:{status}"},
                ))
                existing.status = status
                existing.updated_at = str(row.get("updated_at") or existing.updated_at)
                await store.upsert_entry(task_id, existing)
            repaired += 1
    advance = getattr(store, "advance_seq", None)
    if advance is not None and highest:
        await advance(task_id, highest)
    return repaired


def deadline_after(now: str, seconds: float) -> str:
    """One database timestamp moved forward by a number of seconds."""
    parsed = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    moved = parsed + timedelta(seconds=seconds)
    return moved.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def content_state_after(
    board: dict[str, Any], section: dict[str, Any], *, task_id: str,
) -> dict[str, Any]:
    """The board content after one accepted section, before its commit."""
    return journal.fold_board_section(
        copy.deepcopy(board), section, task_id=task_id, journal_cursor=None, recorded_at=None,
    )
