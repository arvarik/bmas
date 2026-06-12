# /opt/bmas/daemon/src/routes/submit.py
"""Task submission endpoint."""

import asyncio
import contextlib
import logging
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

import database as db
from core.orchestrator import Orchestrator

logger = logging.getLogger("bmas.daemon")

router = APIRouter()


class TaskSubmission(BaseModel):
    task: str


# Module-level strong reference set (prevents GC of background tasks)
_background_tasks: set[asyncio.Task] = set()


async def _run_task_safe(orch: Orchestrator, task_id: str, user_task: str):
    """Wrapper that guarantees a terminal state in SQLite + SSE.

    Runs as a background asyncio task that outlives the HTTP request.
    Uses database.py module functions (ephemeral connections).
    """
    try:
        await orch.process_task(user_task, task_id)
    except Exception as e:
        logger.exception(f"Unhandled crash in background task {task_id}")
        try:
            await db.fail_task(task_id, f"Internal error: {e}")
        except Exception:
            logger.exception(f"Failed to mark task {task_id} as failed in DB")
        with contextlib.suppress(Exception):  # Redis may be down — zombie recovery handles this on restart
            await orch.bb.publish_event(task_id, "error", {
                "error_message": str(e)
            })


@router.post("/submit", status_code=202)
async def submit_task(req: TaskSubmission):
    """Submit a task. Returns immediately with task_id (HTTP 202)."""
    from app import app
    task_id = f"task-{str(uuid.uuid4())[:8]}"

    # Create the SQLite row BEFORE spawning background task
    await db.create_task(task_id, req.task[:80], req.task)

    orch = app.state.orchestrator
    task = asyncio.create_task(_run_task_safe(orch, task_id, req.task))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"task_id": task_id}
