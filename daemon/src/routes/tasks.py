# /opt/bmas/daemon/src/routes/tasks.py
"""Task REST endpoints — list, detail, debate, cost, logs."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import database as db

router = APIRouter()


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
