# /opt/bmas/daemon/main.py
"""
bMAS Daemon entry point.
Exposes a FastAPI interface for the Mission Control UI and CLI.
"""

import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
import httpx
from orchestrator import Orchestrator
from config import AGENT_ENDPOINTS, PROJECT_NAME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("bmas.daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    orch = Orchestrator()
    # Pre-flight: verify Redis connectivity
    try:
        await orch.bb.redis.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis not reachable at startup: {e} — will retry on first request")
    app.state.orchestrator = orch
    app.state.health_client = httpx.AsyncClient(timeout=3.0)
    yield
    await app.state.health_client.aclose()
    await orch.close()


app = FastAPI(title=f"{PROJECT_NAME} — bMAS Daemon", version="1.0.0", lifespan=lifespan)


class TaskSubmission(BaseModel):
    task: str


@app.post("/submit")
async def submit_task(req: TaskSubmission):
    """Submit a task to the bMAS swarm."""
    result = await app.state.orchestrator.process_task(req.task)
    return result


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


@app.get("/state")
async def get_state():
    """Get the current Blackboard public state with live agent health."""
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


@app.get("/health")
async def health():
    """Health check with active dependency verification."""
    orch = app.state.orchestrator
    redis_ok = False
    try:
        redis_ok = bool(await orch.bb.redis.ping())
    except Exception:
        pass

    return {
        "status": "healthy" if redis_ok else "degraded",
        "redis_connected": redis_ok,
        "agents": list(AGENT_ENDPOINTS.keys()),
    }
