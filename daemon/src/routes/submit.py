# /opt/bmas/daemon/src/routes/submit.py
"""Task submission endpoint."""

import asyncio
import contextlib
import logging
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

import database as db
from config import COORDINATION_VARIANT
from core.orchestrator import Orchestrator

logger = logging.getLogger("bmas.daemon")

router = APIRouter()


# ── Per-task override models ──────────────────────────────────────────────

class TaskRoutingOverride(BaseModel):
    """Optional per-task complexity → model routing overrides.

    Only provided tiers are overridden for this task; omitted tiers use
    the current session routing (which itself may be overridden from yaml defaults).
    These overrides do NOT persist to the session — they apply only to the
    single submitted task.
    """
    simple: str | None = None
    light: str | None = None
    medium: str | None = None
    complex: str | None = None

    def to_dict(self) -> dict[str, str]:
        return self.model_dump(exclude_unset=True)


class TaskRoleRegistryOverride(BaseModel):
    """Optional per-task role registry overrides (partial entries per role)."""
    preferred_host: str | None = None
    profile: str | None = None
    dispatch_port: int | None = None

    def to_dict(self) -> dict:
        return self.model_dump(exclude_unset=True)


class TaskOverrides(BaseModel):
    """Task-level settings overrides — apply only to this single task execution."""
    routing: TaskRoutingOverride | None = None
    role_registry: dict[str, TaskRoleRegistryOverride] | None = None

    def routing_dict(self) -> dict[str, str] | None:
        if self.routing is None:
            return None
        d = self.routing.to_dict()
        return d if d else None

    def role_registry_dict(self) -> dict[str, dict] | None:
        if self.role_registry is None:
            return None
        return {
            role: entry.to_dict()
            for role, entry in self.role_registry.items()
            if entry.to_dict()
        } or None


class TaskSubmission(BaseModel):
    task: str
    overrides: TaskOverrides | None = None


# Module-level strong reference set (prevents GC of background tasks)
_background_tasks: set[asyncio.Task] = set()


async def _run_task_safe(
    orch: Orchestrator,
    task_id: str,
    user_task: str,
    overrides: dict | None = None,
):
    """Wrapper that guarantees a terminal state in SQLite + SSE.

    Runs as a background asyncio task that outlives the HTTP request.
    Uses database.py module functions (ephemeral connections).

    ``overrides`` contains optional per-task routing/role_registry dicts
    that are threaded through the orchestrator for this task only.
    """
    try:
        await orch.process_task(user_task, task_id, overrides=overrides)
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
    """Submit a task. Returns immediately with task_id (HTTP 202).

    Optional ``overrides`` apply only to this task and do not persist to the
    session settings store. Useful for one-off routing/registry adjustments.
    """
    from app import app
    task_id = f"task-{str(uuid.uuid4())[:8]}"

    # Create the SQLite row BEFORE spawning background task
    # Always stamp the active variant — never rely on schema default.
    await db.create_task(task_id, req.task[:80], req.task,
                         variant=COORDINATION_VARIANT)

    # Build per-task overrides dict (None if no overrides provided)
    task_overrides: dict | None = None
    if req.overrides is not None:
        task_overrides = {}
        routing_dict = req.overrides.routing_dict()
        if routing_dict:
            task_overrides["routing"] = routing_dict
        rr_dict = req.overrides.role_registry_dict()
        if rr_dict:
            task_overrides["role_registry"] = rr_dict
        if not task_overrides:
            task_overrides = None

    orch = app.state.orchestrator
    task = asyncio.create_task(
        _run_task_safe(orch, task_id, req.task, overrides=task_overrides)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"task_id": task_id}
