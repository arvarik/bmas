# /opt/bmas/daemon/src/core/board_store.py
"""Board Store: event log + entry snapshot + fork (doc 04 §5).

Abstraction over the append-only event log and the materialized
snapshot.  Two implementations:
  - InMemoryBoardStore  — for unit tests (no dependencies)
  - SqliteRedisBoardStore — production (Phase 3 wiring)

The event log is the source of truth.  The snapshot is a fold over
the log.  Folding events ordered by seq reconstructs exactly the
same board_entries snapshot, including removed statuses (durability
contract, doc 04 §5.1).

Authors are opaque strings (seam rule 3).
Event types are variant-namespaced (seam rule 2).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from core.entry import BoardEntry, entry_from_dict, entry_to_dict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("bmas.board_store")

# ── Board Event ──────────────────────────────────────────────────────

def make_event(
    task_id: str,
    seq: int,
    actor: str,
    event_type: str,
    entry_id: str | None = None,
    payload: dict[str, Any] | None = None,
    round_no: int = 0,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Create a board event dict (generic shape, doc 04 §5)."""
    return {
        "task_id": task_id,
        "seq": seq,
        "round": round_no,
        "turn_id": turn_id,
        "actor": actor,
        "event_type": event_type,
        "entry_id": entry_id,
        "payload": payload or {},
        "created_at": datetime.now(UTC).isoformat(),
    }


# ── Board Store Protocol ─────────────────────────────────────────────

