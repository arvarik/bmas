# /opt/bmas/daemon/src/core/event_emitter.py
"""Event emitter abstraction for the Board Gateway (doc 04 §9).

The gateway emits SSE events through this interface, decoupled from
the Redis Pub/Sub transport.  Tests use InMemoryEventEmitter;
production uses RedisEventEmitter.

Event names are defined in protocol.py.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventEmitter(Protocol):
    """Protocol for emitting board events via SSE."""

    async def emit(
        self, task_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        """Emit an event for a task.

        Args:
            task_id: The task this event belongs to.
            event_type: One of the EVENT_* constants from protocol.py.
            data: The event payload (will be JSON-serialized for SSE).
        """
        ...


class InMemoryEventEmitter:
    """Captures events in a list for test assertions.

    Usage in tests:
        emitter = InMemoryEventEmitter()
        gateway = BoardGateway(store, emitter)
        await gateway.append(...)
        assert emitter.events[0] == ("task-1", "board_entry", {...})
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(
        self, task_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        self.events.append((task_id, event_type, data))

    def clear(self) -> None:
        self.events.clear()

    def events_of_type(self, event_type: str) -> list[dict[str, Any]]:
        """Return all event payloads matching the given type."""
        return [d for _, t, d in self.events if t == event_type]

    def has_event(self, event_type: str) -> bool:
        """Check if at least one event of the given type was emitted."""
        return any(t == event_type for _, t, _ in self.events)


class RedisEventEmitter:
    """Publishes events to Redis Pub/Sub for SSE delivery.

    Uses the existing bmas:events:{task_id} channel pattern
    from blackboard.py.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def emit(
        self, task_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        await self._redis.publish(
            f"bmas:events:{task_id}",
            json.dumps({"event": event_type, "data": data}),
        )
