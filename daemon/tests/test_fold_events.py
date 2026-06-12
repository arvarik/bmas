# /opt/bmas/daemon/tests/test_fold_events.py
"""
Tests for the pure fold_events_to_snapshot function in board_store.py.

Verifies replay determinism: a sequence of board events, when folded in
any seq-sorted order, must produce the same snapshot (durability contract,
doc 04 §5.1).
"""

import os
import sys
import uuid
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.board_store import fold_events_to_snapshot, make_event  # noqa: E402


def _make_add_event(
    task_id: str,
    entry_id: str,
    seq: int,
    author: str = "planner",
    entry_type: str = "finding",
    title: str = "Test Finding",
    body: str = "Test body.",
    round_no: int = 1,
) -> dict:
    """Helper: create an entry_added event."""
    return make_event(
        task_id=task_id,
        seq=seq,
        actor=author,
        event_type="entry_added",
        entry_id=entry_id,
        payload={
            "id": entry_id,
            "task_id": task_id,  # required by entry_from_dict
            "type": entry_type,
            "title": title,
            "body": body,
            "author": author,
            "refs": [],
            "confidence": 0.9,
            "salience": 0.5,
            "seq": seq,
            "round": round_no,
            "status": "open",
            "space": "public",
            "turn_id": "turn-001",
            "created_at": datetime.now(UTC).isoformat(),
        },
        round_no=round_no,
    )


def _make_remove_event(task_id: str, entry_id: str, seq: int) -> dict:
    """Helper: create an entry_removed event."""
    return make_event(
        task_id=task_id,
        seq=seq,
        actor="cleaner",
        event_type="entry_removed",
        entry_id=entry_id,
        payload={"entry_id": entry_id, "reason": "Redundant"},
    )


def _make_status_event(
    task_id: str, entry_id: str, seq: int, new_status: str
) -> dict:
    """Helper: create an entry_status_changed event."""
    return make_event(
        task_id=task_id,
        seq=seq,
        actor="gateway",
        event_type="entry_status_changed",
        entry_id=entry_id,
        payload={"entry_id": entry_id, "status": new_status},
    )


class TestFoldEventsToSnapshot:
    """Fold determinism and correctness tests."""

    def test_empty_events_returns_empty_snapshot(self):
        """No events → empty snapshot."""
        result = fold_events_to_snapshot([])
        assert result == {}

    def test_single_add_event(self):
        """One entry_added event → one entry in snapshot."""
        task_id = "task-fold-1"
        entry_id = str(uuid.uuid4())
        events = [_make_add_event(task_id, entry_id, seq=1)]

        snapshot = fold_events_to_snapshot(events)

        assert len(snapshot) == 1
        assert entry_id in snapshot
        assert snapshot[entry_id].status == "open"
        assert snapshot[entry_id].author == "planner"

    def test_add_then_remove(self):
        """entry_added → entry_removed marks entry as 'removed'."""
        task_id = "task-fold-2"
        entry_id = str(uuid.uuid4())
        events = [
            _make_add_event(task_id, entry_id, seq=1),
            _make_remove_event(task_id, entry_id, seq=2),
        ]

        snapshot = fold_events_to_snapshot(events)

        assert entry_id in snapshot
        assert snapshot[entry_id].status == "removed"

    def test_add_then_status_change(self):
        """entry_added → entry_status_changed updates status."""
        task_id = "task-fold-3"
        entry_id = str(uuid.uuid4())
        events = [
            _make_add_event(task_id, entry_id, seq=1),
            _make_status_event(task_id, entry_id, seq=2, new_status="superseded"),
        ]

        snapshot = fold_events_to_snapshot(events)

        assert snapshot[entry_id].status == "superseded"

    def test_multiple_entries(self):
        """Multiple entries accumulate independently."""
        task_id = "task-fold-4"
        ids = [str(uuid.uuid4()) for _ in range(5)]
        events = [_make_add_event(task_id, eid, seq=i + 1) for i, eid in enumerate(ids)]

        snapshot = fold_events_to_snapshot(events)

        assert len(snapshot) == 5
        for eid in ids:
            assert eid in snapshot
            assert snapshot[eid].status == "open"

    def test_out_of_order_events_sorted_by_seq(self):
        """Events provided out of order are sorted by seq before folding."""
        task_id = "task-fold-5"
        entry_id = str(uuid.uuid4())
        # Provide remove (seq=2) before add (seq=1) — should still be removed
        events = [
            _make_remove_event(task_id, entry_id, seq=2),
            _make_add_event(task_id, entry_id, seq=1),
        ]

        snapshot = fold_events_to_snapshot(events)

        # After correct seq-based sorting: add then remove → removed
        assert snapshot[entry_id].status == "removed"

    def test_remove_nonexistent_entry_is_ignored(self):
        """Removing an entry that was never added does not crash."""
        task_id = "task-fold-6"
        events = [
            _make_remove_event(task_id, "nonexistent-id", seq=1),
        ]

        snapshot = fold_events_to_snapshot(events)
        # No crash, snapshot is empty
        assert len(snapshot) == 0

    def test_status_change_nonexistent_entry_is_ignored(self):
        """Status change for nonexistent entry does not crash."""
        task_id = "task-fold-7"
        events = [
            _make_status_event(task_id, "nonexistent-id", seq=1, new_status="accepted"),
        ]

        snapshot = fold_events_to_snapshot(events)
        assert len(snapshot) == 0

    def test_replay_determinism(self):
        """Re-folding same events produces identical snapshot."""
        task_id = "task-fold-8"
        ids = [str(uuid.uuid4()) for _ in range(3)]
        events = [_make_add_event(task_id, eid, seq=i + 1) for i, eid in enumerate(ids)]
        events.append(_make_remove_event(task_id, ids[1], seq=10))

        snapshot_a = fold_events_to_snapshot(events)
        snapshot_b = fold_events_to_snapshot(events)

        assert set(snapshot_a.keys()) == set(snapshot_b.keys())
        for eid in snapshot_a:
            assert snapshot_a[eid].status == snapshot_b[eid].status

    def test_unknown_event_type_is_ignored(self):
        """Events with unrecognised types are skipped without error."""
        task_id = "task-fold-9"
        events = [
            make_event(task_id, seq=1, actor="something", event_type="pheromone_decayed"),
        ]

        snapshot = fold_events_to_snapshot(events)
        assert snapshot == {}

    def test_accepted_solution_type(self):
        """An 'accepted' solution entry is preserved in snapshot."""
        task_id = "task-fold-10"
        entry_id = str(uuid.uuid4())
        events = [
            _make_add_event(task_id, entry_id, seq=1, entry_type="solution"),
            _make_status_event(task_id, entry_id, seq=2, new_status="accepted"),
        ]

        snapshot = fold_events_to_snapshot(events)
        assert snapshot[entry_id].status == "accepted"
        assert snapshot[entry_id].type == "solution"
