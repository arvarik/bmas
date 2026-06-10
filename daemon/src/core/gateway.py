# /opt/bmas/daemon/src/core/gateway.py
"""Board Gateway — the deterministic write path (doc 04 §4).

The ONLY component that mutates board state.  Agents propose entries;
the gateway disposes:
    normalize → validate envelope → capability-based authorization
    → commit → emit

The gateway is deterministic, contains no LLM calls, and is fully
unit-testable with an in-memory fake.

Seam compliance:
  - Rule 1: Never hardcodes a sequence, role name, or "control unit"
  - Rule 3: Authors are opaque strings
  - Rule 4: Authorization is capability-based (via capabilities module)
  - Rule 5: Derived fields via pluggable recompute_hooks list
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from core.entry import (
    BoardEntry,
    ProposedEntry,
    clamp_confidence,
    entry_to_dict,
    role_default_type,
    DEFAULT_CONFIDENCE,
    DEFAULT_MAX_TITLE_LEN,
    DEFAULT_MAX_BODY_LEN,
)
from core.protocol import ENTRY_TYPES
from core.capabilities import authorize_post, authorize_remove, AuthorizationError
from core.board_store import BoardStore, make_event
from core.event_emitter import EventEmitter
from core.protocol import (
    EVENT_BOARD_ENTRY,
    EVENT_ENTRY_REMOVED,
    EVENT_ENTRY_STATUS_CHANGED,
    EVENT_ENTRY_REJECTED,
)

logger = logging.getLogger("bmas.gateway")

# Type for recompute hooks: async fn(task_id, board_store) -> None
RecomputeHook = Callable[[str, BoardStore], Awaitable[None]]


class EntryRejected(Exception):
    """Raised when the gateway rejects a proposed entry."""

    def __init__(self, reason: str, entry: dict[str, Any] | None = None):
        self.reason = reason
        self.entry = entry or {}
        super().__init__(reason)


class BoardGateway:
    """Deterministic write path.  Agents propose entries; the gateway disposes.

    Args:
        board_store: The board store abstraction (event log + snapshot).
        event_emitter: SSE event emitter.
        recompute_hooks: Optional list of async hooks called after each
            commit batch to recompute derived fields (e.g. salience).
            Seam rule 5: the traditional variant registers salience;
            stigmergic registers pressure + decay.
        max_title_len: Maximum title length (default 200, doc 04 §4).
        max_body_len: Maximum body length (default 8000, configurable
            via board.max_entry_chars).
    """

    def __init__(
        self,
        board_store: BoardStore,
        event_emitter: EventEmitter,
        recompute_hooks: list[RecomputeHook] | None = None,
        max_title_len: int = DEFAULT_MAX_TITLE_LEN,
        max_body_len: int = DEFAULT_MAX_BODY_LEN,
    ) -> None:
        self._store = board_store
        self._emitter = event_emitter
        self._recompute_hooks = recompute_hooks or []
        self._max_title_len = max_title_len
        self._max_body_len = max_body_len
        # Per-task locks (doc 04 §6): one writer per task
        self._locks: dict[str, asyncio.Lock] = {}

    def _task_lock(self, task_id: str) -> asyncio.Lock:
        """Get or create the per-task lock."""
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        return self._locks[task_id]

    # ── Public API ───────────────────────────────────────────────────

    async def append(
        self,
        task_id: str,
        actor: str,
        capabilities: list[str],
        proposed: list[dict[str, Any]],
        turn_id: str,
        round_no: int = 0,
    ) -> list[BoardEntry]:
        """Validate, authorize, commit, and emit proposed entries.

        Returns the list of committed BoardEntry objects.
        Rejected entries emit entry_rejected events but do not raise.
        """
        committed: list[BoardEntry] = []

        async with self._task_lock(task_id):
            for raw in proposed:
                try:
                    entry = await self._normalize(
                        raw, task_id, actor, turn_id, round_no
                    )
                    self._validate_envelope(entry)
                    await self._validate_refs(task_id, entry)
                    self._authorize_post(capabilities, entry)
                    await self._commit(task_id, entry, actor, turn_id, round_no)
                    committed.append(entry)
                    await self._emit(
                        task_id, EVENT_BOARD_ENTRY, entry_to_dict(entry)
                    )
                except EntryRejected as e:
                    await self._log_rejection(
                        task_id, raw, actor, e.reason, turn_id, round_no
                    )

            # Recompute derived fields after all entries in this batch
            if committed:
                await self._recompute_derived(task_id)

        return committed

    async def remove(
        self,
        task_id: str,
        actor: str,
        capabilities: list[str],
        entry_ids: list[str],
        reason: str,
    ) -> list[str]:
        """Remove entries (Cleaner path, doc 04 §3).

        Flips status to 'removed'; the entry stays in the event log
        and SQLite forever.  Returns the list of successfully removed ids.
        """
        removed: list[str] = []

        async with self._task_lock(task_id):
            for entry_id in entry_ids:
                entry = await self._store.get_entry(task_id, entry_id)
                if entry is None:
                    logger.warning(
                        "remove: entry %s not found in task %s",
                        entry_id,
                        task_id,
                    )
                    continue

                try:
                    authorize_remove(capabilities, entry.type)
                except AuthorizationError as e:
                    await self._log_rejection(
                        task_id,
                        {"entry_id": entry_id, "action": "remove"},
                        actor,
                        e.reason,
                    )
                    continue

                # Commit removal event
                seq = await self._store.get_next_seq(task_id)
                event = make_event(
                    task_id=task_id,
                    seq=seq,
                    actor=actor,
                    event_type="entry_removed",
                    entry_id=entry_id,
                    payload={"entry_id": entry_id, "reason": reason},
                )
                await self._store.append_event(task_id, event)
                await self._store.remove_entry(task_id, entry_id)
                removed.append(entry_id)

                await self._emit(task_id, EVENT_ENTRY_REMOVED, {
                    "entry_id": entry_id,
                    "by": actor,
                    "reason": reason,
                })

            if removed:
                await self._recompute_derived(task_id)

        return removed

    async def set_status(
        self,
        task_id: str,
        entry_id: str,
        status: str,
        actor: str,
    ) -> None:
        """Change an entry's status (supersede, etc.)."""
        async with self._task_lock(task_id):
            entry = await self._store.get_entry(task_id, entry_id)
            if entry is None:
                logger.warning(
                    "set_status: entry %s not found in task %s",
                    entry_id,
                    task_id,
                )
                return

            old_status = entry.status
            entry.status = status
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            await self._store.upsert_entry(task_id, entry)

            seq = await self._store.get_next_seq(task_id)
            event = make_event(
                task_id=task_id,
                seq=seq,
                actor=actor,
                event_type="entry_status_changed",
                entry_id=entry_id,
                payload={
                    "entry_id": entry_id,
                    "old_status": old_status,
                    "status": status,
                },
            )
            await self._store.append_event(task_id, event)

            await self._emit(task_id, EVENT_ENTRY_STATUS_CHANGED, {
                "entry_id": entry_id,
                "by": actor,
                "old_status": old_status,
                "status": status,
            })

    async def set_meta(self, task_id: str, **fields: Any) -> None:
        """Update board metadata (phase, round, budget_spent, etc.)."""
        await self._store.set_meta(task_id, **fields)

    # ── Internal Methods ─────────────────────────────────────────────

    async def _normalize(
        self,
        raw: dict[str, Any],
        task_id: str,
        actor: str,
        turn_id: str,
        round_no: int,
    ) -> BoardEntry:
        """Normalize a proposed entry: strip reserved fields, assign defaults.

        Reserved fields (gateway-assigned): id, status, salience, round,
        author, author_node, created_at, updated_at.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Infer type from actor if not provided
        entry_type = raw.get("type")
        if not entry_type:
            entry_type = role_default_type(actor)

        # Title: truncate if too long (not rejected — doc 04 §4)
        title = raw.get("title")
        if title and len(title) > self._max_title_len:
            title = title[: self._max_title_len]

        # Confidence: clamp to [0, 1]
        confidence = clamp_confidence(raw.get("confidence"))

        # Refs: ensure list
        refs = raw.get("refs", [])
        if not isinstance(refs, list):
            refs = []

        # Assign gateway-controlled fields
        next_seq = await self._store.get_next_seq(task_id)
        entry_id = f"e-{next_seq}"

        entry = BoardEntry(
            id=entry_id,
            task_id=task_id,
            type=entry_type,
            author=actor,
            body=raw.get("body", ""),
            title=title,
            refs=refs,
            confidence=confidence,
            status="open",
            salience=0.0,
            round=round_no,
            space=raw.get("space", "public"),
            created_by_turn=turn_id,
            created_at=now,
            updated_at=now,
        )
        # Stash seq for _commit so it doesn't have to parse the id string.
        # This decouples the event seq from the entry-id naming convention.
        entry._gateway_seq = next_seq  # type: ignore[attr-defined]
        return entry

    def _validate_envelope(self, entry: BoardEntry) -> None:
        """Validate the entry envelope (cheap checks, doc 04 §4).

        Raises EntryRejected on failure.
        """
        # Type must be in the fixed vocabulary
        if entry.type not in ENTRY_TYPES:
            raise EntryRejected(
                reason=f"Unknown entry type: '{entry.type}'",
                entry=entry_to_dict(entry),
            )

        # Body must be non-empty
        if not entry.body or not entry.body.strip():
            raise EntryRejected(
                reason="Entry body is empty",
                entry=entry_to_dict(entry),
            )

        # Body must not exceed max length
        if len(entry.body) > self._max_body_len:
            raise EntryRejected(
                reason=(
                    f"Entry body exceeds max length "
                    f"({len(entry.body)} > {self._max_body_len})"
                ),
                entry=entry_to_dict(entry),
            )

    async def _validate_refs(
        self, task_id: str, entry: BoardEntry
    ) -> None:
        """Validate refs: unknown ids are dropped with a warning, not rejected.

        Per doc 04 §1: agents misremembering an id should not lose their
        contribution.  Dropped refs are logged but do NOT emit
        EVENT_ENTRY_REJECTED (that event is for actual rejections).
        """
        if not entry.refs:
            return

        valid_refs: list[str] = []
        for ref_id in entry.refs:
            exists = await self._store.entry_exists(task_id, ref_id)
            if exists:
                valid_refs.append(ref_id)
            else:
                logger.warning(
                    "Entry %s refs unknown entry %s — dropping ref",
                    entry.id,
                    ref_id,
                )

        entry.refs = valid_refs

    def _authorize_post(
        self, capabilities: list[str], entry: BoardEntry
    ) -> None:
        """Authorize the entry post via capability profiles.

        Raises EntryRejected if not authorized.
        """
        try:
            authorize_post(capabilities, entry.type)
        except AuthorizationError as e:
            raise EntryRejected(
                reason=e.reason,
                entry=entry_to_dict(entry),
            ) from e

    async def _commit(
        self,
        task_id: str,
        entry: BoardEntry,
        actor: str,
        turn_id: str,
        round_no: int,
    ) -> None:
        """Commit an entry: append event to log + update snapshot.

        Uses the seq stashed by _normalize (entry._gateway_seq) so that
        the event seq is decoupled from the entry-id naming convention.
        """
        seq: int = getattr(entry, "_gateway_seq", 0)

        event = make_event(
            task_id=task_id,
            seq=seq,
            actor=actor,
            event_type="entry_added",
            entry_id=entry.id,
            payload=entry_to_dict(entry),
            round_no=round_no,
            turn_id=turn_id,
        )
        await self._store.append_event(task_id, event)
        await self._store.upsert_entry(task_id, entry)

    async def _log_rejection(
        self,
        task_id: str,
        raw: dict[str, Any],
        actor: str,
        reason: str,
        turn_id: str | None = None,
        round_no: int = 0,
    ) -> None:
        """Log a rejected entry event."""
        seq = await self._store.get_next_seq(task_id)
        event = make_event(
            task_id=task_id,
            seq=seq,
            actor=actor,
            event_type="entry_rejected",
            payload={"entry": raw, "actor": actor, "reason": reason},
            round_no=round_no,
            turn_id=turn_id,
        )
        await self._store.append_event(task_id, event)

        await self._emit(task_id, EVENT_ENTRY_REJECTED, {
            "entry": raw,
            "actor": actor,
            "reason": reason,
        })

    async def _emit(
        self, task_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        """Emit an SSE event."""
        try:
            await self._emitter.emit(task_id, event_type, data)
        except Exception:
            logger.warning(
                "Failed to emit %s for task %s", event_type, task_id
            )

    async def _recompute_derived(self, task_id: str) -> None:
        """Run all registered recompute hooks (seam rule 5).

        Traditional registers salience; stigmergic registers pressure + decay;
        patchboard registers its state hash.
        """
        for hook in self._recompute_hooks:
            try:
                await hook(task_id, self._store)
            except Exception:
                logger.warning(
                    "recompute hook failed for task %s", task_id,
                    exc_info=True,
                )


# ── Salience Recompute Hook ──────────────────────────────────────────

async def salience_recompute_hook(
    task_id: str, store: BoardStore
) -> None:
    """Default recompute hook: recompute salience for all entries.

    This is the traditional variant's derived-field computation.
    Import SalienceWeights from config if needed.
    """
    from core.salience import compute_salience, SalienceWeights

    snapshot = await store.get_snapshot(task_id)
    meta = await store.get_meta(task_id)
    current_round = int(meta.get("round", 0))

    scores = compute_salience(snapshot, current_round)

    for entry_id, score in scores.items():
        await store.set_salience(task_id, entry_id, score)
