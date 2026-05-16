# /opt/bmas/daemon/src/routes/health.py
"""Health and state endpoints."""

import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter
import httpx
from config import AGENT_ENDPOINTS

router = APIRouter()


async def _check_agent_health(client: httpx.AsyncClient, role: str, url: str) -> dict:
    """Probe a single agent's health endpoint. Returns AgentStatus dict."""
    try:
        resp = await client.get(f"{url}/health")
        resp.raise_for_status()
        return {
            "alive": True,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
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
    for role, health in zip(health_coros.keys(), agent_results):
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
    from database import check_sqlite_health

    orch = app.state.orchestrator
    redis_ok = False
    try:
        redis_ok = bool(await orch.bb.redis.ping())
    except Exception:
        pass

    sqlite_ok = await check_sqlite_health()

    return {
        "status": "healthy" if (redis_ok and sqlite_ok) else "degraded",
        "redis_connected": redis_ok,
        "sqlite_connected": sqlite_ok,
        "agents": list(AGENT_ENDPOINTS.keys()),
    }
