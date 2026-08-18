"""Durable event replay, outbox, and recovery contract tests."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

import database as db
from core.blackboard import Blackboard
from core.event_delivery import (
    dispatch_outbox,
    ensure_system_terminal_event,
    ensure_terminal_event,
    reconcile_terminal_events,
    record_and_publish,
    system_terminal_event_payload,
    task_stream,
    terminal_event_payload,
)
from routes.events import (
    _cursor_gap_response,
    _format_sse,
    _journal_stream,
    _last_event_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
async def journal_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "events.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    await db.init_db()
    return db_path


class FakeRedis:
    """Record publications and optionally simulate a Redis outage."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.messages: list[tuple[str, dict]] = []

    async def publish(self, channel: str, payload: str) -> int:
        if self.failing:
            raise ConnectionError("Redis is unavailable")
        self.messages.append((channel, json.loads(payload)))
        return 1


class FakeRequest:
    """Provide the request methods that the SSE generator reads."""

    def __init__(self, last_event_id: str | None = None) -> None:
        self.headers = (
            {"last-event-id": last_event_id} if last_event_id is not None else {}
        )

    async def is_disconnected(self) -> bool:
        return False


class FakePubSub:
    """Record subscription order for the replay race test."""

    def __init__(self, on_subscribe: Callable[[], Awaitable[None]]) -> None:
        self.on_subscribe = on_subscribe
        self.closed = False

    async def subscribe(self, _channel: str) -> None:
        await self.on_subscribe()

    async def get_message(self, **_kwargs):
        return None

    async def unsubscribe(self, _channel: str) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class TransientPubSub(FakePubSub):
    """Return one transient system health event after subscription."""

    def __init__(self) -> None:
        async def no_op() -> None:
            return None

        super().__init__(no_op)
        self.sent = False

    async def get_message(self, **_kwargs):
        if self.sent:
            return None
        self.sent = True
        return {
            "type": "message",
            "data": json.dumps(
                {
                    "event": "daemon-status",
                    "data": {"status": "degraded"},
                }
            ),
        }


