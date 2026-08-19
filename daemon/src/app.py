# /opt/bmas/daemon/src/app.py
"""
bMAS Daemon entry point.
Exposes a FastAPI interface for the Mission Control UI and CLI.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from config import COORDINATION_VARIANT, PROJECT_NAME
from core.event_delivery import delivery_reconciliation_loop, stop_delivery_task
from core.orchestrator import Orchestrator
from core.variants import require_variant_class
from database import init_db
from monitoring.health_loop import system_health_loop
from routes import (
    artifacts,
    capabilities,
    datasets,
    events,
    files,
    health,
    hitl,
    ingest,
    settings,
    submit,
    tasks,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("bmas.daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    require_variant_class(COORDINATION_VARIANT)
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

    # Start the bounded admission queue after all shared clients are ready.
    await submit.start_task_workers(orch)

    # Reconcile durable events independently from task execution.
    delivery_task = asyncio.create_task(delivery_reconciliation_loop(orch.bb.redis))
    app.state.delivery_task = delivery_task

    # Start system health loop
    health_task = asyncio.create_task(system_health_loop(app))

    yield

    health_task.cancel()
    await stop_delivery_task(delivery_task)
    await submit.stop_task_workers()
    await app.state.health_client.aclose()
    await orch.close()


app = FastAPI(title=f"{PROJECT_NAME} — bMAS Daemon", version="1.0.0", lifespan=lifespan)

# Register route modules
app.include_router(submit.router)
app.include_router(capabilities.router)
app.include_router(datasets.router)
app.include_router(tasks.router)
app.include_router(events.router)
app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(files.router)
app.include_router(artifacts.router)
app.include_router(hitl.router)
app.include_router(settings.router)
