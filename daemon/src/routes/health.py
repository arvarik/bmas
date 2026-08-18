# /opt/bmas/daemon/src/routes/health.py
"""Health and state endpoints."""

import asyncio
import contextlib
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter

import database as db
from config import AGENT_ENDPOINTS

router = APIRouter()


def _has_runtime_pressure(queue: dict, runtime: dict) -> bool:
    """Return true when queues, recovery, or endpoint circuits need attention."""
    if queue.get("recovery_blocked_tasks", 0):
        return True
    if queue.get("queued_tasks", 0) >= queue.get("queue_capacity", 1):
        return True
    endpoint_requests = runtime.get("endpoint_requests", {})
    return any(
        endpoint.get("circuit") == "open"
        for endpoint in endpoint_requests.values()
    )


async def _check_agent_health(client: httpx.AsyncClient, role: str, url: str) -> dict:
    """Probe a single agent's health endpoint. Returns AgentStatus dict."""
    try:
        resp = await client.get(f"{url}/health")
        resp.raise_for_status()
        return {
            "alive": True,
            "last_heartbeat": datetime.now(UTC).isoformat(),
            "current_task": None,
        }
    except Exception:
        return {
            "alive": False,
            "last_heartbeat": "",
            "current_task": None,
        }


@router.get("/state")
async def get_state():
    """Get the current Blackboard public state with live agent health."""
    from app import app
    orch = app.state.orchestrator
    client = app.state.health_client

    # Fetch blackboard state and agent health in parallel
    state_coro = orch.bb.get_state()
    health_coros = {
        role: _check_agent_health(client, role, url)
        for role, url in AGENT_ENDPOINTS.items()
    }

    state, *agent_results = await asyncio.gather(
        state_coro,
        *health_coros.values(),
    )

    # Merge live agent health into the state
    for role, health in zip(health_coros.keys(), agent_results, strict=False):
        state["agents"][role] = health

    return state


@router.get("/health")
async def health():
    """Health check with active dependency verification.

    Reports status for all infrastructure dependencies:
    - Redis: blackboard, pub/sub, streams
    - SQLite: task history persistence

    Returns 'healthy' only when ALL dependencies are operational.
    Returns 'degraded' if any dependency is down (HTTP 200 still returned
    so container orchestrators can distinguish 'app running but degraded'
    from 'app crashed').
    """
    from app import app
    from routes.submit import task_queue_snapshot

    orch = app.state.orchestrator
    redis_ok = False
    with contextlib.suppress(Exception):
        redis_ok = bool(await orch.bb.redis.ping())

    sqlite_ok = await db.check_sqlite_health()
    delivery_health = {
        "status": "unavailable",
        "overloaded": True,
    }
    lifecycle_health = {
        "running_tasks": 0,
        "effective_actions": 0,
        "recovery_count": 0,
        "latest_checkpoint_at": None,
    }
    if sqlite_ok:
        with contextlib.suppress(Exception):
            delivery_health = await db.get_event_delivery_health()
        with contextlib.suppress(Exception):
            lifecycle_health = await db.get_lifecycle_health()

    queue_health = task_queue_snapshot()
    runtime_health = orch.runtime_snapshot()
    degraded = (
        not redis_ok
        or not sqlite_ok
        or bool(delivery_health.get("overloaded"))
        or _has_runtime_pressure(queue_health, runtime_health)
    )

    return {
        "status": "degraded" if degraded else "healthy",
        "redis_connected": redis_ok,
        "sqlite_connected": sqlite_ok,
        "agents": list(AGENT_ENDPOINTS.keys()),
        "event_delivery": delivery_health,
        "task_queue": queue_health,
        "runtime": runtime_health,
        "lifecycle": lifecycle_health,
    }
