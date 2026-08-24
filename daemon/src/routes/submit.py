# /opt/bmas/daemon/src/routes/submit.py
"""Task submission endpoint."""

import asyncio
import contextlib
import json
import logging
import os
import random
import uuid
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import Headers

import database as db
from auth import require_api_key
from benchmarks.provenance import build_execution_snapshot
from config import (
    BMAS_API_KEY,
    COORDINATION_VARIANT,
    MAX_ACTIVE_TASKS,
    MAX_QUEUED_TASKS,
    MAX_TASK_CHARS,
    SHUTDOWN_GRACE_S,
)
from core.gateway import LeaseLostError
from core.orchestrator import LeaseBusyError, Orchestrator
from core.variants import (
    UnknownVariantError,
    VariantConfigurationError,
    canonical_variant_id,
    require_variant_class,
)
from routes.files import FileUploadError, store_task_file

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

    model_config = ConfigDict(extra="forbid")

    simple: str | None = None
    light: str | None = None
    medium: str | None = None
    complex: str | None = None

    def to_dict(self) -> dict[str, str]:
        return self.model_dump(exclude_unset=True)


class TaskRoleRegistryOverride(BaseModel):
    """Optional per-task role registry overrides (partial entries per role)."""

    model_config = ConfigDict(extra="forbid")

    preferred_host: str | None = None
    profile: str | None = None
    dispatch_port: int | None = None

    def to_dict(self) -> dict:
        return self.model_dump(exclude_unset=True)


class TaskOverrides(BaseModel):
    """Task-level settings overrides — apply only to this single task execution."""

    model_config = ConfigDict(extra="forbid")

    routing: TaskRoutingOverride | None = None
    role_registry: dict[str, TaskRoleRegistryOverride] | None = None
    # Runtime limit overrides for this one task. The runtime validates the
    # merged result at capture time (classic: validate_classic_settings).
    classic: dict[str, Any] | None = None

    def routing_dict(self) -> dict[str, str] | None:
        if self.routing is None:
            return None
        d = self.routing.to_dict()
        return d if d else None

    def role_registry_dict(self) -> dict[str, dict] | None:
        if self.role_registry is None:
            return None
        return {
            role: entry.to_dict() for role, entry in self.role_registry.items() if entry.to_dict()
        } or None


