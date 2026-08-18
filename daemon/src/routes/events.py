"""SSE endpoints backed by the authoritative SQLite event journal."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import database as db
from core.event_delivery import ensure_terminal_event, task_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

router = APIRouter()

_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _bounded_replay_page_size() -> int:
    """Read the bounded durable replay page size."""
    try:
        value = int(os.getenv("BMAS_EVENT_REPLAY_PAGE", "250"))
    except ValueError:
        return 250
    return min(max(value, 1), 500)


REPLAY_PAGE_SIZE = _bounded_replay_page_size()


def _validate_task_id(task_id: str) -> bool:
    """Return true if the task identifier uses the safe format."""
    return bool(_ID_PATTERN.match(task_id))


def _last_event_id(request: Request) -> int:
    """Parse the standard SSE reconnect cursor."""
    raw_cursor = request.headers.get("last-event-id")
    if raw_cursor is None or raw_cursor == "":
        return 0
    if not raw_cursor.isascii() or not raw_cursor.isdigit():
        raise ValueError("Last-Event-ID must be a non-negative integer")
    return int(raw_cursor)


def _format_sse(event: dict) -> str:
    """Format one durable event with its reconnect cursor."""
    return (
        f"id: {event['cursor']}\n"
        f"event: {event['event_type']}\n"
        f"data: {json.dumps(event['data'], separators=(',', ':'))}\n\n"
    )


def _format_snapshot(event_type: str, data: dict, cursor: int) -> str:
    """Format one materialized state snapshot at a journal cursor."""
    cursor_line = f"id: {cursor}\n" if cursor else ""
    return (
        f"{cursor_line}event: {event_type}\n"
        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
    )


async def _cursor_gap_response(stream: str, cursor: int) -> JSONResponse | None:
    """Return a recovery contract when a cursor falls outside journal bounds."""
    if cursor == 0:
        return None
    bounds = await db.get_delivery_cursor_bounds(stream)
    earliest = bounds["earliest"]
    latest = bounds["latest"]
    if cursor > latest:
        return JSONResponse(
            {
                "error": "event_cursor_gap",
                "requested_event_id": cursor,
                "earliest_event_id": earliest,
                "latest_event_id": latest,
                "recovery": "Reconnect without Last-Event-ID and hydrate current state",
            },
            status_code=409,
        )
    return None


async def _open_pubsub(redis_client: Any, channel: str) -> Any | None:
    """Subscribe before replay so a concurrent notification cannot disappear."""
    pubsub = None
    try:
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        return pubsub
    except Exception:
        if pubsub is not None:
            with suppress(Exception):
                await pubsub.aclose()
        return None


async def _wait_for_notification(pubsub: Any | None) -> dict | None:
    """Wait for Redis or poll SQLite when Redis is unavailable."""
    if pubsub is None:
        await asyncio.sleep(1.0)
        return None
    try:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=1.0,
        )
    except Exception:
        await asyncio.sleep(1.0)
        return None
    if not message or message.get("type") != "message":
        return None
    raw_payload = message.get("data")
    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8", errors="replace")
    if not isinstance(raw_payload, str):
        return None
    with suppress(json.JSONDecodeError, TypeError):
        payload = json.loads(raw_payload)
        return payload if isinstance(payload, dict) else None
    return None


async def _close_pubsub(pubsub: Any | None, channel: str) -> None:
    """Close one optional Redis subscription."""
    if pubsub is None:
        return
    with suppress(Exception):
        await pubsub.unsubscribe(channel)
    with suppress(Exception):
        await pubsub.aclose()


async def _journal_stream(
    request: Request,
    redis_client: Any,
    *,
    stream: str,
    channel: str,
    after_cursor: int,
    snapshot_loader: Callable[[], Awaitable[list[tuple[str, dict]]]] | None = None,
    close_on_terminal: bool = False,
    forward_transient: bool = False,
) -> AsyncIterator[str]:
    """Replay durable pages, then use Redis only as a live wake signal."""
    snapshot_cursor = None
    if after_cursor == 0 and snapshot_loader:
        # Use a low watermark from before subscription. The later snapshot can
        # include newer state, but the journal still replays every newer event.
        bounds = await db.get_delivery_cursor_bounds(stream)
        snapshot_cursor = bounds["latest"]
    pubsub = await _open_pubsub(redis_client, channel)
    cursor = after_cursor
    try:
        if cursor == 0 and snapshot_loader:
            cursor = int(snapshot_cursor or 0)
            snapshots = await snapshot_loader()
            for event_type, data in snapshots:
                yield _format_snapshot(event_type, data, cursor)

        while True:
            if await request.is_disconnected():
                break

            events = await db.get_delivery_events_after(
                stream,
                cursor,
                limit=REPLAY_PAGE_SIZE,
            )
            if events:
                for event in events:
                    event_cursor = int(event["cursor"])
                    if event_cursor <= cursor:
                        continue
                    cursor = event_cursor
                    yield _format_sse(event)
                    if close_on_terminal and event["event_type"] in {"complete", "error"}:
                        return
                continue

            notification = await _wait_for_notification(pubsub)
            if (
                forward_transient
                and notification
                and "cursor" not in notification
                and isinstance(notification.get("event"), str)
                and isinstance(notification.get("data"), dict)
            ):
                yield _format_snapshot(
                    notification["event"],
                    notification["data"],
                    0,
                )
                continue
            yield ":keepalive\n\n"
    finally:
        await _close_pubsub(pubsub, channel)


def _stream_headers() -> dict[str, str]:
    """Return proxy-safe SSE and replay contract headers."""
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "X-BMAS-Event-Journal": "sqlite",
        "X-BMAS-Replay-Page": str(REPLAY_PAGE_SIZE),
    }


@router.get("/events/system")
async def system_events(request: Request):
    """Stream durable lifecycle events and current transient health state."""
    from app import app

    try:
        cursor = _last_event_id(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    gap = await _cursor_gap_response("system", cursor)
    if gap:
        return gap

    async def load_snapshots() -> list[tuple[str, dict]]:
        snapshots: list[tuple[str, dict]] = []
        if hasattr(app.state, "last_daemon_status"):
            snapshots.append(("daemon-status", app.state.last_daemon_status))
        if hasattr(app.state, "last_agent_health"):
            snapshots.append(("agent-health", app.state.last_agent_health))
        return snapshots

    return StreamingResponse(
        _journal_stream(
            request,
            app.state.orchestrator.bb.redis,
            stream="system",
            channel="bmas:events:system",
            after_cursor=cursor,
            snapshot_loader=load_snapshots,
            forward_transient=True,
        ),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@router.get("/events/{task_id}")
async def task_events(task_id: str, request: Request):
    """Stream task events with durable replay and terminal reconciliation."""
    if not _validate_task_id(task_id):
        return JSONResponse(
            {"error": "Invalid task_id: must be 1-64 alphanumeric/hyphen/underscore chars"},
            status_code=400,
        )

    from app import app

    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    try:
        cursor = _last_event_id(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    redis_client = app.state.orchestrator.bb.redis
    if task["status"] in {"completed", "failed"}:
        await ensure_terminal_event(redis_client, task)

    stream = task_stream(task_id)
    gap = await _cursor_gap_response(stream, cursor)
    if gap:
        return gap

    snapshot_loader = None
    if cursor == 0 and task["status"] not in {"completed", "failed"}:
        async def load_snapshot() -> list[tuple[str, dict]]:
            current = await db.get_task(task_id)
            sub_tasks = await db.get_sub_tasks(task_id)
            if current and current["status"] in {"completed", "failed"}:
                await ensure_terminal_event(redis_client, current)
            return [("initial_state", {"task": current, "sub_tasks": sub_tasks})]

        snapshot_loader = load_snapshot

    return StreamingResponse(
        _journal_stream(
            request,
            redis_client,
            stream=stream,
            channel=f"bmas:events:{task_id}",
            after_cursor=cursor,
            snapshot_loader=snapshot_loader,
            close_on_terminal=True,
        ),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )
