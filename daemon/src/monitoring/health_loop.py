# /opt/bmas/daemon/src/monitoring/health_loop.py
"""Background system health monitoring loop."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from config import AGENT_ENDPOINTS
from database import check_sqlite_health

logger = logging.getLogger("bmas.daemon")


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


async def system_health_loop(app: FastAPI):
    """Background loop that publishes system health to Pub/Sub."""
    orch = app.state.orchestrator
    client = app.state.health_client
    tick = 0
    while True:
        try:
            tick += 1
            # Daemon status every 5s
            redis_ok = False
            with contextlib.suppress(Exception):
                redis_ok = bool(await orch.bb.redis.ping())
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