class BenchmarkContext(BaseModel):
    """Optional durable benchmark identity for a scheduler-created task."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    trial_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    variant: str | None = None
    effort: str | None = Field(
        default=None, pattern=r"^[a-z]{1,32}$",
        description="Named effort level from the runtime's effort profiles",
    )
    overrides: TaskOverrides | None = None
    benchmark: BenchmarkContext | None = None


class TaskSubmissionResponse(BaseModel):
    """Confirm durable queue admission for one task."""

    task_id: str
    variant: str
    status: str


@dataclass(frozen=True)
class TaskWorkItem:
    task_id: str
    user_task: str
    overrides: dict | None = None
    variant_id: str = "classic"
    effective_configuration: dict[str, Any] | None = None
    resume: bool = False


_task_queue: asyncio.Queue[TaskWorkItem] | None = None
_workers: list[asyncio.Task] = []
_recovery_task: asyncio.Task | None = None
_scheduled_ids: set[str] = set()
_active_jobs: dict[str, asyncio.Task] = {}
_cancel_reasons: dict[str, str] = {}
_recovery_blocked: dict[str, str] = {}
_blocked_recovery_cursor: tuple[str, str] | None = None
_orchestrator: Orchestrator | None = None


def _remember_recovery_block(task_id: str, variant_id: str) -> None:
    """Keep one bounded recent record for recovery health."""
    _recovery_blocked.pop(task_id, None)
    _recovery_blocked[task_id] = variant_id
    while len(_recovery_blocked) > 1000:
        _recovery_blocked.pop(next(iter(_recovery_blocked)))


async def _retry_compatible_blocked_tasks() -> None:
    """Check one fair page of blocked tasks for runtime compatibility."""
    global _blocked_recovery_cursor
    blocked_loader = getattr(db, "get_blocked_tasks", None)
    if blocked_loader is None:
        return
    tasks = await blocked_loader(
        limit=100,
        after=_blocked_recovery_cursor,
    )
    if not tasks:
        _blocked_recovery_cursor = None
        return
    last_task = tasks[-1]
    _blocked_recovery_cursor = (
        str(last_task.get("created_at") or ""),
        str(last_task["id"]),
    )
    if len(tasks) < 100:
        _blocked_recovery_cursor = None

    for task in tasks:
        task_id = str(task["id"])
        try:
            variant_class = require_variant_class(str(task.get("variant") or COORDINATION_VARIANT))
            board_meta = await db.get_board_meta(task_id)
            configuration = variant_class.configuration_from_metadata(board_meta)
            if configuration is None:
                raise VariantConfigurationError("The blocked task has no saved configuration")
        except (UnknownVariantError, VariantConfigurationError):
            _remember_recovery_block(
                task_id,
                str(task.get("variant") or "unknown"),
            )
            continue
        if await db.retry_blocked_task(task_id):
            _recovery_blocked.pop(task_id, None)


async def resume_blocked_task(task_id: str) -> bool:
    """Validate and enqueue one blocked task immediately."""
    if _task_queue is None:
        raise RuntimeError("The task queue is not running")
    task = await db.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    if task_id in _scheduled_ids:
        return True
    if _task_queue.full():
        raise asyncio.QueueFull

    variant_class = require_variant_class(str(task.get("variant") or COORDINATION_VARIANT))
    board_meta = await db.get_board_meta(task_id)
    configuration = variant_class.configuration_from_metadata(board_meta)
    if configuration is None:
        raise VariantConfigurationError("The blocked task has no saved configuration")
    persisted_overrides = board_meta.get("submission_overrides")

    _scheduled_ids.add(task_id)
    if not await db.retry_blocked_task(task_id):
        _scheduled_ids.discard(task_id)
        return False
    try:
        _task_queue.put_nowait(
            TaskWorkItem(
                task_id=task_id,
                user_task=str(task.get("full_input") or task.get("label") or ""),
                overrides=(persisted_overrides if isinstance(persisted_overrides, dict) else None),
                variant_id=canonical_variant_id(str(task.get("variant") or COORDINATION_VARIANT)),
                effective_configuration=configuration,
                resume=task.get("status") == "running",
            )
        )
    except Exception:
        _scheduled_ids.discard(task_id)
        await db.block_task_recovery(task_id)
        raise
    _recovery_blocked.pop(task_id, None)
    return True


async def _run_task_safe(
    orch: Orchestrator,
    task_id: str,
    user_task: str,
    overrides: dict | None = None,
    variant_id: str | None = None,
    effective_configuration: dict[str, Any] | None = None,
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
            variant_id=variant_id,
            effective_configuration=effective_configuration,
            resume=resume,
        )
    except asyncio.CancelledError:
        _cancel_reasons.pop(task_id, None)
        raise
    except LeaseBusyError:
        logger.info("Task lease is busy. Recovery will retry %s", task_id)
    except LeaseLostError:
        logger.warning("Task lease was lost. Recovery will retry %s", task_id)
    except VariantConfigurationError as exc:
        _remember_recovery_block(task_id, variant_id or "unknown")
        logger.error(
            "Task recovery is blocked by its saved configuration: %s: %s",
            task_id,
            exc,
        )
    except Exception as e:
        logger.exception(f"Unhandled crash in background task {task_id}")
        with contextlib.suppress(
            Exception
        ):  # Redis may be down — zombie recovery handles this on restart
            await orch.bb.publish_event(task_id, "error", {"error_message": str(e)})


async def _task_worker(worker_no: int) -> None:
    """Execute admitted tasks with a fixed global concurrency limit."""
    assert _task_queue is not None
    assert _orchestrator is not None
    while True:
        item = await _task_queue.get()
        try:
            queued_cancel = _cancel_reasons.pop(item.task_id, None)
            if queued_cancel:
                await db.cancel_task(item.task_id, queued_cancel)
                continue
            job = asyncio.create_task(
                _run_task_safe(
                    _orchestrator,
                    item.task_id,
                    item.user_task,
                    overrides=item.overrides,
                    variant_id=item.variant_id,
                    effective_configuration=item.effective_configuration,
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
            await _retry_compatible_blocked_tasks()
            for task in await db.get_resumable_tasks():
                task_id = str(task["id"])
                if task_id in _scheduled_ids or _task_queue.full():
                    continue
                board_meta = await db.get_board_meta(task_id)
                persisted_overrides = board_meta.get("submission_overrides")
                persisted_configuration = board_meta.get("effective_configuration")
                try:
                    variant_id = canonical_variant_id(
                        str(task.get("variant") or COORDINATION_VARIANT)
                    )
                except UnknownVariantError:
                    unavailable = str(task.get("variant") or "")
                    if _recovery_blocked.get(task_id) != unavailable:
                        logger.error(
                            "Recovery rejected unavailable variant for task %s: %s",
                            task_id,
                            unavailable,
                        )
                    _remember_recovery_block(task_id, unavailable)
                    if not await db.block_task_recovery(task_id):
                        logger.warning(
                            "Recovery could not block unavailable runtime task %s",
                            task_id,
                        )
                    continue
                _recovery_blocked.pop(task_id, None)
                _task_queue.put_nowait(
                    TaskWorkItem(
                        task_id=task_id,
                        user_task=str(task.get("full_input") or task.get("label") or ""),
                        overrides=(
                            persisted_overrides if isinstance(persisted_overrides, dict) else None
                        ),
                        variant_id=variant_id,
                        effective_configuration=(
                            persisted_configuration
                            if isinstance(persisted_configuration, dict)
                            else None
                        ),
                        resume=task.get("status") == "running",
                    )
                )
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
        _workers.append(
            asyncio.create_task(
                _task_worker(worker_no),
                name=f"bmas-task-worker-{worker_no}",
            )
        )
    _recovery_task = asyncio.create_task(
        _recover_unfinished_tasks(),
        name="bmas-task-recovery",
    )


async def stop_task_workers() -> None:
    """Drain admitted work, then cancel remaining work after the grace limit."""
    global _blocked_recovery_cursor, _recovery_task, _task_queue, _orchestrator
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
    _recovery_blocked.clear()
    _blocked_recovery_cursor = None
    _task_queue = None
    _orchestrator = None


async def abort_scheduled_task(task_id: str, reason: str) -> bool:
    """Cancel an active task and mark a queued task for early abort."""
    job = _active_jobs.get(task_id)
    if job is not None:
        _cancel_reasons[task_id] = reason
        lease_token = None
        if _orchestrator is not None:
            token_reader = getattr(_orchestrator, "task_lease_token", None)
            if callable(token_reader):
                lease_token = token_reader(task_id)
            rollup = getattr(_orchestrator, "rollup_task_cost", None)
            try:
                if callable(rollup):
                    await rollup(task_id, lease_token)
                else:
                    await db.update_task_cost_totals(
                        task_id,
                        lease_token=lease_token,
                    )
            except Exception:
                logger.warning(
                    "Final cost rollup failed during abort for task %s",
                    task_id,
                    exc_info=True,
                )
        await db.cancel_task(task_id, reason, lease_token=lease_token)
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
        await db.cancel_task(task_id, reason)
        if removed:
            _cancel_reasons.pop(task_id, None)
            _scheduled_ids.discard(task_id)
        return True
    return False


def task_queue_snapshot() -> dict[str, int | bool]:
    """Return current task admission counts and limits."""
    queued = _task_queue.qsize() if _task_queue is not None else 0
    return {
        "ready": _task_queue is not None,
        "queued_tasks": queued,
        "queue_capacity": MAX_QUEUED_TASKS,
        "active_tasks": len(_active_jobs),
        "active_capacity": MAX_ACTIVE_TASKS,
        "recovery_blocked_tasks": len(_recovery_blocked),
    }


async def _admit_task(
    req: TaskSubmission,
    uploads: list[UploadFile] | None = None,
    *,
    captured_configuration: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> dict[str, str]:
    """Save one task, store initial uploads, and admit the task to the queue."""
    requested_task_id = task_id
    task_id = task_id or f"task-{str(uuid.uuid4())[:8]}"
    initial_uploads = uploads or []

    if not req.task.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "objective_empty",
                "message": "The task objective cannot be empty",
            },
        )
    if len(req.task) > MAX_TASK_CHARS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "objective_too_large",
                "message": "The task objective exceeds the character limit",
                "limit_chars": MAX_TASK_CHARS,
                "actual_chars": len(req.task),
            },
        )

    try:
        variant_id = canonical_variant_id(req.variant or COORDINATION_VARIANT)
        variant_class = require_variant_class(variant_id)
    except UnknownVariantError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "variant_unavailable",
                "message": str(exc),
            },
        ) from exc

    existing = await db.get_task(task_id) if requested_task_id else None
    if existing is not None:
        metadata = await db.get_board_meta(task_id)
        existing_benchmark = metadata.get("benchmark")
        requested_benchmark = req.benchmark.model_dump() if req.benchmark else None
        if existing_benchmark != requested_benchmark:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "task_identity_conflict",
                    "message": "The task identifier belongs to another submission",
                },
            )
        return {
            "task_id": task_id,
            "variant": canonical_variant_id(str(existing.get("variant") or variant_id)),
            "status": str(existing.get("status") or "queued"),
        }

    if _task_queue is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "queue_not_ready",
                "message": "The task queue is not ready",
                "capacity": task_queue_snapshot(),
            },
            headers={"Retry-After": "5"},
        )
    if _task_queue.full():
        raise HTTPException(
            status_code=429,
            detail={
                "code": "queue_full",
                "message": "The task queue is full",
                "capacity": task_queue_snapshot(),
            },
            headers={"Retry-After": "5"},
        )

    # Create the SQLite row BEFORE spawning background task
    # Always stamp the active variant — never rely on schema default.
    # Build per-task overrides dict (None if no overrides provided)
    task_overrides: dict | None = None
    if req.overrides is not None or req.effort is not None:
        task_overrides = {}
        if req.overrides is not None:
            routing_dict = req.overrides.routing_dict()
            if routing_dict:
                task_overrides["routing"] = routing_dict
            rr_dict = req.overrides.role_registry_dict()
            if rr_dict:
                task_overrides["role_registry"] = rr_dict
            if req.overrides.classic:
                task_overrides["classic"] = dict(req.overrides.classic)
        if req.effort is not None:
            task_overrides["effort"] = req.effort
        if not task_overrides:
            task_overrides = None

    effective_configuration = (
        captured_configuration
        if captured_configuration is not None
        else await variant_class.capture_configuration(task_overrides)
    )

    benchmark_context = req.benchmark.model_dump() if req.benchmark else None
    execution_snapshot, snapshot_checksum = build_execution_snapshot(
        runtime_id=variant_id,
        effective_configuration=effective_configuration,
        submission_overrides=task_overrides,
        benchmark_context=benchmark_context,
    )

    submission_meta: dict[str, Any] = {
        "effective_configuration": effective_configuration,
        "execution_snapshot": execution_snapshot,
        "execution_snapshot_checksum": snapshot_checksum,
    }
    if benchmark_context:
        submission_meta["benchmark"] = benchmark_context
    if task_overrides:
        submission_meta["submission_overrides"] = task_overrides
    await db.create_task_with_meta(
        task_id,
        req.task[:80],
        req.task,
        variant_id,
        submission_meta,
        **({"run_state": "staging"} if initial_uploads else {}),
    )

    stored_uploads: list[dict] = []
    try:
        for upload in initial_uploads:
            stored_uploads.append(await store_task_file(task_id, upload, announce=False))
    except FileUploadError as exc:
        await _discard_staged_task(task_id, stored_uploads)
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": "attachment_invalid",
                "message": exc.message,
            },
        ) from exc
    except Exception as exc:
        await _discard_staged_task(task_id, stored_uploads)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "attachment_storage_failed",
                "message": "The daemon could not store the task attachments",
            },
        ) from exc

    _scheduled_ids.add(task_id)
    try:
        if initial_uploads and not await db.update_run_state(task_id, "queued"):
            raise RuntimeError("The staged task could not enter the queue")
        _task_queue.put_nowait(
            TaskWorkItem(
                task_id=task_id,
                user_task=req.task,
                overrides=task_overrides,
                variant_id=variant_id,
                effective_configuration=effective_configuration,
            )
        )
    except asyncio.QueueFull as exc:
        _scheduled_ids.discard(task_id)
        await db.fail_task(task_id, "Task queue became full during submission")
        raise HTTPException(
            status_code=429,
            detail={
                "code": "queue_full",
                "message": "The task queue is full",
                "capacity": task_queue_snapshot(),
            },
            headers={"Retry-After": "5"},
        ) from exc
    except Exception as exc:
        _scheduled_ids.discard(task_id)
        await db.fail_task(task_id, "Task queue admission failed")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "queue_admission_failed",
                "message": "The task could not enter the execution queue",
            },
        ) from exc

    return {"task_id": task_id, "variant": variant_id, "status": "queued"}


async def _discard_staged_task(task_id: str, stored_uploads: list[dict]) -> None:
    """Remove files and database data for one rejected staged task."""
    parent_dirs: set[str] = set()
    for upload in stored_uploads:
        stored_path = str(upload.get("stored_path") or "")
        text_path = str(upload.get("text_path") or "")
        if stored_path:
            parent_dirs.add(os.path.dirname(stored_path))
        for path in (text_path, stored_path):
            if not path:
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    await db.delete_task(task_id)
    for directory in parent_dirs:
        with contextlib.suppress(OSError):
            os.rmdir(directory)


@router.post(
    "/submit",
    status_code=202,
    response_model=TaskSubmissionResponse,
)
async def submit_task(req: TaskSubmission, request: Request):
    """Submit one task without initial file attachments."""
    require_api_key(request, BMAS_API_KEY)
    return await _admit_task(req)


@router.post(
    "/submit-with-files",
    status_code=202,
    response_model=TaskSubmissionResponse,
)
async def submit_task_with_files(
    request: Request,
    task: str = Form(...),
    variant: str | None = Form(None),
    effort: str | None = Form(None),
    overrides: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
):
    """Store initial files before one task can enter the execution queue."""
    require_api_key(request, BMAS_API_KEY)
    parsed_overrides: TaskOverrides | None = None
    if overrides:
        try:
            decoded = json.loads(overrides)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "overrides must contain valid JSON"},
            ) from exc
        try:
            parsed_overrides = TaskOverrides.model_validate(decoded)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": f"overrides are invalid: {exc.errors()[0].get('msg', 'validation failed')}"},
            ) from exc
    return await _admit_task(
        TaskSubmission(
            task=task,
            variant=variant or None,
            effort=effort or None,
            overrides=parsed_overrides,
        ),
        list(files or []),
    )


@router.post(
    "/tasks/{task_id}/duplicate",
    status_code=202,
    response_model=TaskSubmissionResponse,
)
async def duplicate_task(task_id: str, request: Request):
    """Create a queued task from durable server-side task inputs."""
    require_api_key(request, BMAS_API_KEY)
    source = await db.get_task(task_id)
    if source is None:
        raise HTTPException(status_code=404, detail={"message": "Task not found"})

    metadata = await db.get_board_meta(task_id)
    raw_overrides = metadata.get("submission_overrides")
    overrides = (
        TaskOverrides.model_validate(raw_overrides) if isinstance(raw_overrides, dict) else None
    )
    uploads: list[UploadFile] = []
    try:
        for stored in await db.get_task_files(task_id):
            path = str(stored.get("stored_path") or "")
            if not path or not os.path.isfile(path):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"Stored input is unavailable: {stored.get('name', 'file')}"
                    },
                )
            uploads.append(
                UploadFile(
                    file=open(path, "rb"),  # noqa: SIM115 -- UploadFile closes this handle below.
                    filename=str(stored.get("name") or "input"),
                    size=int(stored.get("bytes") or 0),
                    headers=Headers(
                        {"content-type": str(stored.get("mime") or "application/octet-stream")}
                    ),
                )
            )
        return await _admit_task(
            TaskSubmission(
                task=str(source.get("full_input") or source.get("label") or ""),
                variant=str(source.get("variant") or COORDINATION_VARIANT),
                overrides=overrides,
            ),
            uploads,
        )
    finally:
        for upload in uploads:
            await upload.close()
