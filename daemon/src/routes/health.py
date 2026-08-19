# /opt/bmas/daemon/src/routes/health.py
"""Health and state endpoints."""

import asyncio
import contextlib
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter

import database as db
from config import AGENT_ENDPOINTS, COORDINATION_VARIANT, LITELLM_KEY, LITELLM_URL

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
        body = resp.json() if resp.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        return {
            "alive": True,
            "last_heartbeat": datetime.now(UTC).isoformat(),
            "current_task": None,
            "endpoint": url,
            "execution_backend": body.get("execution_backend"),
            "litellm_reachable": body.get("litellm_reachable"),
        }
    except Exception:
        return {
            "alive": False,
            "last_heartbeat": "",
            "current_task": None,
            "endpoint": url,
            "execution_backend": None,
            "litellm_reachable": False,
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
    client = app.state.health_client
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
    agent_roles = list(AGENT_ENDPOINTS.items())
    agent_results = await asyncio.gather(*(
        _check_agent_health(client, role, url)
        for role, url in agent_roles
    ))
    agent_health = {
        role: result
        for (role, _), result in zip(agent_roles, agent_results, strict=False)
    }

    litellm_ok = False
    with contextlib.suppress(Exception):
        response = await client.get(
            f"{LITELLM_URL.removesuffix('/v1')}/health/readiness",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
        )
        litellm_ok = response.is_success

    agents_ready = bool(agent_health) and all(
        value.get("alive") for value in agent_health.values()
    )
    degraded = (
        not redis_ok
        or not sqlite_ok
        or not litellm_ok
        or not agents_ready
        or bool(delivery_health.get("overloaded"))
        or _has_runtime_pressure(queue_health, runtime_health)
    )

    return {
        "status": "degraded" if degraded else "healthy",
        "redis_connected": redis_ok,
        "sqlite_connected": sqlite_ok,
        "litellm_connected": litellm_ok,
        "agents": list(AGENT_ENDPOINTS.keys()),
        "agent_health": agent_health,
        "runtime_id": COORDINATION_VARIANT,
        "event_delivery": delivery_health,
        "task_queue": queue_health,
        "runtime": runtime_health,
        "lifecycle": lifecycle_health,
    }


@router.get("/readiness")
async def readiness():
    """Return actionable checks for operators and first-run clients."""
    snapshot = await health()
    agent_health = snapshot["agent_health"]
    agents_ready = bool(agent_health) and all(
        value.get("alive") for value in agent_health.values()
    )
    delivery_ready = not snapshot["event_delivery"].get("overloaded", True)

    checks = [
        {
            "id": "redis",
            "label": "Redis",
            "ready": snapshot["redis_connected"],
            "detail": "The live projection and lock service is reachable.",
            "fix": "Run: docker compose logs redis",
        },
        {
            "id": "sqlite",
            "label": "SQLite",
            "ready": snapshot["sqlite_connected"],
            "detail": "The durable task database is writable.",
            "fix": "Run: docker compose logs daemon",
        },
        {
            "id": "litellm",
            "label": "LiteLLM",
            "ready": snapshot["litellm_connected"],
            "detail": "The model gateway is reachable.",
            "fix": "Run: docker compose logs litellm",
        },
        {
            "id": "agents",
            "label": "Execution agents",
            "ready": agents_ready,
            "detail": (
                f"{len(agent_health)} configured execution endpoint(s)."
                if agent_health
                else "No execution endpoint is configured."
            ),
            "fix": "Run: docker compose logs agent",
        },
        {
            "id": "runtime",
            "label": "Classic runtime",
            "ready": snapshot["runtime_id"] == "classic",
            "detail": f"Configured runtime: {snapshot['runtime_id']}.",
            "fix": "Set coordination.variant to classic in bmas.yaml.",
        },
        {
            "id": "delivery",
            "label": "Event delivery",
            "ready": delivery_ready,
            "detail": "The durable event outbox accepts new events.",
            "fix": "Run: docker compose logs daemon",
        },
    ]
    return {
        "status": "ready" if all(check["ready"] for check in checks) else "not_ready",
        "checks": checks,
        "agent_health": agent_health,
    }
