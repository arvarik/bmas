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

import copy
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from core.entry import BoardEntry, entry_to_dict, entry_from_dict


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
        "created_at": datetime.now(timezone.utc).isoformat(),
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
        # task_id → {entry_id: salience_score}
        self._salience: dict[str, dict[str, float]] = {}

    async def append_event(
        self, task_id: str, event: dict[str, Any]
    ) -> int:
        if task_id not in self._events:
            self._events[task_id] = []
        self._events[task_id].append(event)
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
                datetime.now(timezone.utc).isoformat()
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

            # genesis, entry_rejected, etc. don't modify the snapshot


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

    return entries
