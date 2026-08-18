"""SQLite-first event delivery with Redis as a low-latency notification path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from typing import Any

import database as db

logger = logging.getLogger("bmas.event_delivery")

def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read one bounded integer setting."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


OUTBOX_BATCH_SIZE = _bounded_env_int("BMAS_EVENT_OUTBOX_BATCH", 100, 1, 500)
RECONCILIATION_INTERVAL_S = 1.0
_dispatch_lock = asyncio.Lock()


def task_stream(task_id: str) -> str:
    """Return the durable stream name for one task."""
    return f"task:{task_id}"


def redis_channel(stream: str) -> str:
    """Map one durable stream to its Redis notification channel."""
    if stream == "system":
        return "bmas:events:system"
    if stream.startswith("task:"):
        return f"bmas:events:{stream.removeprefix('task:')}"
    raise ValueError(f"Unknown event stream: {stream}")


def terminal_event_payload(task: dict) -> dict:
    """Build the canonical terminal event from one authoritative task row."""
    return {
        "task_id": task["id"],
        "status": task["status"],
        "answer": task.get("result_summary"),
        "error_message": task.get("error_message"),
        "terminated_by": task.get("terminated_by"),
        "answer_source": task.get("answer_source"),
        "rounds_completed": task.get("rounds_used"),
        "budget_spent": task.get("total_cost_usd"),
        "total_tokens": task.get("total_tokens"),
        "duration_ms": task.get("duration_ms"),
        "completed_at": task.get("completed_at"),
    }


def system_terminal_event_payload(task: dict) -> dict:
    """Build one canonical global terminal lifecycle payload."""
    return {
        "task_id": task["id"],
        "status": task["status"],
        "label": task.get("label"),
    }


async def publish_journal_event(redis_client: Any, event: dict) -> None:
    """Publish one journal event and acknowledge its outbox record."""
    payload = {
        "cursor": event["cursor"],
        "event": event["event_type"],
        "data": event["data"],
    }
    await redis_client.publish(
        redis_channel(str(event["stream"])),
        json.dumps(payload, separators=(",", ":")),
    )
    await db.mark_delivery_published(int(event["cursor"]))


async def record_and_publish(
    redis_client: Any,
    stream: str,
    event_type: str,
    data: dict,
    *,
    task_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Save one event before a best-effort Redis notification."""
    if task_id and event_type in {"complete", "error"}:
        task = await db.get_task(task_id)
        if task and task.get("status") in {"completed", "failed"}:
            event_type = "complete" if task["status"] == "completed" else "error"
            data = terminal_event_payload(task)
            idempotency_key = f"terminal:{task_id}:{task['status']}"
            existing = await db.get_delivery_event_by_idempotency(
                stream,
                idempotency_key,
            )
            if existing is not None:
                if not existing.get("published_at"):
                    await dispatch_outbox(redis_client, limit=OUTBOX_BATCH_SIZE)
                return existing
    event = await db.append_delivery_event(
        stream,
        event_type,
        data,
        task_id=task_id,
        idempotency_key=idempotency_key,
    )
    if event.get("published_at"):
        return event
    await dispatch_outbox(redis_client, limit=OUTBOX_BATCH_SIZE)
    return event


async def reconcile_terminal_events(redis_client: Any, limit: int = 100) -> int:
    """Create missing task and system terminal events from task rows."""
    task_rows = await db.get_terminal_tasks_without_events(limit=limit)
    system_rows = await db.get_terminal_tasks_missing_system_events(limit=limit)
    tasks = {
        str(task["id"]): task
        for task in [*task_rows, *system_rows]
    }
    for task in task_rows:
        await ensure_terminal_event(redis_client, task)
    for task in system_rows:
        await ensure_system_terminal_event(redis_client, task)
    return len(tasks)


async def ensure_terminal_event(redis_client: Any, task: dict) -> dict:
    """Create one idempotent terminal event from a terminal task row."""
    if task.get("status") not in {"completed", "failed"}:
        raise ValueError("A terminal event requires a completed or failed task")
    event_type = "complete" if task["status"] == "completed" else "error"
    idempotency_key = f"terminal:{task['id']}:{task['status']}"
    existing = await db.get_delivery_event_by_idempotency(
        task_stream(str(task["id"])),
        idempotency_key,
    )
    if existing is not None:
        if not existing.get("published_at"):
            await dispatch_outbox(redis_client, limit=OUTBOX_BATCH_SIZE)
        return existing
    return await record_and_publish(
        redis_client,
        task_stream(str(task["id"])),
        event_type,
        terminal_event_payload(task),
        task_id=str(task["id"]),
        idempotency_key=idempotency_key,
    )


async def ensure_system_terminal_event(redis_client: Any, task: dict) -> dict:
    """Create one idempotent global event from a terminal task row."""
    if task.get("status") not in {"completed", "failed"}:
        raise ValueError("A system terminal event requires a terminal task")
    idempotency_key = f"system-terminal:{task['id']}:{task['status']}"
    existing = await db.get_delivery_event_by_idempotency(
        "system",
        idempotency_key,
    )
    if existing is not None:
        if not existing.get("published_at"):
            await dispatch_outbox(redis_client, limit=OUTBOX_BATCH_SIZE)
        return existing
    return await record_and_publish(
        redis_client,
        "system",
        "task-completed",
        system_terminal_event_payload(task),
        task_id=str(task["id"]),
        idempotency_key=idempotency_key,
    )


async def dispatch_outbox(redis_client: Any, limit: int = OUTBOX_BATCH_SIZE) -> int:
    """Publish one bounded outbox batch in cursor order."""
    async with _dispatch_lock:
        delivered = 0
        for event in await db.get_pending_delivery_events(limit=limit):
            try:
                await publish_journal_event(redis_client, event)
                delivered += 1
            except Exception as exc:
                await db.mark_delivery_failed(int(event["cursor"]), str(exc))
                logger.warning(
                    "Outbox delivery stopped at cursor %s: %s",
                    event["cursor"],
                    exc,
                )
                break
        return delivered


async def reconcile_once(redis_client: Any) -> dict[str, int]:
    """Reconcile missing terminal events and retry one outbox batch."""
    terminals = await reconcile_terminal_events(redis_client)
    delivered = await dispatch_outbox(redis_client)
    return {"terminal_events": terminals, "delivered_events": delivered}


async def delivery_reconciliation_loop(redis_client: Any) -> None:
    """Retry durable event delivery until the daemon shuts down."""
    while True:
        try:
            await reconcile_once(redis_client)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event delivery reconciliation failed")
        await asyncio.sleep(RECONCILIATION_INTERVAL_S)


async def stop_delivery_task(task: asyncio.Task[None]) -> None:
    """Cancel and await the delivery task without leaking cancellation."""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
