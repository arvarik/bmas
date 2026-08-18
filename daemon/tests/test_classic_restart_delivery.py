"""Restart and delivery matrix for the classic board."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import database as db
from core.board_store import InMemoryBoardStore, SqliteRedisBoardStore
from core.event_emitter import InMemoryEventEmitter
from core.gateway import BoardGateway
from core.orchestrator import Orchestrator


class LockedOnceStore(InMemoryBoardStore):
    """Fail the first event commit before durable state changes."""

    def __init__(self) -> None:
        super().__init__()
        self.locked = True

    async def append_event(self, task_id, event):
        if self.locked:
            self.locked = False
            raise RuntimeError("database is locked")
        return await super().append_event(task_id, event)


@pytest.mark.asyncio
async def test_request_loss_before_commit_retries_without_duplicate_entry():
    store = LockedOnceStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    proposed = [{
        "type": "finding",
        "body": "durable evidence",
        "_mutation_id": "activation-1:0",
    }]

    with pytest.raises(RuntimeError, match="locked"):
        await gateway.append(
            "task-before-commit",
            "expert.alpha",
            ["finding_writer"],
            proposed,
            turn_id="activation-1",
        )
    committed = await gateway.append(
        "task-before-commit",
        "expert.alpha",
        ["finding_writer"],
        proposed,
        turn_id="activation-1",
    )

    assert len(committed) == 1
    assert len(await store.get_snapshot("task-before-commit")) == 1
    assert len(await store.get_events("task-before-commit")) == 1


@pytest.mark.asyncio
async def test_crash_after_event_commit_replays_snapshot_and_deduplicates(
    tmp_path, monkeypatch,
):
    db_path = str(tmp_path / "commit-boundary.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    await db.init_db()
    await db.create_task("task-after-event", "test", "test")
    original_upsert = db.upsert_board_entry
    failures = 0

    async def crash_after_event(entry, lease_token=None):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise RuntimeError("simulated daemon termination")
        return await original_upsert(entry, lease_token=lease_token)

    monkeypatch.setattr(db, "upsert_board_entry", crash_after_event)
    first_store = SqliteRedisBoardStore()
    first_gateway = BoardGateway(first_store, InMemoryEventEmitter())
    proposed = [{
        "type": "finding",
        "body": "event committed before snapshot",
        "_mutation_id": "activation-after-event:0",
    }]

    with pytest.raises(RuntimeError, match="termination"):
        await first_gateway.append(
            "task-after-event",
            "expert.alpha",
            ["finding_writer"],
            proposed,
            turn_id="activation-after-event",
        )

    monkeypatch.setattr(db, "upsert_board_entry", original_upsert)
    resumed_store = SqliteRedisBoardStore()
    await resumed_store.load_task("task-after-event")
    replayed = await resumed_store.get_snapshot("task-after-event")
    resumed_gateway = BoardGateway(resumed_store, InMemoryEventEmitter())
    duplicate = await resumed_gateway.append(
        "task-after-event",
        "expert.alpha",
        ["finding_writer"],
        proposed,
        turn_id="activation-after-event",
    )

    assert [entry.body for entry in replayed.values()] == [
        "event committed before snapshot"
    ]
    assert len(duplicate) == 1
    assert len(await db.get_board_events("task-after-event")) == 1


@pytest.mark.asyncio
async def test_redis_loss_after_commit_preserves_board_and_retry_identity():
    store = InMemoryBoardStore()

    async def failing_hook(task_id, board_store):
        raise ConnectionError("Redis snapshot write failed")

    gateway = BoardGateway(
        store,
        InMemoryEventEmitter(),
        recompute_hooks=[failing_hook],
    )
    proposed = [{
        "type": "finding",
        "body": "SQLite-equivalent board state survives Redis loss",
        "_mutation_id": "activation-redis-loss:0",
    }]

    first = await gateway.append(
        "task-redis-loss",
        "expert.alpha",
        ["finding_writer"],
        proposed,
        turn_id="activation-redis-loss",
    )
    second = await gateway.append(
        "task-redis-loss",
        "expert.alpha",
        ["finding_writer"],
        proposed,
        turn_id="activation-redis-loss",
    )

    assert first[0].id == second[0].id
    assert len(await store.get_events("task-redis-loss")) == 1
    assert len(await store.get_snapshot("task-redis-loss")) == 1


@pytest.mark.asyncio
async def test_acknowledgment_loss_retries_one_external_action_and_one_cost(
    tmp_path, monkeypatch,
):
    db_path = str(tmp_path / "ack-loss.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    await db.init_db()
    await db.create_task("task-ack-loss", "test", "test")
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    action_counts: Counter[str] = Counter()
    cached: dict[str, dict] = {}

    async def post(url, **kwargs):
        activation_id = kwargs["json"]["activation_id"]
        if activation_id not in cached:
            action_counts[activation_id] += 1
            cached[activation_id] = {
                "status": "completed",
                "result": "answer",
                "usage": {
                    "model": "test-model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                },
            }
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = dict(cached[activation_id])
        return response

    orchestrator = object.__new__(Orchestrator)
    orchestrator.http = SimpleNamespace(post=post)
    orchestrator._safe_log = AsyncMock()
    orchestrator._assert_dispatch_lease = AsyncMock()
    activation_id = "activation-ack-loss"

    for _ in range(2):
        result = await orchestrator._dispatch_turn(
            role="expert",
            task_id="task-ack-loss",
            description="question",
            persona="persona",
            model="test-model",
            actor="expert.alpha",
            turn_id=activation_id,
            activation_id=activation_id,
            endpoint="http://node-a:8000",
        )
        assert result["status"] == "completed"

    turns = await db.get_turns("task-ack-loss")
    async with aiosqlite.connect(db_path) as connection:
        cost_row = await connection.execute(
            "SELECT COUNT(*) FROM cost_entries WHERE task_id = ?",
            ("task-ack-loss",),
        )
        cost_count = (await cost_row.fetchone())[0]

    assert action_counts == {activation_id: 1}
    assert len(turns) == 1
    assert turns[0]["status"] == "completed"
    assert cost_count == 1


@pytest.mark.asyncio
async def test_partial_round_checkpoint_restores_only_unfinished_activation():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    state = {
        "round": 4,
        "status": "active",
        "rationale": "resume",
        "selection_source": "checkpoint",
        "phase": "Debate",
        "activations": [
            {
                "actor": "expert.alpha",
                "role": "expert",
                "model": "model-a",
                "node_endpoint": "http://node-a:8000",
                "profile": "expert",
                "activation_id": "activation-a",
            },
            {
                "actor": "critic",
                "role": "critic",
                "model": "model-b",
                "node_endpoint": "http://node-b:8000",
                "profile": "critic",
                "activation_id": "activation-b",
            },
        ],
        "completed": {"activation-a": "completed"},
    }
    await gateway.set_meta("task-partial", round=4, round_state=state)

    variant = SimpleNamespace(store=store)
    from core.variants.traditional import TraditionalVariant

    restored = await TraditionalVariant.restore_active_round(
        variant, "task-partial"
    )

    assert restored is not None
    assert [activation.activation_id for activation in restored.activations] == [
        "activation-b"
    ]
