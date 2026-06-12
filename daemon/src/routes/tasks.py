# /opt/bmas/daemon/src/routes/tasks.py
"""Task REST endpoints — list, detail, debate, cost, logs, config probe."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import database as db
from config import BLACKBOARD_V2, COORDINATION_VARIANT

router = APIRouter()


@router.get("/config/active")
async def get_active_config():
    """Return the daemon's active coordination variant and build flags.

    Used by the eval A/B harness (Phase E) to verify which arm is running
    before submitting benchmark items. Read-only; exposes only config that
    is already visible in daemon startup logs.
    """
    return {
        "variant": COORDINATION_VARIANT,
        "blackboard_v2": BLACKBOARD_V2,
    }


@router.get("/tasks")
async def list_tasks_endpoint(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
):
    """List task history with pagination and optional status filter."""
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)

    if status and status not in ("pending", "running", "completed", "failed"):
        return JSONResponse(
            {"error": f"Invalid status: {status}. Must be pending/running/completed/failed"},
            status_code=400,
        )

    tasks = await db.list_tasks(limit=limit, offset=offset, status=status)
    total = await db.count_tasks(status=status)

    return {"tasks": tasks, "total": total, "limit": limit, "offset": offset}


@router.get("/tasks/{task_id}")
async def get_task_detail(task_id: str):
    """Full task detail including sub-tasks."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    sub_tasks = await db.get_sub_tasks(task_id)
    return {"task": task, "sub_tasks": sub_tasks}


@router.get("/tasks/{task_id}/debate")
async def get_task_debate(task_id: str):
    """Fetch debate entries for a task."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    entries = await db.get_debate(task_id)
    return {"entries": entries}


@router.get("/tasks/{task_id}/cost")
async def get_task_cost_endpoint(task_id: str):
    """Per-task cost breakdown by model and phase."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    summary = await db.get_task_cost_summary(task_id)
    return summary


@router.get("/tasks/{task_id}/logs")
async def get_task_logs_endpoint(task_id: str, limit: int = 500, offset: int = 0):
    """Archived log entries for a task with pagination."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)

    entries = await db.get_task_logs(task_id, limit=limit, offset=offset)
    total = await db.count_task_logs(task_id)
    return {"entries": entries, "total": total}


# ── Phase 1: Trace & Turn read endpoints ─────────────────────────────

@router.get("/tasks/{task_id}/trace")
async def get_task_traces_endpoint(task_id: str, limit: int = 200, offset: int = 0):
    """Fetch agent trace events for a task (paginated).

    Returns traces ordered by turn_id + seq.
    """
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)

    traces = await db.get_task_traces(task_id, limit=limit, offset=offset)
    return {"traces": traces, "total": len(traces)}


@router.get("/tasks/{task_id}/turns")
async def get_task_turns_endpoint(task_id: str):
    """Fetch all turn records for a task, ordered by round_no."""
    task = await db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    turns = await db.get_turns(task_id)
    return {"turns": turns}

