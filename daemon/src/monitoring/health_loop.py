# /opt/bmas/daemon/src/monitoring/health_loop.py
"""Background system health monitoring loop."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from config import AGENT_ENDPOINTS, BMAS_EXECUTE_KEY
from database import check_sqlite_health

logger = logging.getLogger("bmas.daemon")


async def _check_agent_health(client: httpx.AsyncClient, role: str, url: str) -> dict:
    """Probe one agent's capability-aware readiness endpoint."""
    try:
        headers = (
            {"Authorization": f"Bearer {BMAS_EXECUTE_KEY}"}
            if BMAS_EXECUTE_KEY
            else None
        )
        resp = await client.get(f"{url}/health/detailed", headers=headers)
        probe = "detailed"
        if resp.status_code == 404:
            resp = await client.get(f"{url}/health", headers=headers)
            probe = "legacy"
        resp.raise_for_status()
        body = resp.json() if resp.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        return {
            "alive": True,
            "ready": bool(body.get("ready", False)),
            "last_heartbeat": datetime.now(UTC).isoformat(),
            "current_task": None,
            "probe": probe,
            "runs_api_ready": body.get("runs_api_ready", False),
            "hermes_status": body.get("hermes_status"),
            "missing_required_features": body.get(
                "missing_required_features", []
            ),
            "missing_required_endpoints": body.get(
                "missing_required_endpoints", []
            ),
        }
    except Exception:
        return {
            "alive": False,
            "ready": False,
            "last_heartbeat": "",
            "current_task": None,
            "probe": "unavailable",
            "runs_api_ready": False,
            "hermes_status": None,
            "missing_required_features": [],
            "missing_required_endpoints": [],
        }


async def system_health_loop(app: FastAPI):
    """Background loop that publishes system health to Pub/Sub."""
    orch = app.state.orchestrator
    client = app.state.health_client
    tick = 0
    sqlite_ok = True  # Assume healthy until first probe
    while True:
        try:
            tick += 1
            # Redis ping every 5s (cheap TCP round-trip)
            redis_ok = False
            with contextlib.suppress(Exception):
                redis_ok = bool(await orch.bb.redis.ping())

            # SQLite check every 30s (every 6 ticks) — opening a connection
            # with 4 PRAGMAs is expensive for a pure liveness signal.
            if tick % 6 == 1:
                sqlite_ok = await check_sqlite_health()

            daemon_status = {
                "status": "healthy" if (redis_ok and sqlite_ok) else "degraded",
                "redis_connected": redis_ok,
                "sqlite_connected": sqlite_ok,
            }
            app.state.last_daemon_status = daemon_status
            await orch.bb.publish_system_event("daemon-status", daemon_status)

            # Agent health every 10s (every other tick)
            if tick % 2 == 0:
                agent_health = {}
                for role, url in AGENT_ENDPOINTS.items():
                    health = await _check_agent_health(client, role, url)
                    agent_health[role] = health
                app.state.last_agent_health = agent_health
                await orch.bb.publish_system_event("agent-health", agent_health)

            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"System health loop error: {e}")
            await asyncio.sleep(5)
