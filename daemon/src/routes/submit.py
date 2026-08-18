# /opt/bmas/daemon/src/routes/submit.py
"""Task submission endpoint."""

import asyncio
import contextlib
import logging
import random
import uuid
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import database as db
from auth import require_api_key
from config import (
    BMAS_API_KEY,
    COORDINATION_VARIANT,
    MAX_ACTIVE_TASKS,
    MAX_QUEUED_TASKS,
    SHUTDOWN_GRACE_S,
)
from core.gateway import LeaseLostError
from core.orchestrator import LeaseBusyError, Orchestrator

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


@dataclass(frozen=True)
class TaskWorkItem:
    task_id: str
    user_task: str
    overrides: dict | None = None
    resume: bool = False


_task_queue: asyncio.Queue[TaskWorkItem] | None = None
_workers: list[asyncio.Task] = []
_recovery_task: asyncio.Task | None = None
_scheduled_ids: set[str] = set()
_active_jobs: dict[str, asyncio.Task] = {}
_cancel_reasons: dict[str, str] = {}
_orchestrator: Orchestrator | None = None


async def _run_task_safe(
    orch: Orchestrator,
    task_id: str,
    user_task: str,
    overrides: dict | None = None,
    *,
    resume: bool = False,
):
    """Wrapper that guarantees a terminal state in SQLite + SSE.

    Runs as a background asyncio task that outlives the HTTP request.
    Uses database.py module functions (ephemeral connections).

    ``overrides`` contains optional per-task routing/role_registry dicts
    that are threaded through the orchestrator for this task only.
    """
    try:
        await orch.process_task(
            user_task,
            task_id,
            overrides=overrides,
            resume=resume,
        )
    except asyncio.CancelledError:
        _cancel_reasons.pop(task_id, None)
        raise
    except LeaseBusyError:
        logger.info("Task lease is busy. Recovery will retry %s", task_id)
    except LeaseLostError:
        logger.warning("Task lease was lost. Recovery will retry %s", task_id)
    except Exception as e:
        logger.exception(f"Unhandled crash in background task {task_id}")
        with contextlib.suppress(Exception):  # Redis may be down — zombie recovery handles this on restart
            await orch.bb.publish_event(task_id, "error", {
                "error_message": str(e)
            })


async def _task_worker(worker_no: int) -> None:
    """Execute admitted tasks with a fixed global concurrency limit."""
    assert _task_queue is not None
    assert _orchestrator is not None
    while True:
        item = await _task_queue.get()
        try:
            queued_cancel = _cancel_reasons.pop(item.task_id, None)
            if queued_cancel:
                await db.fail_task(item.task_id, f"Task aborted: {queued_cancel}")
                continue
            job = asyncio.create_task(
                _run_task_safe(
                    _orchestrator,
                    item.task_id,
                    item.user_task,
                    overrides=item.overrides,
                    resume=item.resume,
                ),
                name=f"bmas-task-{item.task_id}",
            )
            _active_jobs[item.task_id] = job
            await job
        except asyncio.CancelledError:
            worker = asyncio.current_task()
            if worker is not None and worker.cancelling():
                raise
            logger.info(
                "Task worker %d survived cancellation of %s",
                worker_no,
                item.task_id,
            )
        finally:
            _active_jobs.pop(item.task_id, None)
            _scheduled_ids.discard(item.task_id)
            _task_queue.task_done()
            logger.debug("Task worker %d released %s", worker_no, item.task_id)


async def _recover_unfinished_tasks() -> None:
    """Continuously admit unfinished tasks that are not already scheduled."""
    assert _task_queue is not None
    while True:
        try:
            for task in await db.get_resumable_tasks():
                task_id = str(task["id"])
                if task_id in _scheduled_ids or _task_queue.full():
                    continue
                board_meta = await db.get_board_meta(task_id)
                persisted_overrides = board_meta.get("submission_overrides")
                _task_queue.put_nowait(TaskWorkItem(
                    task_id=task_id,
                    user_task=str(task.get("full_input") or task.get("label") or ""),
                    overrides=(
                        persisted_overrides
                        if isinstance(persisted_overrides, dict)
                        else None
                    ),
                    resume=task.get("status") == "running",
                ))
                _scheduled_ids.add(task_id)
            await asyncio.sleep(4.0 + random.uniform(0.0, 3.0))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Task recovery scan failed", exc_info=True)
            await asyncio.sleep(4.0 + random.uniform(0.0, 3.0))


