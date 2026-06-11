# /opt/bmas/daemon/tests/test_board_store.py
"""Board store tests: append, snapshot, fork, replay (doc 04 §5)."""
from __future__ import annotations

import pytest

from core.board_store import InMemoryBoardStore, make_event, fold_events_to_snapshot
from core.entry import BoardEntry, entry_to_dict


def _make_entry_added_event(
    task_id: str,
    seq: int,
    entry_id: str,
    actor: str = "expert.systems",
    body: str = "test body",
    entry_type: str = "finding",
    round_no: int = 1,
    turn_id: str = "turn-1",
) -> dict:
    """Build an entry_added event with a full entry payload."""
    entry = BoardEntry(
        id=entry_id,
        task_id=task_id,
        type=entry_type,
        author=actor,
        body=body,
        confidence=0.75,
        round=round_no,
        status="open",
    )
    return make_event(
        task_id=task_id,
        seq=seq,
        actor=actor,
        event_type="entry_added",
        entry_id=entry_id,
        payload=entry_to_dict(entry),
        round_no=round_no,
        turn_id=turn_id,
    )


class TestInMemoryBoardStore:
    """Test the InMemoryBoardStore implementation."""

    @pytest.mark.asyncio
    async def test_append_and_get_events(self):
        store = InMemoryBoardStore()
        event = make_event("task-1", 1, "actor", "entry_added")
        await store.append_event("task-1", event)
        events = await store.get_events("task-1")
        assert len(events) == 1
        assert events[0]["seq"] == 1

    @pytest.mark.asyncio
    async def test_seq_monotonicity(self):
        """Sequence numbers are strictly monotonic."""
        store = InMemoryBoardStore()
        seq1 = await store.get_next_seq("task-1")
        seq2 = await store.get_next_seq("task-1")
        seq3 = await store.get_next_seq("task-1")
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

    @pytest.mark.asyncio
    async def test_seq_isolation_per_task(self):
        """Each task has its own sequence counter."""
        store = InMemoryBoardStore()
        seq_a = await store.get_next_seq("task-a")
        seq_b = await store.get_next_seq("task-b")
        assert seq_a == 1
        assert seq_b == 1

    @pytest.mark.asyncio
    async def test_upsert_and_snapshot(self):
        store = InMemoryBoardStore()
        entry = BoardEntry(
            id="e-1", task_id="task-1", type="finding",
            author="expert.x", body="test",
        )
        await store.upsert_entry("task-1", entry)
        snap = await store.get_snapshot("task-1")
        assert "e-1" in snap
        assert snap["e-1"].body == "test"

    @pytest.mark.asyncio
    async def test_remove_entry(self):
        store = InMemoryBoardStore()
        entry = BoardEntry(
            id="e-1", task_id="task-1", type="finding",
            author="expert.x", body="test",
        )
        await store.upsert_entry("task-1", entry)
        await store.remove_entry("task-1", "e-1")
        snap = await store.get_snapshot("task-1")
        assert snap["e-1"].status == "removed"

    @pytest.mark.asyncio
    async def test_get_entry_exists(self):
        store = InMemoryBoardStore()
        entry = BoardEntry(
            id="e-1", task_id="task-1", type="finding",
            author="expert.x", body="test",
        )
        await store.upsert_entry("task-1", entry)
        fetched = await store.get_entry("task-1", "e-1")
        assert fetched is not None
        assert fetched.id == "e-1"

    @pytest.mark.asyncio
    async def test_get_entry_not_exists(self):
        store = InMemoryBoardStore()
        fetched = await store.get_entry("task-1", "nonexistent")
        assert fetched is None

    @pytest.mark.asyncio
    async def test_entry_exists(self):
        store = InMemoryBoardStore()
        assert not await store.entry_exists("task-1", "e-1")
        entry = BoardEntry(
            id="e-1", task_id="task-1", type="finding",
            author="expert.x", body="test",
        )
        await store.upsert_entry("task-1", entry)
        assert await store.entry_exists("task-1", "e-1")

    @pytest.mark.asyncio
    async def test_set_and_get_meta(self):
        store = InMemoryBoardStore()
        await store.set_meta("task-1", phase="executing", round=2)
        meta = await store.get_meta("task-1")
        assert meta["phase"] == "executing"
        assert meta["round"] == 2

    @pytest.mark.asyncio
    async def test_set_salience(self):
        store = InMemoryBoardStore()
        entry = BoardEntry(
            id="e-1", task_id="task-1", type="finding",
            author="expert.x", body="test",
        )
        await store.upsert_entry("task-1", entry)
        await store.set_salience("task-1", "e-1", 0.75)
        fetched = await store.get_entry("task-1", "e-1")
        assert fetched.salience == 0.75

    @pytest.mark.asyncio
    async def test_get_events_until_seq(self):
        store = InMemoryBoardStore()
        for i in range(1, 6):
            event = make_event("task-1", i, "actor", "entry_added")
            await store.append_event("task-1", event)
        events = await store.get_events("task-1", until_seq=3)
        assert len(events) == 3
        assert all(e["seq"] <= 3 for e in events)


class TestFork:
    """Test fork-from-event (doc 04 §5.2)."""

    @pytest.mark.asyncio
    async def test_fork_basic(self):
        """Fork at event N → new board has exactly events 1..N."""
        store = InMemoryBoardStore()

        # Build a board with 5 events
        for i in range(1, 6):
            event = _make_entry_added_event("task-1", i, f"e-{i}")
            await store.append_event("task-1", event)
            entry = BoardEntry(
                id=f"e-{i}", task_id="task-1", type="finding",
                author="expert.x", body=f"body {i}",
            )
            await store.upsert_entry("task-1", entry)
            store._seq_counters["task-1"] = i

        fork_id = await store.fork("task-1", at_event_n=3)

        fork_events = await store.get_events(fork_id)
        assert len(fork_events) == 3
        assert fork_events[-1]["seq"] == 3

    @pytest.mark.asyncio
    async def test_fork_snapshot(self):
        """Fork snapshot matches board state at event N."""
        store = InMemoryBoardStore()

        for i in range(1, 6):
            event = _make_entry_added_event("task-1", i, f"e-{i}")
            await store.append_event("task-1", event)
            entry = BoardEntry(
                id=f"e-{i}", task_id="task-1", type="finding",
                author="expert.x", body=f"body {i}",
            )
            await store.upsert_entry("task-1", entry)
            store._seq_counters["task-1"] = i

        fork_id = await store.fork("task-1", at_event_n=3)
        fork_snap = await store.get_snapshot(fork_id)
        assert len(fork_snap) == 3
        assert "e-1" in fork_snap
        assert "e-2" in fork_snap
        assert "e-3" in fork_snap
        assert "e-4" not in fork_snap

    @pytest.mark.asyncio
    async def test_fork_meta(self):
        """Fork stores forked_from metadata."""
        store = InMemoryBoardStore()
        event = _make_entry_added_event("task-1", 1, "e-1")
        await store.append_event("task-1", event)
        store._seq_counters["task-1"] = 1

        fork_id = await store.fork("task-1", at_event_n=1)
        meta = await store.get_meta(fork_id)
        assert meta["forked_from"]["task_id"] == "task-1"
        assert meta["forked_from"]["at_event"] == 1

    @pytest.mark.asyncio
    async def test_fork_with_mutate_fn(self):
        """Fork with mutate_fn drops specific events."""
        store = InMemoryBoardStore()

        for i in range(1, 6):
            actor = "critic" if i == 3 else "expert.x"
            event = _make_entry_added_event(
                "task-1", i, f"e-{i}", actor=actor,
                entry_type="critique" if i == 3 else "finding",
            )
            await store.append_event("task-1", event)
            store._seq_counters["task-1"] = i

        # Drop all critic events
        def drop_critic(event):
            if event.get("actor") == "critic":
                return None
            return event

        fork_id = await store.fork("task-1", at_event_n=5, mutate_fn=drop_critic)
        fork_events = await store.get_events(fork_id)
        assert len(fork_events) == 4  # 5 - 1 critic event
        fork_snap = await store.get_snapshot(fork_id)
        assert "e-3" not in fork_snap  # critic entry was dropped

    @pytest.mark.asyncio
    async def test_fork_with_removal(self):
        """Fork preserves removal status from events."""
        store = InMemoryBoardStore()

        # Add 3 entries
        for i in range(1, 4):
            event = _make_entry_added_event("task-1", i, f"e-{i}")
            await store.append_event("task-1", event)
            store._seq_counters["task-1"] = i

        # Remove entry 2 (seq 4)
        remove_event = make_event(
            "task-1", 4, "cleaner", "entry_removed",
            entry_id="e-2", payload={"entry_id": "e-2", "reason": "cleanup"},
        )
        await store.append_event("task-1", remove_event)
        store._seq_counters["task-1"] = 4

        fork_id = await store.fork("task-1", at_event_n=4)
        fork_snap = await store.get_snapshot(fork_id)
        assert fork_snap["e-2"].status == "removed"
        assert fork_snap["e-1"].status == "open"


class TestFoldEvents:
    """Test the fold_events_to_snapshot pure function."""

    def test_fold_empty(self):
        assert fold_events_to_snapshot([]) == {}

    def test_fold_add_entries(self):
        events = [
            _make_entry_added_event("task-1", 1, "e-1"),
            _make_entry_added_event("task-1", 2, "e-2"),
        ]
        snapshot = fold_events_to_snapshot(events)
        assert len(snapshot) == 2
        assert "e-1" in snapshot
        assert "e-2" in snapshot

    def test_fold_with_removal(self):
        events = [
            _make_entry_added_event("task-1", 1, "e-1"),
            _make_entry_added_event("task-1", 2, "e-2"),
            make_event("task-1", 3, "cleaner", "entry_removed",
                       entry_id="e-1",
                       payload={"entry_id": "e-1", "reason": "cleanup"}),
        ]
        snapshot = fold_events_to_snapshot(events)
        assert snapshot["e-1"].status == "removed"
        assert snapshot["e-2"].status == "open"

    def test_fold_with_status_change(self):
        events = [
            _make_entry_added_event("task-1", 1, "e-1"),
            make_event("task-1", 2, "decider", "entry_status_changed",
                       entry_id="e-1",
                       payload={"entry_id": "e-1", "status": "superseded"}),
        ]
        snapshot = fold_events_to_snapshot(events)
        assert snapshot["e-1"].status == "superseded"

    def test_fold_determinism(self):
        """Folding the same events twice produces identical snapshots."""
        events = [
            _make_entry_added_event("task-1", i, f"e-{i}")
            for i in range(1, 11)
        ]
        snap1 = fold_events_to_snapshot(events)
        snap2 = fold_events_to_snapshot(events)
        assert list(snap1.keys()) == list(snap2.keys())
        for k in snap1:
            assert snap1[k].body == snap2[k].body
            assert snap1[k].status == snap2[k].status
