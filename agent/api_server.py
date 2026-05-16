# /opt/bmas/agent/api_server.py
"""
Hermes bMAS Agent API Server.

Bridges the bMAS Daemon (Phase 5) to the local Hermes Agent installation.
Uses `hermes -z` (one-shot CLI) via subprocess for clean venv isolation.
Persona injection via per-task AGENTS.md (Hermes's native context discovery).

Deployed to each edge node LXC. The canonical source lives in the bMAS repo
at `agent/api_server.py`. To deploy updates, copy this file to the target
node at `/opt/bmas/api_server.py` and restart the hermes-agent service.

Author: bMAS Infrastructure
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Configuration ──────────────────────────────────────────────────────────

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000/v1")
LITELLM_MODEL = os.getenv("LITELLM_MODEL", "medium")
HERMES_BIN = os.getenv("HERMES_BIN", "/usr/local/bin/hermes")
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "120"))
NODE_ID = os.getenv("NODE_ID", "agent-node1")

# ── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("bmas.agent")


# ── Models ─────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    completed = "completed"
    failed = "failed"
    timeout = "timeout"


class TaskRequest(BaseModel):
    """Incoming task from the bMAS Daemon."""
    task_id: str = Field(..., description="Unique task identifier from the orchestrator")
    description: str = Field(..., min_length=1, description="Task to execute")
    role_prompt: Optional[str] = Field(
        None, description="Ephemeral persona injected as AGENTS.md for this task"
    )
    context: Optional[dict] = Field(
        None, description="Blackboard context snapshot for situational awareness"
    )
    timeout: Optional[int] = Field(
        None, description="Override default timeout (seconds)", ge=10, le=600
    )


class TaskResponse(BaseModel):
    """Outgoing result to the bMAS Daemon."""
    task_id: str
    status: TaskStatus
    result: str
    node_id: str
    request_id: str
    duration_ms: int
    timestamp: str


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    node_id: str
    hermes_available: bool
    litellm_reachable: bool
    litellm_url: str
    model: str




# ── Lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify Hermes binary exists on startup."""
    if not Path(HERMES_BIN).exists():
        logger.error(f"Hermes binary not found at {HERMES_BIN}")
        raise RuntimeError(f"Hermes binary not found: {HERMES_BIN}")
    logger.info(f"bMAS Agent API starting | node={NODE_ID} model={LITELLM_MODEL}")
    yield
    logger.info("bMAS Agent API shutting down")


app = FastAPI(
    title="Hermes bMAS Agent",
    version="2.1.0",
    description="FastAPI wrapper for bMAS Daemon → Hermes Agent integration",
    lifespan=lifespan,
)


# ── Middleware ─────────────────────────────────────────────────────────────

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a unique request ID to every request for log correlation."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ── Core Execution ─────────────────────────────────────────────────────────

async def _run_hermes(
    description: str,
    role_prompt: Optional[str],
    context: Optional[dict],
    timeout: int,
    request_id: str,
) -> tuple[TaskStatus, str]:
    """
    Execute a task via `hermes -z` in a temporary workspace directory.

    Persona injection uses AGENTS.md — Hermes's native context-file
    discovery mechanism. Each task gets an isolated workspace that is
    cleaned up after execution.
    """
    workspace = Path(tempfile.mkdtemp(prefix=f"bmas-{request_id}-"))

    try:
        # Write persona as AGENTS.md (Hermes auto-discovers this file)
        if role_prompt:
            agents_content = role_prompt
            if context:
                agents_content += (
                    f"\n\n## Blackboard Context\n"
                    f"```json\n{json.dumps(context, indent=2)}\n```"
                )
            (workspace / "AGENTS.md").write_text(agents_content)

        # Build the hermes command
        cmd = [
            HERMES_BIN, "-z", description,
            "--model", LITELLM_MODEL,
        ]

        logger.info(
            f"[{request_id}] Executing hermes -z | "
            f"timeout={timeout}s workspace={workspace}"
        )

        # Run as async subprocess with timeout
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env={
                **os.environ,
                "HOME": os.environ.get("HOME", "/root"),
                "PATH": f"/usr/local/bin:{os.environ.get('PATH', '')}",
            },
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"[{request_id}] Task timed out after {timeout}s")
            return TaskStatus.timeout, f"Task timed out after {timeout}s"

        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error(
                f"[{request_id}] hermes exited with code {proc.returncode} | "
                f"stderr={errors[:500]}"
            )
            return TaskStatus.failed, errors or f"Exit code {proc.returncode}"

        logger.info(f"[{request_id}] Task completed | output_len={len(output)}")
        return TaskStatus.completed, output

    finally:
        # Always clean up the temporary workspace
        shutil.rmtree(workspace, ignore_errors=True)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    """
    Health check with dependency verification.
    Checks both Hermes binary availability and LiteLLM gateway reachability.
    """
    hermes_ok = Path(HERMES_BIN).exists()
    litellm_ok = False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LITELLM_URL.rstrip('/v1')}/health/readiness")
            litellm_ok = resp.status_code == 200
    except Exception:
        pass

    status = "healthy" if (hermes_ok and litellm_ok) else "degraded"
    return HealthResponse(
        status=status,
        node_id=NODE_ID,
        hermes_available=hermes_ok,
        litellm_reachable=litellm_ok,
        litellm_url=LITELLM_URL,
        model=LITELLM_MODEL,
    )


@app.post("/execute", response_model=TaskResponse)
async def execute_task(req: TaskRequest, request: Request):
    """
    Execute a task with optional persona injection.

    The bMAS Daemon calls this endpoint to assign work.
    Each invocation is stateless — a fresh `hermes -z` subprocess
    with an isolated workspace directory.
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4())[:8])
    timeout = req.timeout or TASK_TIMEOUT_SECONDS
    start = time.monotonic()

    logger.info(
        f"[{request_id}] Received task={req.task_id} | "
        f"role={'custom' if req.role_prompt else 'default'} "
        f"context={'yes' if req.context else 'no'}"
    )

    status, result = await _run_hermes(
        description=req.description,
        role_prompt=req.role_prompt,
        context=req.context,
        timeout=timeout,
        request_id=request_id,
    )

    duration_ms = int((time.monotonic() - start) * 1000)

    return TaskResponse(
        task_id=req.task_id,
        status=status,
        result=result,
        node_id=NODE_ID,
        request_id=request_id,
        duration_ms=duration_ms,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