class PubSubRedis:
    """Return one controlled Pub/Sub object."""

    def __init__(self, pubsub: FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> FakePubSub:
        return self._pubsub


async def _create_task(task_id: str) -> None:
    await db.create_task(task_id, "test", "test")


@pytest.mark.asyncio
async def test_replay_uses_durable_cursor_order_across_streams(journal_db):
    await _create_task("task-order")
    first = await db.append_delivery_event(
        task_stream("task-order"), "phase", {"value": 1}, task_id="task-order"
    )
    await db.append_delivery_event("system", "task-started", {"task_id": "other"})
    second = await db.append_delivery_event(
        task_stream("task-order"), "turn", {"value": 2}, task_id="task-order"
    )

    replay = await db.get_delivery_events_after(
        task_stream("task-order"), int(first["cursor"]), limit=10
    )

    assert [event["cursor"] for event in replay] == [second["cursor"]]
    assert int(second["cursor"]) > int(first["cursor"])
    assert _format_sse(second).startswith(f"id: {second['cursor']}\nevent: turn\n")


@pytest.mark.asyncio
async def test_idempotency_key_returns_one_event_and_rejects_new_content(journal_db):
    await _create_task("task-duplicate")
    first = await db.append_delivery_event(
        task_stream("task-duplicate"),
        "phase",
        {"phase": "work"},
        task_id="task-duplicate",
        idempotency_key="phase-work",
    )
    duplicate = await db.append_delivery_event(
        task_stream("task-duplicate"),
        "phase",
        {"phase": "work"},
        task_id="task-duplicate",
        idempotency_key="phase-work",
    )

    assert duplicate["cursor"] == first["cursor"]
    assert len(
        await db.get_delivery_events_after(task_stream("task-duplicate"), 0)
    ) == 1
    task = await db.get_task("task-duplicate")
    health = await db.get_event_delivery_health("task-duplicate")
    assert task is not None
    assert task["state_revision"] == 1
    assert health["unpublished_events"] == 1
    assert health["outbox_backlog"] == 1
    with pytest.raises(db.EventIdempotencyConflict):
        await db.append_delivery_event(
            task_stream("task-duplicate"),
            "phase",
            {"phase": "different"},
            task_id="task-duplicate",
            idempotency_key="phase-work",
        )


@pytest.mark.asyncio
async def test_redis_loss_keeps_event_and_retry_clears_backlog(journal_db):
    await _create_task("task-redis-loss")
    redis = FakeRedis(failing=True)

    event = await record_and_publish(
        redis,
        task_stream("task-redis-loss"),
        "turn",
        {"turn": 1},
        task_id="task-redis-loss",
    )

    replay = await db.get_delivery_events_after(task_stream("task-redis-loss"), 0)
    health = await db.get_event_delivery_health("task-redis-loss")
    assert [row["cursor"] for row in replay] == [event["cursor"]]
    assert health["status"] == "recovering"
    assert health["publish_failures"] == 1

    redis.failing = False
    assert await dispatch_outbox(redis) == 1
    assert redis.messages[0][1]["cursor"] == event["cursor"]
    assert (await db.get_event_delivery_health("task-redis-loss"))["status"] == "healthy"


@pytest.mark.asyncio
async def test_concurrent_dispatchers_do_not_publish_duplicates(journal_db):
    await _create_task("task-concurrent")
    await db.append_delivery_event(
        task_stream("task-concurrent"),
        "turn",
        {"turn": 1},
        task_id="task-concurrent",
    )
    redis = FakeRedis()

    delivered = await asyncio.gather(
        dispatch_outbox(redis),
        dispatch_outbox(redis),
    )

    assert sum(delivered) == 1
    assert len(redis.messages) == 1


@pytest.mark.asyncio
async def test_outbox_capacity_refills_in_strict_cursor_order(
    journal_db, monkeypatch
):
    monkeypatch.setattr(db, "MAX_OUTBOX_BACKLOG", 2)
    monkeypatch.setattr(db, "OUTBOX_OVERLOAD_THRESHOLD", 3)
    await _create_task("task-capacity")
    events = []
    for index, event_type in enumerate(("phase", "turn", "cost", "complete"), 1):
        events.append(
            await db.append_delivery_event(
                task_stream("task-capacity"),
                event_type,
                {"index": index},
                task_id="task-capacity",
            )
        )

    overloaded = await db.get_event_delivery_health("task-capacity")
    assert overloaded["status"] == "overloaded"
    assert overloaded["overloaded"] is True

    first_batch = await db.get_pending_delivery_events(limit=10)
    assert [event["cursor"] for event in first_batch] == [
        events[0]["cursor"],
        events[1]["cursor"],
    ]
    for event in first_batch:
        await db.mark_delivery_published(int(event["cursor"]))

    second_batch = await db.get_pending_delivery_events(limit=10)
    assert [event["cursor"] for event in second_batch] == [
        events[2]["cursor"],
        events[3]["cursor"],
    ]


@pytest.mark.asyncio
async def test_terminal_reconciliation_is_idempotent(journal_db):
    await _create_task("task-terminal")
    await db.update_task_status("task-terminal", status="running")
    await db.fail_task("task-terminal", "worker stopped")
    task = await db.get_task("task-terminal")
    assert task is not None
    redis = FakeRedis()

    first = await record_and_publish(
        redis,
        task_stream("task-terminal"),
        "error",
        {"error": "live path payload"},
        task_id="task-terminal",
    )
    second = await ensure_terminal_event(redis, task)

    assert first["cursor"] == second["cursor"]
    assert first["data"] == second["data"] == terminal_event_payload(task)
    assert len(redis.messages) == 1
    await ensure_system_terminal_event(redis, task)
    assert await reconcile_terminal_events(redis) == 0
    events = await db.get_delivery_events_after(task_stream("task-terminal"), 0)
    assert [event["event_type"] for event in events] == ["error"]


@pytest.mark.asyncio
async def test_terminal_reconciliation_keeps_the_first_canonical_payload(journal_db):
    await _create_task("task-terminal-stable")
    await db.update_task_status("task-terminal-stable", status="running")
    await db.complete_task(
        "task-terminal-stable",
        "answer",
        '{"rounds_completed":2,"terminated_by":"consensus"}',
    )
    first_task = await db.get_task("task-terminal-stable")
    assert first_task is not None
    redis = FakeRedis()
    first = await ensure_terminal_event(redis, first_task)

    await db.update_task_cost_totals("task-terminal-stable")
    async with db._connect() as connection:
        await connection.execute(
            "UPDATE tasks SET total_tokens = 999 WHERE id = ?",
            ("task-terminal-stable",),
        )
        await connection.commit()
    changed_task = await db.get_task("task-terminal-stable")
    assert changed_task is not None
    second = await ensure_terminal_event(redis, changed_task)

    assert second["cursor"] == first["cursor"]
    assert second["data"] == first["data"]
    assert second["data"]["total_tokens"] != 999
    assert len(redis.messages) == 1


@pytest.mark.asyncio
async def test_system_terminal_reconciliation_is_canonical_and_idempotent(
    journal_db,
):
    await _create_task("task-system-terminal")
    await db.update_task_status("task-system-terminal", status="running")
    await db.complete_task(
        "task-system-terminal",
        "answer",
        '{"rounds_completed":3,"terminated_by":"consensus"}',
    )
    task = await db.get_task("task-system-terminal")
    assert task is not None
    redis = FakeRedis()

    first = await ensure_system_terminal_event(redis, task)
    second = await ensure_system_terminal_event(redis, task)

    assert second["cursor"] == first["cursor"]
    assert second["data"] == first["data"] == system_terminal_event_payload(task)
    system_events = await db.get_delivery_events_after("system", 0)
    assert [event["event_type"] for event in system_events] == ["task-completed"]
    assert system_events[0]["task_id"] == "task-system-terminal"
    assert len(redis.messages) == 1


@pytest.mark.asyncio
async def test_reconciliation_repairs_system_event_after_task_event_exists(
    journal_db,
):
    await _create_task("task-system-repair")
    await db.update_task_status("task-system-repair", status="running")
    await db.fail_task("task-system-repair", "worker stopped")
    task = await db.get_task("task-system-repair")
    assert task is not None
    redis = FakeRedis()
    await ensure_terminal_event(redis, task)

    assert await reconcile_terminal_events(redis) == 1
    assert await reconcile_terminal_events(redis) == 0
    events = await db.get_delivery_events_after("system", 0)
    assert len(events) == 1
    assert events[0]["data"] == system_terminal_event_payload(task)


@pytest.mark.asyncio
async def test_sse_subscribes_before_replay_and_returns_raced_event(
    journal_db, monkeypatch
):
    await _create_task("task-race")
    first = await db.append_delivery_event(
        task_stream("task-race"), "phase", {"index": 1}, task_id="task-race"
    )
    second = await db.append_delivery_event(
        task_stream("task-race"), "turn", {"index": 2}, task_id="task-race"
    )
    order: list[str] = []

    async def on_subscribe() -> None:
        order.append("subscribe")
        await db.append_delivery_event(
            task_stream("task-race"), "cost", {"index": 3}, task_id="task-race"
        )

    original_replay = db.get_delivery_events_after

    async def observed_replay(*args, **kwargs):
        order.append("replay")
        return await original_replay(*args, **kwargs)

    monkeypatch.setattr(db, "get_delivery_events_after", observed_replay)
    pubsub = FakePubSub(on_subscribe)
    stream = _journal_stream(
        FakeRequest(),
        PubSubRedis(pubsub),
        stream=task_stream("task-race"),
        channel="bmas:events:task-race",
        after_cursor=int(first["cursor"]),
    )

    first_message = await anext(stream)
    second_message = await anext(stream)
    await stream.aclose()

    assert order[:2] == ["subscribe", "replay"]
    assert f"id: {second['cursor']}" in first_message
    assert "event: cost" in second_message
    assert pubsub.closed is True


@pytest.mark.asyncio
async def test_fresh_snapshot_boundary_replays_event_created_during_load(journal_db):
    await _create_task("task-snapshot-race")
    first = await db.append_delivery_event(
        task_stream("task-snapshot-race"),
        "phase",
        {"index": 1},
        task_id="task-snapshot-race",
    )

    async def no_op() -> None:
        return None

    async def load_snapshot() -> list[tuple[str, dict]]:
        await db.append_delivery_event(
            task_stream("task-snapshot-race"),
            "turn",
            {"index": 2},
            task_id="task-snapshot-race",
        )
        return [("initial_state", {"task": {"id": "task-snapshot-race"}})]

    stream = _journal_stream(
        FakeRequest(),
        PubSubRedis(FakePubSub(no_op)),
        stream=task_stream("task-snapshot-race"),
        channel="bmas:events:task-snapshot-race",
        after_cursor=0,
        snapshot_loader=load_snapshot,
    )

    snapshot = await anext(stream)
    raced_event = await anext(stream)
    await stream.aclose()

    assert f"id: {first['cursor']}" in snapshot
    assert "event: turn" in raced_event


@pytest.mark.asyncio
async def test_fresh_snapshot_replays_event_created_during_subscription(journal_db):
    await _create_task("task-subscribe-race")

    async def on_subscribe() -> None:
        await db.append_delivery_event(
            task_stream("task-subscribe-race"),
            "trace",
            {"index": 1},
            task_id="task-subscribe-race",
        )

    async def load_snapshot() -> list[tuple[str, dict]]:
        return [("initial_state", {"task": {"id": "task-subscribe-race"}})]

    stream = _journal_stream(
        FakeRequest(),
        PubSubRedis(FakePubSub(on_subscribe)),
        stream=task_stream("task-subscribe-race"),
        channel="bmas:events:task-subscribe-race",
        after_cursor=0,
        snapshot_loader=load_snapshot,
    )

    snapshot = await anext(stream)
    raced_event = await anext(stream)
    await stream.aclose()

    assert snapshot.startswith("event: initial_state")
    assert "event: trace" in raced_event


@pytest.mark.asyncio
async def test_trace_batch_idempotency_saves_one_event_per_identity(journal_db):
    await _create_task("task-trace-batch")
    board = Blackboard()
    redis = FakeRedis()
    board.redis = redis  # type: ignore[assignment]
    traces = [
        ("trace", {"seq": 1}, "trace:turn-1:1"),
        ("trace", {"seq": 2}, "trace:turn-1:2"),
        ("cost", {"turn_id": "turn-1"}, "trace-cost:turn-1"),
    ]

    for _ in range(2):
        for event_type, data, key in traces:
            await board.publish_event(
                "task-trace-batch",
                event_type,
                data,
                idempotency_key=key,
            )

    events = await db.get_delivery_events_after(task_stream("task-trace-batch"), 0)
    assert [event["idempotency_key"] for event in events] == [
        "trace:turn-1:1",
        "trace:turn-1:2",
        "trace-cost:turn-1",
    ]
    assert len(redis.messages) == 3


@pytest.mark.asyncio
async def test_system_stream_forwards_transient_health_without_cursor(journal_db):
    stream = _journal_stream(
        FakeRequest(),
        PubSubRedis(TransientPubSub()),
        stream="system",
        channel="bmas:events:system",
        after_cursor=0,
        forward_transient=True,
    )

    message = await anext(stream)
    await stream.aclose()

    assert message.startswith("event: daemon-status\n")
    assert "id:" not in message
    assert '"status":"degraded"' in message


@pytest.mark.asyncio
async def test_health_samples_use_transient_redis_without_journal_growth(journal_db):
    board = Blackboard()
    redis = FakeRedis()
    board.redis = redis  # type: ignore[assignment]

    await board.publish_system_event("daemon-status", {"status": "healthy"})
    await board.publish_system_event("agent-health", {"expert": {"alive": True}})

    assert await db.get_delivery_events_after("system", 0) == []
    assert [message[1]["event"] for message in redis.messages] == [
        "daemon-status",
        "agent-health",
    ]


@pytest.mark.asyncio
async def test_cursor_and_gap_contracts(journal_db):
    request = FakeRequest("7")
    assert _last_event_id(request) == 7
    with pytest.raises(ValueError):
        _last_event_id(FakeRequest("not-a-cursor"))

    await _create_task("task-gap")
    event = await db.append_delivery_event(
        task_stream("task-gap"), "phase", {"phase": "work"}, task_id="task-gap"
    )
    response = await _cursor_gap_response(
        task_stream("task-gap"), int(event["cursor"]) + 10
    )
    assert response is not None
    assert response.status_code == 409
    assert json.loads(response.body)["recovery"].startswith("Reconnect without")


@pytest.mark.asyncio
async def test_payload_limit_rejects_oversized_events(journal_db, monkeypatch):
    monkeypatch.setattr(db, "MAX_EVENT_PAYLOAD_BYTES", 32)
    await _create_task("task-payload")

    with pytest.raises(db.EventPayloadTooLarge):
        await db.append_delivery_event(
            task_stream("task-payload"),
            "log",
            {"message": "x" * 100},
            task_id="task-payload",
        )