async def start_task_workers(orch: Orchestrator) -> None:
    """Start the bounded task queue and restart-recovery scanner."""
    global _task_queue, _recovery_task, _orchestrator
    if _workers:
        return
    _orchestrator = orch
    _task_queue = asyncio.Queue(maxsize=MAX_QUEUED_TASKS)
    for worker_no in range(MAX_ACTIVE_TASKS):
        _workers.append(asyncio.create_task(
            _task_worker(worker_no),
            name=f"bmas-task-worker-{worker_no}",
        ))
    _recovery_task = asyncio.create_task(
        _recover_unfinished_tasks(),
        name="bmas-task-recovery",
    )


async def stop_task_workers() -> None:
    """Drain admitted work, then cancel remaining work after the grace limit."""
    global _recovery_task, _task_queue, _orchestrator
    if _recovery_task:
        _recovery_task.cancel()
        await asyncio.gather(_recovery_task, return_exceptions=True)
        _recovery_task = None
    if _task_queue is not None:
        try:
            await asyncio.wait_for(_task_queue.join(), timeout=SHUTDOWN_GRACE_S)
        except TimeoutError:
            logger.warning("Task shutdown grace period expired")
    for task_id in _active_jobs:
        _cancel_reasons.setdefault(task_id, "daemon shutdown")
    for worker in _workers:
        worker.cancel()
    if _workers:
        await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
    _active_jobs.clear()
    _scheduled_ids.clear()
    _task_queue = None
    _orchestrator = None


async def abort_scheduled_task(task_id: str, reason: str) -> bool:
    """Cancel an active task and mark a queued task for early abort."""
    job = _active_jobs.get(task_id)
    if job is not None:
        _cancel_reasons[task_id] = reason
        await db.fail_task(task_id, f"Task aborted: {reason}")
        job.cancel()
        return True
    if task_id in _scheduled_ids:
        _cancel_reasons[task_id] = reason
        removed = False
        if _task_queue is not None:
            queued_items = cast("Any", getattr(_task_queue, "_queue", ()))
            for item in tuple(queued_items):
                if item.task_id != task_id:
                    continue
                queued_items.remove(item)
                _task_queue.task_done()
                removed = True
                break
        await db.fail_task(task_id, f"Task aborted: {reason}")
        if removed:
            _cancel_reasons.pop(task_id, None)
            _scheduled_ids.discard(task_id)
        return True
    return False


@router.post("/submit", status_code=202)
async def submit_task(req: TaskSubmission, request: Request):
    """Submit a task. Returns immediately with task_id (HTTP 202).

    Optional ``overrides`` apply only to this task and do not persist to the
    session settings store. Useful for one-off routing/registry adjustments.
    """
    require_api_key(request, BMAS_API_KEY)
    task_id = f"task-{str(uuid.uuid4())[:8]}"

    if _task_queue is None:
        raise HTTPException(status_code=503, detail="Task queue is not ready")
    if _task_queue.full():
        raise HTTPException(status_code=429, detail="Task queue is full")

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

    if task_overrides:
        await db.upsert_board_meta(
            task_id, {"submission_overrides": task_overrides},
        )

    try:
        _task_queue.put_nowait(TaskWorkItem(
            task_id=task_id,
            user_task=req.task,
            overrides=task_overrides,
        ))
        _scheduled_ids.add(task_id)
        await db.update_run_state(task_id, "queued")
    except asyncio.QueueFull as exc:
        await db.fail_task(task_id, "Task queue became full during submission")
        raise HTTPException(status_code=429, detail="Task queue is full") from exc

    return {"task_id": task_id}
