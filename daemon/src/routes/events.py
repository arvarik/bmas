# /opt/bmas/daemon/src/routes/events.py
"""SSE streaming endpoints — task-scoped and system-wide."""

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import database as db

router = APIRouter()

# Allow only safe task ID formats: alphanumeric, hyphens, underscores (same as hitl.py)
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_task_id(task_id: str) -> bool:
    """Return True if the task_id is a safe, well-formed identifier."""
    return bool(_ID_PATTERN.match(task_id))


@router.get("/events/system")
async def system_events(request: Request):
    """Global system health and task lifecycle SSE stream."""
    from app import app

    async def generate():
        orch = app.state.orchestrator
        pubsub = orch.bb.redis.pubsub()
        await pubsub.subscribe("bmas:events:system")
        try:
            if hasattr(app.state, "last_daemon_status"):
                yield f"event: daemon-status\ndata: {json.dumps(app.state.last_daemon_status)}\n\n"
            if hasattr(app.state, "last_agent_health"):
                yield f"event: agent-health\ndata: {json.dumps(app.state.last_agent_health)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    payload = json.loads(message["data"])
                    event_name = payload["event"]
                    yield f"event: {event_name}\ndata: {json.dumps(payload['data'])}\n\n"
                else:
                    yield ":keepalive\n\n"
        finally:
            await pubsub.unsubscribe("bmas:events:system")
            await pubsub.aclose()

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/events/{task_id}")
async def task_events(task_id: str, request: Request):
    """Task-scoped SSE stream.

    Returns 400 if the task_id contains unsafe characters, 404 if not found.
    For completed/failed tasks, emits a single terminal event and closes.
    """
    if not _validate_task_id(task_id):
        return JSONResponse(
            {"error": "Invalid task_id: must be 1-64 alphanumeric/hyphen/underscore chars"},
            status_code=400,
        )

    from app import app

    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    if task["status"] in ("completed", "failed"):
        async def emit_final():
            event = "complete" if task["status"] == "completed" else "error"
            yield f"event: {event}\ndata: {json.dumps(task)}\n\n"
        return StreamingResponse(emit_final(), media_type="text/event-stream")

    async def generate():
        orch = app.state.orchestrator
        pubsub = orch.bb.redis.pubsub()
        await pubsub.subscribe(f"bmas:events:{task_id}")
        try:
            current = await db.get_task(task_id)
            sub_tasks = await db.get_sub_tasks(task_id)

            yield f"event: initial_state\ndata: {json.dumps({'task': current, 'sub_tasks': sub_tasks})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    payload = json.loads(message["data"])
                    event_name = payload["event"]
                    yield f"event: {event_name}\ndata: {json.dumps(payload['data'])}\n\n"
                    if event_name in ("complete", "error"):
                        break
                else:
                    yield ":keepalive\n\n"
        finally:
            await pubsub.unsubscribe(f"bmas:events:{task_id}")
            await pubsub.aclose()

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
