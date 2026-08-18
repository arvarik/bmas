# /opt/bmas/daemon/tests/test_board_store.py
"""Board store tests: append, snapshot, fork, replay (doc 04 §5)."""
from __future__ import annotations

import sqlite3

import pytest

from core.board_store import (
    InMemoryBoardStore,
    SqliteRedisBoardStore,
    fold_events_to_snapshot,
    make_event,
)
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
    async def test_mutation_id_lookup_uses_the_event_index(self):
        store = InMemoryBoardStore()
        event = make_event(
            "task-1", 1, "actor", "entry_rejected",
            payload={"_mutation_id": "turn-1:0"},
        )
        await store.append_event("task-1", event)

        assert await store.get_event_by_mutation_id(
            "task-1", "turn-1:0",
        ) is event

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


class TestSqliteRedisBoardStore:

    @pytest.mark.asyncio
    async def test_legacy_snapshot_import_becomes_replayable(self, tmp_path, monkeypatch):
        import database as db

        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.db"))
        await db.init_db()
        await db.create_task("task-legacy", "test", "test")
        entry = BoardEntry(
            id="e-4",
            task_id="task-legacy",
            type="finding",
            author="expert.x",
            body="legacy fact",
            round=2,
            salience=0.7,
        )
        store = SqliteRedisBoardStore()

        imported = await store.import_snapshot(
            "task-legacy",
            [{**entry_to_dict(entry), "seq": 4}],
            {"round": 2, "phase": "Debate"},
        )

        resumed = SqliteRedisBoardStore()
        await resumed.load_task("task-legacy")
        snapshot = await resumed.get_snapshot("task-legacy")
        assert imported == 1
        assert snapshot["e-4"].body == "legacy fact"
        assert (await resumed.get_meta("task-legacy"))["round"] == 2
        assert await resumed.get_next_seq("task-legacy") == 5

    @pytest.mark.asyncio
    async def test_restart_replays_entries_and_meta(self, tmp_path, monkeypatch):
        import database as db

        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "board.db"))
        await db.init_db()
        await db.create_task("task-1", "test", "test")

        first = SqliteRedisBoardStore()
        seq = await first.get_next_seq("task-1")
        event = _make_entry_added_event("task-1", seq, "e-1")
        entry = BoardEntry(
            id="e-1",
            task_id="task-1",
            type="finding",
            author="expert.x",
            body="durable fact",
        )
        await first.append_event("task-1", event)
        await first.upsert_entry("task-1", entry)
        await first.set_meta("task-1", round=7, budget_spent=0.25)

        resumed = SqliteRedisBoardStore()
        await resumed.load_task("task-1")
        snapshot = await resumed.get_snapshot("task-1")
        meta = await resumed.get_meta("task-1")

        assert snapshot["e-1"].body == "test body"
        assert meta == {"round": 7, "budget_spent": 0.25}
        assert await resumed.get_next_seq("task-1") == 2

    @pytest.mark.asyncio
    async def test_unload_releases_hot_state_and_later_replays(self, tmp_path, monkeypatch):
        import database as db

        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "unload.db"))
        await db.init_db()
        await db.create_task("task-1", "test", "test")
        store = SqliteRedisBoardStore()
        entry = BoardEntry(
            id="e-1",
            task_id="task-1",
            type="finding",
            author="expert.x",
            body="durable fact",
        )
        await store.append_event(
            "task-1", _make_entry_added_event("task-1", 1, "e-1"),
        )
        await store.upsert_entry("task-1", entry)
        await store.set_meta("task-1", round=2)

        await store.unload_task("task-1")

        assert "task-1" not in store._events
        assert "task-1" not in store._entries
        assert "task-1" not in store._meta
        assert "task-1" not in store._mutation_events
        assert "task-1" not in store._loaded_tasks
        assert "task-1" not in store._load_locks
        replayed = await store.get_snapshot("task-1")
        assert replayed["e-1"].body == "test body"
        assert (await store.get_meta("task-1"))["round"] == 2

    @pytest.mark.asyncio
    async def test_legacy_import_rolls_back_as_one_transaction(
        self, tmp_path, monkeypatch,
    ):
        import database as db

        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "atomic.db"))
        await db.init_db()
        await db.create_task("task-1", "test", "test")
        duplicate_events = [
            make_event("task-1", 1, "expert.x", "entry_added"),
            make_event("task-1", 1, "expert.y", "entry_added"),
        ]

        with pytest.raises(sqlite3.IntegrityError):
            await db.import_legacy_board_snapshot(
                "task-1", duplicate_events, [], {"round": 7},
            )

        assert await db.get_board_events("task-1") == []
        assert await db.get_board_meta("task-1") == {}

    def test_fold_archived_space_removes_private_entries(self):
        private = BoardEntry(
            id="e-1",
            task_id="task-1",
            type="finding",
            author="expert.x",
            body="private",
            space="private:conflict-e-9",
        )
        events = [
            make_event(
                "task-1", 1, "expert.x", "entry_added",
                entry_id="e-1", payload=entry_to_dict(private),
            ),
            make_event(
                "task-1", 2, "control_unit", "space_archived",
                payload={"space": "private:conflict-e-9"},
            ),
        ]
        assert fold_events_to_snapshot(events) == {}