@runtime_checkable
class BoardStore(Protocol):
    """Protocol for the board store abstraction."""

    async def append_event(
        self, task_id: str, event: dict[str, Any]
    ) -> int:
        """Append an event to the log. Returns the assigned seq."""
        ...

    async def get_snapshot(
        self, task_id: str
    ) -> dict[str, BoardEntry]:
        """Get the current live snapshot (open + superseded entries)."""
        ...

    async def upsert_entry(
        self, task_id: str, entry: BoardEntry
    ) -> None:
        """Insert or update an entry in the snapshot."""
        ...

    async def remove_entry(
        self, task_id: str, entry_id: str
    ) -> None:
        """Mark an entry as removed in the snapshot."""
        ...

    async def get_entry(
        self, task_id: str, entry_id: str
    ) -> BoardEntry | None:
        """Get a single entry by ID, or None."""
        ...

    async def get_events(
        self, task_id: str, until_seq: int | None = None
    ) -> list[dict[str, Any]]:
        """Get ordered events, optionally up to a seq number."""
        ...

    async def get_event_by_mutation_id(
        self, task_id: str, mutation_id: str,
    ) -> dict[str, Any] | None:
        """Get the event for one stable mutation identifier."""
        ...

    async def get_next_seq(self, task_id: str) -> int:
        """Get the next monotonic sequence number for this task."""
        ...

    async def set_meta(self, task_id: str, **fields: Any) -> None:
        """Set board metadata (phase, round, budget_spent, etc.)."""
        ...

    async def get_meta(self, task_id: str) -> dict[str, Any]:
        """Get board metadata."""
        ...

    async def set_salience(
        self, task_id: str, entry_id: str, score: float
    ) -> None:
        """Update the salience score for an entry."""
        ...

    async def entry_exists(
        self, task_id: str, entry_id: str
    ) -> bool:
        """Check if an entry exists (any status)."""
        ...

    async def get_private_snapshot(
        self, task_id: str, space: str,
    ) -> dict[str, BoardEntry]:
        """Get entries scoped to a private space (doc 05 §4)."""
        ...

    async def archive_space(
        self,
        task_id: str,
        space: str,
        mutation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Archive a private space: return its events and wipe live state.

        Returns the archived events for SQLite persistence.
        """
        ...

    async def fork(
        self,
        task_id: str,
        at_event_n: int,
        mutate_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> str:
        """Fork the board at event N, optionally transforming events.

        Creates a new task-scoped board with events 1..N (optionally
        filtered/transformed by mutate_fn).  The fork's snapshot is
        materialized by folding.

        Returns the fork task_id.
        """
        ...


# ── In-Memory Implementation (for tests) ─────────────────────────────

class InMemoryBoardStore:
    """Dict-based board store for unit tests.  No dependencies.

    Implements the full BoardStore protocol including durability
    contract properties (monotonic seq, deterministic replay).
    """

    def __init__(self) -> None:
        # task_id → list of events (ordered by seq)
        self._events: dict[str, list[dict[str, Any]]] = {}
        # task_id → {entry_id: BoardEntry}
        self._entries: dict[str, dict[str, BoardEntry]] = {}
        # task_id → next seq counter
        self._seq_counters: dict[str, int] = {}
        # task_id → metadata dict
        self._meta: dict[str, dict[str, Any]] = {}
        # task_id → mutation id → event
        self._mutation_events: dict[str, dict[str, dict[str, Any]]] = {}
        # task_id → {entry_id: salience_score}
        self._salience: dict[str, dict[str, float]] = {}

    async def append_event(
        self, task_id: str, event: dict[str, Any]
    ) -> int:
        if task_id not in self._events:
            self._events[task_id] = []
        self._events[task_id].append(event)
        payload = event.get("payload", {})
        mutation_id = (
            payload.get("_mutation_id") if isinstance(payload, dict) else None
        )
        if mutation_id:
            self._mutation_events.setdefault(task_id, {})[
                str(mutation_id)
            ] = event
        seq = event.get("seq", 0)
        return seq

    async def get_snapshot(
        self, task_id: str
    ) -> dict[str, BoardEntry]:
        return dict(self._entries.get(task_id, {}))

    async def upsert_entry(
        self, task_id: str, entry: BoardEntry
    ) -> None:
        if task_id not in self._entries:
            self._entries[task_id] = {}
        self._entries[task_id][entry.id] = entry

    async def remove_entry(
        self, task_id: str, entry_id: str
    ) -> None:
        entries = self._entries.get(task_id, {})
        if entry_id in entries:
            entries[entry_id].status = "removed"
            entries[entry_id].updated_at = (
                datetime.now(UTC).isoformat()
            )

    async def get_entry(
        self, task_id: str, entry_id: str
    ) -> BoardEntry | None:
        return self._entries.get(task_id, {}).get(entry_id)

    async def get_events(
        self, task_id: str, until_seq: int | None = None
    ) -> list[dict[str, Any]]:
        events = self._events.get(task_id, [])
        if until_seq is not None:
            events = [e for e in events if e.get("seq", 0) <= until_seq]
        return list(events)

    async def get_event_by_mutation_id(
        self, task_id: str, mutation_id: str,
    ) -> dict[str, Any] | None:
        return self._mutation_events.get(task_id, {}).get(mutation_id)

    async def get_next_seq(self, task_id: str) -> int:
        counter = self._seq_counters.get(task_id, 0) + 1
        self._seq_counters[task_id] = counter
        return counter

    async def set_meta(self, task_id: str, **fields: Any) -> None:
        if task_id not in self._meta:
            self._meta[task_id] = {}
        self._meta[task_id].update(fields)

    async def get_meta(self, task_id: str) -> dict[str, Any]:
        return dict(self._meta.get(task_id, {}))

    async def set_salience(
        self, task_id: str, entry_id: str, score: float
    ) -> None:
        if task_id not in self._salience:
            self._salience[task_id] = {}
        self._salience[task_id][entry_id] = score
        # Also update the entry's salience field
        entry = self._entries.get(task_id, {}).get(entry_id)
        if entry:
            entry.salience = score

    async def entry_exists(
        self, task_id: str, entry_id: str
    ) -> bool:
        return entry_id in self._entries.get(task_id, {})

    async def get_private_snapshot(
        self, task_id: str, space: str,
    ) -> dict[str, BoardEntry]:
        """Get entries scoped to a private space (doc 05 §4).

        Filters the main entry store by the space field.
        """
        all_entries = self._entries.get(task_id, {})
        return {
            eid: entry
            for eid, entry in all_entries.items()
            if entry.space == space and entry.status != "removed"
        }

    async def archive_space(
        self,
        task_id: str,
        space: str,
        mutation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Archive a private space: return its events and wipe entries.

        Returns the archived events list for SQLite persistence.
        Removes entries with matching space from the live snapshot.
        """
        # Collect events for this space before the archive marker.
        archived: list[dict[str, Any]] = []
        for event in self._events.get(task_id, []):
            payload = event.get("payload", {})
            if payload.get("space") == space:
                archived.append(copy.deepcopy(event))

        seq = await self.get_next_seq(task_id)
        await self.append_event(
            task_id,
            make_event(
                task_id=task_id,
                seq=seq,
                actor="control_unit",
                event_type="space_archived",
                payload={
                    "space": space,
                    "_mutation_id": mutation_id,
                },
            ),
        )

        # Remove entries with matching space from snapshot
        entries = self._entries.get(task_id, {})
        to_remove = [
            eid for eid, entry in entries.items()
            if entry.space == space
        ]
        for eid in to_remove:
            del entries[eid]

        return archived

    async def fork(
        self,
        task_id: str,
        at_event_n: int,
        mutate_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> str:
        """Fork the board at event N (doc 04 §5.2).

        Creates a new independent copy.  Events are small JSON;
        a 4-round task has ~20–60 events, so copies are cheap.
        """
        fork_id = f"fork-{uuid.uuid4().hex[:8]}"

        # Copy events 1..N
        source_events = await self.get_events(task_id, until_seq=at_event_n)

        self._events[fork_id] = []
        self._entries[fork_id] = {}
        self._seq_counters[fork_id] = 0
        self._meta[fork_id] = {
            "forked_from": {"task_id": task_id, "at_event": at_event_n},
        }
        self._salience[fork_id] = {}
        self._mutation_events[fork_id] = {}

        for event in source_events:
            # Optionally transform
            if mutate_fn is not None:
                transformed = mutate_fn(copy.deepcopy(event))
                if transformed is None:
                    continue  # dropped by mutate_fn
                event = transformed
            else:
                event = copy.deepcopy(event)

            # Re-assign seq for the fork
            next_seq = self._seq_counters.get(fork_id, 0) + 1
            self._seq_counters[fork_id] = next_seq
            event["seq"] = next_seq
            event["task_id"] = fork_id
            self._events[fork_id].append(event)
            payload = event.get("payload", {})
            mutation_id = (
                payload.get("_mutation_id")
                if isinstance(payload, dict)
                else None
            )
            if mutation_id:
                self._mutation_events[fork_id][str(mutation_id)] = event

        # Fold events to materialize snapshot
        await self._fold_events(fork_id)

        return fork_id

    async def _fold_events(self, task_id: str) -> None:
        """Materialize snapshot by folding events in seq order.

        This is the replay/recovery path: folding events ordered by
        seq reconstructs exactly the same board_entries snapshot
        (durability contract, doc 04 §5.1).
        """
        self._entries[task_id] = {}
        events = sorted(
            self._events.get(task_id, []),
            key=lambda e: e.get("seq", 0),
        )

        for event in events:
            event_type = event.get("event_type", "")
            payload = event.get("payload", {})

            if event_type == "entry_added":
                entry = entry_from_dict(payload)
                self._entries[task_id][entry.id] = entry

            elif event_type == "entry_removed":
                entry_id = event.get("entry_id") or payload.get("entry_id")
                if entry_id and entry_id in self._entries.get(task_id, {}):
                    self._entries[task_id][entry_id].status = "removed"

            elif event_type == "entry_status_changed":
                entry_id = event.get("entry_id") or payload.get("entry_id")
                new_status = payload.get("status", "open")
                if entry_id and entry_id in self._entries.get(task_id, {}):
                    self._entries[task_id][entry_id].status = new_status

            elif event_type == "entry_salience_changed":
                entry_id = event.get("entry_id") or payload.get("entry_id")
                if entry_id and entry_id in self._entries.get(task_id, {}):
                    self._entries[task_id][entry_id].salience = float(
                        payload.get("salience", 0.0)
                    )

            elif event_type == "space_archived":
                space = payload.get("space")
                if space:
                    self._entries[task_id] = {
                        entry_id: entry
                        for entry_id, entry in self._entries[task_id].items()
                        if entry.space != space
                    }

            # genesis, entry_rejected, etc. don't modify the snapshot


class SqliteRedisBoardStore(InMemoryBoardStore):
    """SQLite-backed classic board with an optional Redis view hook.

    SQLite stores the authoritative event log and control metadata. The
    inherited dictionaries provide the hot materialized view for one daemon.
    The existing persistence hook mirrors that view into Redis for the UI.
    """

    def __init__(self, lease_token: str | None = None) -> None:
        super().__init__()
        self._lease_token = lease_token
        self._loaded_tasks: set[str] = set()
        self._load_locks: dict[str, asyncio.Lock] = {}

    async def load_task(self, task_id: str) -> None:
        """Replay one task from SQLite into the hot materialized view."""
        await self._ensure_loaded(task_id)

    async def import_snapshot(
        self,
        task_id: str,
        entries: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Import one legacy Redis snapshot into the SQLite event log."""
        await self._ensure_loaded(task_id)
        if self._events.get(task_id):
            return 0
        events: list[dict[str, Any]] = []
        imported_entries: dict[str, BoardEntry] = {}
        next_seq = 0
        for raw in sorted(
            entries,
            key=lambda item: (
                int(item.get("seq", 0) or 0),
                str(item.get("id", "")),
            ),
        ):
            try:
                entry = entry_from_dict(raw)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Skipped invalid legacy board entry for task %s",
                    task_id,
                )
                continue
            seq = int(raw.get("seq", 0) or 0)
            if seq <= next_seq:
                seq = next_seq + 1
            next_seq = seq
            events.append(
                make_event(
                    task_id=task_id,
                    seq=seq,
                    actor=entry.author,
                    event_type="entry_added",
                    entry_id=entry.id,
                    payload=entry_to_dict(entry),
                    round_no=entry.round,
                    turn_id=entry.created_by_turn,
                )
            )
            imported_entries[entry.id] = entry

        import database as db

        imported = await db.import_legacy_board_snapshot(
            task_id,
            events,
            [entry_to_dict(entry) for entry in imported_entries.values()],
            dict(meta or {}),
            lease_token=self._lease_token,
        )
        if imported == 0 and events:
            self._loaded_tasks.discard(task_id)
            await self._ensure_loaded(task_id)
            return 0

        self._events[task_id] = [copy.deepcopy(event) for event in events]
        self._entries[task_id] = dict(imported_entries)
        self._seq_counters[task_id] = next_seq
        self._meta[task_id] = dict(meta or {})
        self._salience[task_id] = {
            entry_id: entry.salience
            for entry_id, entry in imported_entries.items()
        }
        self._mutation_events[task_id] = {
            str(event["payload"]["_mutation_id"]): event
            for event in self._events[task_id]
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("_mutation_id")
        }
        return imported

    async def unload_task(self, task_id: str) -> None:
        """Drop one terminal task from the hot view.

        A later access replays the authoritative SQLite event log.
        The caller must exclude concurrent board mutations for this task.
        """
        lock = self._load_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            self._events.pop(task_id, None)
            self._entries.pop(task_id, None)
            self._seq_counters.pop(task_id, None)
            self._meta.pop(task_id, None)
            self._salience.pop(task_id, None)
            self._mutation_events.pop(task_id, None)
            self._loaded_tasks.discard(task_id)
        self._load_locks.pop(task_id, None)

    async def _ensure_loaded(self, task_id: str) -> None:
        if task_id in self._loaded_tasks:
            return
        lock = self._load_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            if task_id in self._loaded_tasks:
                return
            import database as db

            events = await db.get_board_events(task_id)
            self._events[task_id] = [copy.deepcopy(event) for event in events]
            self._entries[task_id] = {}
            self._seq_counters[task_id] = max(
                (int(event.get("seq", 0)) for event in events),
                default=0,
            )
            self._meta[task_id] = await db.get_board_meta(task_id)
            self._salience[task_id] = {}
            self._mutation_events[task_id] = {
                str(event["payload"]["_mutation_id"]): event
                for event in self._events[task_id]
                if isinstance(event.get("payload"), dict)
                and event["payload"].get("_mutation_id")
            }
            await self._fold_events(task_id)
            self._loaded_tasks.add(task_id)

    async def append_event(self, task_id: str, event: dict[str, Any]) -> int:
        await self._ensure_loaded(task_id)
        import database as db

        await db.insert_board_event(
            task_id=task_id,
            seq=int(event.get("seq", 0)),
            round_no=event.get("round"),
            turn_id=event.get("turn_id"),
            actor=str(event.get("actor", "unknown")),
            event_type=str(event.get("event_type", "unknown")),
            entry_id=event.get("entry_id"),
            payload=event.get("payload", {}),
            lease_token=self._lease_token,
        )
        return await super().append_event(task_id, event)

    async def get_snapshot(self, task_id: str) -> dict[str, BoardEntry]:
        await self._ensure_loaded(task_id)
        return await super().get_snapshot(task_id)

    async def upsert_entry(self, task_id: str, entry: BoardEntry) -> None:
        await self._ensure_loaded(task_id)
        import database as db

        await db.upsert_board_entry(
            entry_to_dict(entry), lease_token=self._lease_token,
        )
        await super().upsert_entry(task_id, entry)

    async def remove_entry(self, task_id: str, entry_id: str) -> None:
        await self._ensure_loaded(task_id)
        import database as db

        await db.update_board_entry_status(
            task_id, entry_id, "removed", lease_token=self._lease_token,
        )
        await super().remove_entry(task_id, entry_id)

    async def get_entry(self, task_id: str, entry_id: str) -> BoardEntry | None:
        await self._ensure_loaded(task_id)
        return await super().get_entry(task_id, entry_id)

    async def get_events(
        self, task_id: str, until_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_loaded(task_id)
        return await super().get_events(task_id, until_seq)

    async def get_next_seq(self, task_id: str) -> int:
        await self._ensure_loaded(task_id)
        return await super().get_next_seq(task_id)

    async def set_meta(self, task_id: str, **fields: Any) -> None:
        await self._ensure_loaded(task_id)
        import database as db

        merged = dict(self._meta.get(task_id, {}))
        merged.update(fields)
        await db.upsert_board_meta(
            task_id, merged, lease_token=self._lease_token,
        )
        await super().set_meta(task_id, **fields)

    async def get_meta(self, task_id: str) -> dict[str, Any]:
        await self._ensure_loaded(task_id)
        return await super().get_meta(task_id)

    async def set_salience(
        self, task_id: str, entry_id: str, score: float,
    ) -> None:
        await self._ensure_loaded(task_id)
        import database as db

        await db.update_board_entry_salience(
            task_id, entry_id, score, lease_token=self._lease_token,
        )
        await super().set_salience(task_id, entry_id, score)

    async def entry_exists(self, task_id: str, entry_id: str) -> bool:
        await self._ensure_loaded(task_id)
        return await super().entry_exists(task_id, entry_id)

    async def get_private_snapshot(
        self, task_id: str, space: str,
    ) -> dict[str, BoardEntry]:
        await self._ensure_loaded(task_id)
        return await super().get_private_snapshot(task_id, space)

    async def archive_space(
        self,
        task_id: str,
        space: str,
        mutation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_loaded(task_id)
        archived = await super().archive_space(
            task_id, space, mutation_id=mutation_id,
        )
        import database as db

        await db.delete_board_entries_in_space(
            task_id, space, lease_token=self._lease_token,
        )
        return archived


def make_board_persist_hook(
    blackboard: Any,
) -> Callable[[str, BoardStore], Awaitable[None]]:
    """Build a recompute hook that mirrors the snapshot into Redis.

    The traditional variant keeps its working board in an in-process
    store; this hook fires after every gateway commit batch (seam rule 5)
    and writes the full materialized snapshot to a durable Redis key with
    NO expiry, so the board is never lost — for live OR completed tasks.

    Persistence failures are swallowed: durability mirroring must never
    break the coordination loop.
    """

    async def _hook(task_id: str, store: BoardStore) -> None:
        try:
            snapshot = await store.get_snapshot(task_id)
            meta = await store.get_meta(task_id)
            entries = {
                eid: entry_to_dict(entry)
                for eid, entry in snapshot.items()
            }
            await blackboard.save_board_snapshot(task_id, entries, meta)
        except Exception:
            logger.warning(
                "board persist hook failed for task %s", task_id,
                exc_info=True,
            )

    return _hook


def fold_events_to_snapshot(
    events: list[dict[str, Any]],
) -> dict[str, BoardEntry]:
    """Pure function: fold a list of events into a snapshot.

    Useful for testing replay determinism.
    """
    entries: dict[str, BoardEntry] = {}
    sorted_events = sorted(events, key=lambda e: e.get("seq", 0))

    for event in sorted_events:
        event_type = event.get("event_type", "")
        payload = event.get("payload", {})

        if event_type == "entry_added":
            entry = entry_from_dict(payload)
            entries[entry.id] = entry

        elif event_type == "entry_removed":
            entry_id = event.get("entry_id") or payload.get("entry_id")
            if entry_id and entry_id in entries:
                entries[entry_id].status = "removed"

        elif event_type == "entry_status_changed":
            entry_id = event.get("entry_id") or payload.get("entry_id")
            new_status = payload.get("status", "open")
            if entry_id and entry_id in entries:
                entries[entry_id].status = new_status

        elif event_type == "entry_salience_changed":
            entry_id = event.get("entry_id") or payload.get("entry_id")
            if entry_id and entry_id in entries:
                entries[entry_id].salience = float(payload.get("salience", 0.0))

        elif event_type == "space_archived":
            space = payload.get("space")
            if space:
                entries = {
                    entry_id: entry
                    for entry_id, entry in entries.items()
                    if entry.space != space
                }

    return entries
