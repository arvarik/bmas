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

from benchmarks import scheduler as benchmark_scheduler
from config import COORDINATION_VARIANT, PROJECT_NAME
from core.event_delivery import delivery_reconciliation_loop, stop_delivery_task
from core.orchestrator import Orchestrator
from core.variants import require_variant_class
from database import init_db
from monitoring.health_loop import system_health_loop
from routes import (
    agent_protocol,
    artifacts,
    benchmarks,
    capabilities,
    datasets,
    evaluation,
    events,
    files,
    health,
    hitl,
    ingest,
    recovery,
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
    await benchmark_scheduler.start_scheduler()

    # Reconcile durable events independently from task execution.
    delivery_task = asyncio.create_task(delivery_reconciliation_loop(orch.bb.redis))
    app.state.delivery_task = delivery_task

    # Start system health loop
    health_task = asyncio.create_task(system_health_loop(app))

    # Calibrate every judge anchor set on its weekly schedule.
    from benchmarks import judge_calibration

    calibration_task = asyncio.create_task(
        judge_calibration.calibration_loop(),
    )
    app.state.calibration_task = calibration_task

    # Restore a fresh backup on a schedule when an operator enables it.
    import restore_test

    restore_task = None
    if restore_test.RESTORE_TEST_INTERVAL_SECONDS > 0:
        restore_task = asyncio.create_task(restore_test.restore_test_loop())
    app.state.restore_test_task = restore_task

    yield

    if restore_task is not None:
        restore_task.cancel()
    calibration_task.cancel()
    health_task.cancel()
    await stop_delivery_task(delivery_task)
    await benchmark_scheduler.stop_scheduler()
    await submit.stop_task_workers()
    await app.state.health_client.aclose()
    await orch.close()


app = FastAPI(title=f"{PROJECT_NAME} — bMAS Daemon", version="1.0.0", lifespan=lifespan)

# Every request authenticates at the edge before any route runs.
import edge_access  # noqa: E402

app.middleware("http")(edge_access.enforce_edge_access)

# Register route modules
app.include_router(submit.router)
app.include_router(benchmarks.router)
app.include_router(evaluation.router)
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
app.include_router(recovery.router)
app.include_router(agent_protocol.router)
