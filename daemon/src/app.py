# /opt/bmas/daemon/src/app.py
"""
bMAS Daemon entry point.
Exposes a FastAPI interface for the Mission Control UI and CLI.
"""

import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
import httpx
from core.orchestrator import Orchestrator
from config import PROJECT_NAME
from database import init_db

from routes import submit, tasks, events, health
from monitoring.health_loop import system_health_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("bmas.daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite infrastructure (validates volume mount + aiosqlite)
    # This runs before Orchestrator so a missing volume fails fast.
    await init_db()
    logger.info("SQLite initialized")

    orch = Orchestrator()
    # Pre-flight: verify Redis connectivity
    try:
        await orch.bb.redis.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis not reachable at startup: {e} — will retry on first request")
    app.state.orchestrator = orch
    app.state.health_client = httpx.AsyncClient(timeout=3.0)

    # Start system health loop
    health_task = asyncio.create_task(system_health_loop(app))

    yield

    health_task.cancel()
    await app.state.health_client.aclose()
    await orch.close()


app = FastAPI(title=f"{PROJECT_NAME} — bMAS Daemon", version="1.0.0", lifespan=lifespan)

# Register route modules
app.include_router(submit.router)
app.include_router(tasks.router)
app.include_router(events.router)
app.include_router(health.router)
