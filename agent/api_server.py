# /opt/bmas/agent/api_server.py
"""
Hermes bMAS Agent API Server — Phase 1 (Runs API Integration).

Bridges the bMAS Daemon to the local Hermes Agent installation.
Primary path: POST /v1/runs + SSE event stream via the Hermes Gateway (:8642).
Fallback path: `hermes -z` one-shot CLI via subprocess (doc 06 §8).

Trace events are translated from Hermes SSE format to bMAS trace schema
(doc 06 §4) and batch-POSTed to the daemon's ingest endpoint.

Feature-gated: set HERMES_GATEWAY_URL to enable the Runs API path.
If unset, falls back to the legacy hermes -z subprocess.

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

# ── Phase 1: Runs API configuration ───────────────────────────────────────
# Set HERMES_GATEWAY_URL to enable the Runs API path.
# If unset, falls back to hermes -z subprocess (doc 06 §8).
HERMES_GATEWAY_URL = os.getenv("HERMES_GATEWAY_URL")  # e.g. http://localhost:8642
HERMES_GATEWAY_KEY = os.getenv("HERMES_GATEWAY_KEY", os.getenv("API_SERVER_KEY", ""))
DAEMON_INGEST_URL = os.getenv("DAEMON_INGEST_URL")    # e.g. http://192.168.4.240:9000
BMAS_NODE_KEY = os.getenv("BMAS_NODE_KEY", "")

# SSE consume timeout — how long to wait for the next SSE event before
# considering the connection stalled (seconds).  Hermes runs can take
# minutes with tool calls, so this is generous.
SSE_READ_TIMEOUT = int(os.getenv("SSE_READ_TIMEOUT", "600"))

# Trace batch settings
TRACE_BATCH_SIZE = int(os.getenv("TRACE_BATCH_SIZE", "10"))
TRACE_FLUSH_INTERVAL = float(os.getenv("TRACE_FLUSH_INTERVAL", "2.0"))

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
    declined = "declined"
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
        None, description="Override default timeout (seconds)", ge=10, le=3600
    )
    # ── Phase 1 additions ──────────────────────────────────────────────
    turn_id: Optional[str] = Field(
        None, description="Stable turn identifier for trace correlation"
    )
    model: Optional[str] = Field(
        None, description="Daemon-selected model (pool-drawn; falls back to LITELLM_MODEL)"
    )
    role: Optional[str] = Field(
        None, description="Opaque actor string for trace correlation"
    )
    # ── Phase 3a additions (doc 12 §2.5) ───────────────────────────────
    profile: Optional[str] = Field(
        None, description="Hermes profile name for role-scoped SOUL/toolset isolation"
    )


class TaskResponse(BaseModel):
    """Outgoing result to the bMAS Daemon — v2 with trace fields."""
    task_id: str
    status: TaskStatus
    result: str
    node_id: str
    request_id: str
    duration_ms: int
    timestamp: str
    # ── Phase 1 additions (all Optional for backward compat) ───────────
    turn_id: Optional[str] = None
    run_id: Optional[str] = None
    action: Optional[str] = None           # contribute | decline | clean
    entries: Optional[list[dict]] = None   # proposed board entries (entries_v1)
    usage: Optional[dict] = None           # {prompt_tokens, completion_tokens, total_tokens, model}
    trace_count: Optional[int] = None
    artifacts: Optional[list[dict]] = None
    envelope_fallback: Optional[bool] = None
    # Phase 5: stateful turns (doc 12 §5.2)
    response_id: Optional[str] = None      # run_id serves as response_id for Responses API


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    node_id: str
    hermes_available: bool
    litellm_reachable: bool
    litellm_url: str
    model: str
    runs_api_available: bool = False


# ── SSE Parser ─────────────────────────────────────────────────────────────

def parse_sse_line_buffer(lines: list[str]) -> list[tuple[str, dict]]:
    """Parse buffered SSE lines into (event_name, data_dict) tuples.

    Handles TWO SSE formats:

    1. Standard SSE (separate event: and data: lines):
        event: message.delta
        data: {"delta": "Hello"}

    2. Hermes Gateway format (event name embedded in data JSON):
        data: {"event": "message.delta", "delta": "Hello"}

    The live Hermes gateway (:8642) uses format #2 (verified 2026-06-10).
    We support both for forward compatibility.

    Each event is separated by a blank line.
    """
    events = []
    current_event = ""
    current_data_parts: list[str] = []

    for line in lines:
        stripped = line.rstrip("\r\n")

        if stripped == "":
            # End of event block — emit if we have data
            if current_data_parts:
                data_str = "\n".join(current_data_parts)
                try:
                    data = json.loads(data_str)
                except (json.JSONDecodeError, TypeError):
                    data = {"raw": data_str}

                # Hermes gateway format: event name is inside the JSON
                # data, not as a separate event: line.
                event_name = current_event
                if not event_name and isinstance(data, dict):
                    event_name = data.pop("event", "message")

                events.append((event_name or "message", data))
            current_event = ""
            current_data_parts = []
            continue

        if stripped.startswith("event:"):
            current_event = stripped[len("event:"):].strip()
        elif stripped.startswith("data:"):
            current_data_parts.append(stripped[len("data:"):].strip())
        elif stripped.startswith(":"):
            # Comment / keepalive — skip
            continue

    # Handle trailing event without final blank line
    if current_data_parts:
        data_str = "\n".join(current_data_parts)
        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            data = {"raw": data_str}

        event_name = current_event
        if not event_name and isinstance(data, dict):
            event_name = data.pop("event", "message")

        events.append((event_name or "message", data))

    return events


# ── Trace Translation (doc 06 §2 → §4) ────────────────────────────────────

def translate(
    hermes_event: str,
    hermes_data: dict,
    task_id: str,
    turn_id: str,
    seq: int,
    role: str,
    node: str,
) -> dict:
    """Translate a Hermes SSE event into a bMAS trace event (doc 06 §4).

    Returns a dict matching the bMAS trace event schema.
    """
    ts = datetime.now(timezone.utc).isoformat()
    base = {
        "trace_id": f"trace-{turn_id}",
        "task_id": task_id,
        "turn_id": turn_id,
        "seq": seq,
        "ts": ts,
        "role": role,
        "node": node,
    }

    if hermes_event == "message.delta":
        return {
            **base,
            "type": "reasoning",
            "data": {"text": hermes_data.get("delta", "")},
            "tokens": {"in": 0, "out": len(hermes_data.get("delta", "")) // 4},  # rough estimate
            "cost_usd": 0.0,
        }

    elif hermes_event == "reasoning.available":
        return {
            **base,
            "type": "reasoning",
            "data": {"text": hermes_data.get("text", "")},
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }

    elif hermes_event == "tool.started":
        return {
            **base,
            "type": "tool_call",
            "data": {
                "tool": hermes_data.get("name", hermes_data.get("tool", "unknown")),
                "args": hermes_data.get("arguments", hermes_data.get("args", {})),
            },
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }

    elif hermes_event == "tool.completed":
        result_str = hermes_data.get("result", hermes_data.get("output", ""))
        if isinstance(result_str, dict):
            result_str = json.dumps(result_str)
        return {
            **base,
            "type": "tool_result",
            "data": {
                "tool": hermes_data.get("name", hermes_data.get("tool", "unknown")),
                "ok": not hermes_data.get("error", False),
                "summary": str(result_str)[:500],
            },
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }

    elif hermes_event in ("approval.request", "approval.responded"):
        return {
            **base,
            "type": "approval_request",
            "data": {
                "action": hermes_data.get("action", "unknown"),
                "args": hermes_data.get("args", {}),
            },
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }

    elif hermes_event == "run.completed":
        usage = hermes_data.get("usage", {})
        return {
            **base,
            "type": "final",
            "data": {
                "summary": str(hermes_data.get("output", ""))[:500],
                "usage": usage,
            },
            "tokens": {
                "in": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                "out": usage.get("output_tokens", usage.get("completion_tokens", 0)),
            },
            "cost_usd": 0.0,  # Daemon computes this
        }

    elif hermes_event in ("run.failed", "run.cancelled"):
        return {
            **base,
            "type": "error",
            "data": {"message": hermes_data.get("error", f"Run {hermes_event.split('.')[1]}")},
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }

    else:
        # Unknown event — log and return a generic trace
        logger.debug(f"Unknown Hermes SSE event: {hermes_event}")
        return {
            **base,
            "type": "reasoning",
            "data": {"text": f"[{hermes_event}] {json.dumps(hermes_data)[:200]}"},
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }


# ── Trace Emitter ──────────────────────────────────────────────────────────

class TraceEmitter:
    """Batches and POSTs trace events to the daemon's ingest endpoint.

    Non-blocking — logs warnings on failure but never blocks the run.
    """

    def __init__(self, client: httpx.AsyncClient, task_id: str, turn_id: str):
        self.client = client
        self.task_id = task_id
        self.turn_id = turn_id
        self.buffer: list[dict] = []
        self._all_traces: list[dict] = []  # full record for final flush
        self._enabled = bool(DAEMON_INGEST_URL and BMAS_NODE_KEY)

    async def emit(self, trace: dict) -> None:
        """Add a trace event to the buffer; flush if batch full."""
        self._all_traces.append(trace)
        if not self._enabled:
            return
        self.buffer.append(trace)
        if len(self.buffer) >= TRACE_BATCH_SIZE:
            await self.flush()

    async def flush(self) -> None:
        """Send buffered traces to the daemon ingest endpoint."""
        if not self.buffer or not self._enabled:
            return
        batch = self.buffer[:]
        self.buffer.clear()
        try:
            resp = await self.client.post(
                f"{DAEMON_INGEST_URL}/ingest/traces/{self.task_id}/{self.turn_id}",
                json=batch,
                headers={"Authorization": f"Bearer {BMAS_NODE_KEY}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Trace ingest returned {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"Trace ingest failed: {e}")

    async def flush_all(self) -> None:
        """Final flush — send any remaining buffered traces."""
        await self.flush()

    @property
    def trace_count(self) -> int:
        return len(self._all_traces)

    @property
    def all_traces(self) -> list[dict]:
        return self._all_traces


# ── Lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify Hermes binary exists on startup; check Runs API availability."""
    has_hermes_bin = Path(HERMES_BIN).exists()
    has_runs_api = bool(HERMES_GATEWAY_URL)

    if not has_hermes_bin and not has_runs_api:
        logger.error(
            f"Neither Hermes binary ({HERMES_BIN}) nor HERMES_GATEWAY_URL is available"
        )
        raise RuntimeError("No execution backend configured")

    mode = "Runs API" if has_runs_api else "hermes -z (legacy)"
    logger.info(
        f"bMAS Agent API starting | node={NODE_ID} model={LITELLM_MODEL} mode={mode}"
    )
    if has_runs_api:
        logger.info(f"  Gateway: {HERMES_GATEWAY_URL}")
        logger.info(f"  Daemon ingest: {DAEMON_INGEST_URL or 'DISABLED'}")
    yield
    logger.info("bMAS Agent API shutting down")


app = FastAPI(
    title="Hermes bMAS Agent",
    version="3.0.0",
    description="FastAPI wrapper for bMAS Daemon → Hermes Agent integration (Phase 1)",
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


# ── Core Execution: Runs API (Primary) ─────────────────────────────────────

async def _run_via_api(
    description: str,
    role_prompt: Optional[str],
    context: Optional[dict],
    task_id: str,
    turn_id: str,
    role: str,
    model: str,
    request_id: str,
    profile: Optional[str] = None,
) -> tuple[TaskStatus, str, Optional[dict], int, Optional[str]]:
    """Execute a task via the Hermes Runs API (POST /v1/runs + SSE).

    Returns:
        (status, result_text, usage_dict, trace_count, run_id)
    """
    # Build the run input
    input_text = description
    if context:
        input_text += f"\n\n## Blackboard Context\n```json\n{json.dumps(context, indent=2)}\n```"

    run_payload = {
        "input": input_text,
        "model": model,
        "session_id": f"{task_id}:{role}",
    }
    if role_prompt:
        run_payload["instructions"] = role_prompt

    # Phase 5: Stateful turns — include previous_response_id for
    # cross-round memory via the Responses API (doc 12 §5.2)
    prev_response_id = (context or {}).get("previous_response_id")
    if prev_response_id:
        run_payload["previous_response_id"] = prev_response_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Submit the run
        headers = {"Authorization": f"Bearer {HERMES_GATEWAY_KEY}"}
        # Phase 3a: Log profile for traceability. Per-profile gateway
        # dispatch is Phase 3b; for now the default gateway processes
        # all profiles (role identity is in the instructions/SOUL).
        logger.info(
            f"[{request_id}] POST /v1/runs | model={model} "
            f"session={run_payload['session_id']} profile={profile or 'default'}"
        )

        try:
            resp = await client.post(
                f"{HERMES_GATEWAY_URL}/v1/runs",
                json=run_payload,
                headers=headers,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[{request_id}] Failed to submit run: {e}")
            return TaskStatus.failed, f"Run submission failed: {e}", None, 0, None

        run_data = resp.json()
        run_id = run_data.get("run_id", run_data.get("id", "unknown"))
        logger.info(f"[{request_id}] Run created: {run_id}")

        # 2. Consume the SSE event stream
        emitter = TraceEmitter(client, task_id, turn_id)

        # Emit synthetic turn_start (doc 06 §2 note: Hermes doesn't emit this)
        turn_start_trace = translate(
            "__synthetic_turn_start", {},
            task_id, turn_id, seq=0, role=role, node=NODE_ID,
        )
        turn_start_trace["type"] = "turn_start"
        turn_start_trace["data"] = {"objective": description[:200], "phase": "execute", "round": 1}
        await emitter.emit(turn_start_trace)

        trace_seq = 1
        final_output = ""
        final_usage = None
        status = TaskStatus.completed

        try:
            async with client.stream(
                "GET",
                f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}/events",
                headers=headers,
                timeout=httpx.Timeout(connect=10.0, read=float(SSE_READ_TIMEOUT), write=10.0, pool=10.0),
            ) as stream:
                line_buffer: list[str] = []

                async for raw_line in stream.aiter_lines():
                    line_buffer.append(raw_line)

                    # Process on blank line (event boundary)
                    if raw_line.strip() == "":
                        events = parse_sse_line_buffer(line_buffer)
                        line_buffer.clear()

                        for event_name, event_data in events:
                            bmas_trace = translate(
                                event_name, event_data,
                                task_id, turn_id,
                                seq=trace_seq, role=role, node=NODE_ID,
                            )
                            trace_seq += 1
                            await emitter.emit(bmas_trace)

                            if event_name == "run.completed":
                                final_output = str(event_data.get("output", ""))
                                final_usage = event_data.get("usage")
                            elif event_name in ("run.failed", "run.cancelled"):
                                final_output = event_data.get("error", f"Run {event_name}")
                                status = TaskStatus.failed

                # Process any trailing events
                if line_buffer:
                    events = parse_sse_line_buffer(line_buffer)
                    for event_name, event_data in events:
                        bmas_trace = translate(
                            event_name, event_data,
                            task_id, turn_id,
                            seq=trace_seq, role=role, node=NODE_ID,
                        )
                        trace_seq += 1
                        await emitter.emit(bmas_trace)

                        if event_name == "run.completed":
                            final_output = str(event_data.get("output", ""))
                            final_usage = event_data.get("usage")
                        elif event_name in ("run.failed", "run.cancelled"):
                            final_output = event_data.get("error", f"Run {event_name}")
                            status = TaskStatus.failed

        except httpx.ReadTimeout:
            logger.warning(f"[{request_id}] SSE stream timed out after {SSE_READ_TIMEOUT}s")
            status = TaskStatus.timeout
            final_output = f"SSE stream timed out after {SSE_READ_TIMEOUT}s"
        except Exception as e:
            logger.error(f"[{request_id}] SSE stream error: {e}")
            # Try to recover final state via poll
            try:
                poll_resp = await client.get(
                    f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}",
                    headers=headers,
                    timeout=10.0,
                )
                if poll_resp.status_code == 200:
                    poll_data = poll_resp.json()
                    final_output = str(poll_data.get("output", final_output))
                    final_usage = poll_data.get("usage", final_usage)
                    poll_status = poll_data.get("status", "")
                    if poll_status == "completed":
                        status = TaskStatus.completed
                    elif poll_status in ("failed", "cancelled"):
                        status = TaskStatus.failed
            except Exception:
                pass

        # 3. If we didn't get usage from SSE, poll the final state
        if final_usage is None and status == TaskStatus.completed:
            try:
                poll_resp = await client.get(
                    f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}",
                    headers=headers,
                    timeout=10.0,
                )
                if poll_resp.status_code == 200:
                    poll_data = poll_resp.json()
                    final_usage = poll_data.get("usage")
                    if not final_output:
                        final_output = str(poll_data.get("output", ""))
            except Exception as e:
                logger.warning(f"[{request_id}] Failed to poll final run state: {e}")

        # Normalize usage field names (Hermes uses input_tokens; we also accept prompt_tokens)
        if final_usage:
            normalized_usage = {
                "prompt_tokens": final_usage.get("input_tokens", final_usage.get("prompt_tokens", 0)),
                "completion_tokens": final_usage.get("output_tokens", final_usage.get("completion_tokens", 0)),
                "total_tokens": final_usage.get("total_tokens", 0),
                "model": model,
            }
            # Recompute total if missing
            if normalized_usage["total_tokens"] == 0:
                normalized_usage["total_tokens"] = (
                    normalized_usage["prompt_tokens"] + normalized_usage["completion_tokens"]
                )
            final_usage = normalized_usage

        # 4. Final flush of traces
        await emitter.flush_all()

        logger.info(
            f"[{request_id}] Run {run_id} {status.value} | "
            f"traces={emitter.trace_count} output_len={len(final_output)}"
        )

        return status, final_output, final_usage, emitter.trace_count, run_id


# ── Core Execution: hermes -z Fallback (doc 06 §8) ────────────────────────

async def _run_hermes(
    description: str,
    role_prompt: Optional[str],
    context: Optional[dict],
    timeout: int,
    request_id: str,
    task_id: str = "",
    turn_id: str = "",
    role: str = "agent",
    profile: Optional[str] = None,
) -> tuple[TaskStatus, str, Optional[dict], int, Optional[str]]:
    """Execute a task via `hermes -z` in a temporary workspace directory.

    Legacy fallback path (doc 06 §8). Emits a single synthetic trace
    (turn_start → final with the full stdout as one reasoning block,
    usage unknown).

    Returns:
        (status, result_text, usage_dict, trace_count, run_id)
    """
    workspace = Path(tempfile.mkdtemp(prefix=f"bmas-{request_id}-"))

    try:
        # Write persona as AGENTS.md (Hermes auto-discovers this file)
        if role_prompt:
            agents_content = role_prompt
            if context:
                # Exclude attachments from AGENTS.md context (they're staged as files)
                ctx_for_md = {k: v for k, v in context.items() if k != "attachments"}
                if ctx_for_md:
                    agents_content += (
                        f"\n\n## Blackboard Context\n"
                        f"```json\n{json.dumps(ctx_for_md, indent=2)}\n```"
                    )
            (workspace / "AGENTS.md").write_text(agents_content)

        # Stage uploaded file attachments into workspace (doc 17 §5)
        attachments = (context or {}).get("attachments", []) if context else []
        if attachments and DAEMON_INGEST_URL:
            await _stage_attachments(
                task_id=task_id or request_id,
                attachments=attachments,
                workspace=workspace,
                request_id=request_id,
            )

        # Build the hermes command
        # Phase 3a: prepend --profile <role> for role-scoped SOUL/toolset
        # isolation (doc 12 §2.5). When profile is None, uses the default
        # Hermes profile (backward compatible).
        cmd = [
            HERMES_BIN,
            *(["-p", profile] if profile else []),
            "-z", description,
            "--model", LITELLM_MODEL,
        ]

        logger.info(
            f"[{request_id}] Executing hermes -z (fallback) | "
            f"profile={profile or 'default'} timeout={timeout}s workspace={workspace}"
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
            return TaskStatus.timeout, f"Task timed out after {timeout}s", None, 0, None

        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error(
                f"[{request_id}] hermes exited with code {proc.returncode} | "
                f"stderr={errors[:500]}"
            )
            return TaskStatus.failed, errors or f"Exit code {proc.returncode}", None, 0, None

        # Emit synthetic traces (doc 06 §8: coarse trace rather than nothing)
        trace_count = 0
        if DAEMON_INGEST_URL and BMAS_NODE_KEY and task_id and turn_id:
            try:
                synthetic_traces = [
                    {
                        "trace_id": f"trace-{turn_id}",
                        "task_id": task_id,
                        "turn_id": turn_id,
                        "seq": 0,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "role": role,
                        "node": NODE_ID,
                        "type": "turn_start",
                        "data": {"objective": description[:200], "phase": "execute", "round": 1},
                        "tokens": {"in": 0, "out": 0},
                        "cost_usd": 0.0,
                    },
                    {
                        "trace_id": f"trace-{turn_id}",
                        "task_id": task_id,
                        "turn_id": turn_id,
                        "seq": 1,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "role": role,
                        "node": NODE_ID,
                        "type": "reasoning",
                        "data": {"text": output[:2000]},
                        "tokens": {"in": 0, "out": 0},
                        "cost_usd": 0.0,
                    },
                    {
                        "trace_id": f"trace-{turn_id}",
                        "task_id": task_id,
                        "turn_id": turn_id,
                        "seq": 2,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "role": role,
                        "node": NODE_ID,
                        "type": "final",
                        "data": {"summary": output[:500], "usage": None},
                        "tokens": {"in": 0, "out": 0},
                        "cost_usd": 0.0,
                    },
                ]
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{DAEMON_INGEST_URL}/ingest/traces/{task_id}/{turn_id}",
                        json=synthetic_traces,
                        headers={"Authorization": f"Bearer {BMAS_NODE_KEY}"},
                    )
                trace_count = 3
            except Exception as e:
                logger.warning(f"[{request_id}] Synthetic trace ingest failed: {e}")

        logger.info(f"[{request_id}] Task completed (fallback) | output_len={len(output)}")

        # Sync any files hermes created in outputs/ back to daemon (doc 17 §6)
        outputs_dir = workspace / "outputs"
        if outputs_dir.is_dir() and DAEMON_INGEST_URL:
            await _sync_artifacts(
                task_id=task_id or request_id,
                turn_id=turn_id or request_id,
                outputs_dir=outputs_dir,
                request_id=request_id,
            )

        # usage is null under the legacy path (doc 06 §3.1 note)
        return TaskStatus.completed, output, None, trace_count, None

    finally:
        # Always clean up the temporary workspace
        shutil.rmtree(workspace, ignore_errors=True)


# ── File Staging & Artifact Sync (doc 17 §5-6) ───────────────────────────

async def _stage_attachments(
    task_id: str,
    attachments: list[dict],
    workspace: Path,
    request_id: str,
) -> None:
    """Fetch uploaded files from daemon into workspace/inputs/ (doc 17 §5).

    Each attachment dict has: file_id, name, mime, bytes, sha256.
    Text previews are also written as .extracted.txt files.
    """
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(exist_ok=True)  # create outputs/ for agent use

    headers = {}
    if BMAS_NODE_KEY:
        headers["Authorization"] = f"Bearer {BMAS_NODE_KEY}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        for att in attachments:
            fid = att.get("file_id", "")
            name = att.get("name", "file")
            if not fid:
                continue

            try:
                resp = await client.get(
                    f"{DAEMON_INGEST_URL}/tasks/{task_id}/files/{fid}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    (inputs_dir / name).write_bytes(resp.content)
                    logger.info(f"[{request_id}] Staged file: {name} ({len(resp.content)} bytes)")

                    text_preview = att.get("text_preview", "")
                    if text_preview:
                        (inputs_dir / f"{name}.extracted.txt").write_text(text_preview)
                else:
                    logger.warning(
                        f"[{request_id}] Failed to fetch file {fid}: HTTP {resp.status_code}"
                    )
            except Exception as e:
                logger.warning(f"[{request_id}] Error staging file {fid}: {e}")


async def _sync_artifacts(
    task_id: str,
    turn_id: str,
    outputs_dir: Path,
    request_id: str,
) -> None:
    """Sync files in outputs/ back to daemon as artifacts (doc 17 §6).

    Walks the outputs directory and POSTs each file to
    /ingest/artifacts/{task_id}/{turn_id}.
    """
    import hashlib

    headers = {}
    if BMAS_NODE_KEY:
        headers["Authorization"] = f"Bearer {BMAS_NODE_KEY}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        for file_path in outputs_dir.rglob("*"):
            if not file_path.is_file():
                continue
            rel_path = str(file_path.relative_to(outputs_dir))
            content = file_path.read_bytes()
            sha256 = hashlib.sha256(content).hexdigest()

            try:
                resp = await client.post(
                    f"{DAEMON_INGEST_URL}/ingest/artifacts/{task_id}/{turn_id}",
                    headers=headers,
                    data={"rel_path": rel_path, "sha256": sha256},
                    files={"file": (file_path.name, content)},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(
                        f"[{request_id}] Synced artifact: {rel_path} "
                        f"v{data.get('version', '?')} ({len(content)} bytes)"
                    )
                else:
                    logger.warning(
                        f"[{request_id}] Failed to sync {rel_path}: "
                        f"HTTP {resp.status_code} {resp.text[:200]}"
                    )
            except Exception as e:
                logger.warning(f"[{request_id}] Error syncing {rel_path}: {e}")


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    """
    Health check with dependency verification.
    Checks Hermes binary, LiteLLM gateway, and Runs API availability.
    """
    hermes_ok = Path(HERMES_BIN).exists()
    litellm_ok = False
    runs_api_ok = False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LITELLM_URL.rstrip('/v1')}/health/readiness")
            litellm_ok = resp.status_code == 200
    except Exception:
        pass

    if HERMES_GATEWAY_URL:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{HERMES_GATEWAY_URL}/health",
                    headers={"Authorization": f"Bearer {HERMES_GATEWAY_KEY}"},
                )
                runs_api_ok = resp.status_code == 200
        except Exception:
            pass

    status = "healthy" if (hermes_ok or runs_api_ok) and litellm_ok else "degraded"
    return HealthResponse(
        status=status,
        node_id=NODE_ID,
        hermes_available=hermes_ok,
        litellm_reachable=litellm_ok,
        litellm_url=LITELLM_URL,
        model=LITELLM_MODEL,
        runs_api_available=runs_api_ok,
    )


@app.post("/execute", response_model=TaskResponse)
async def execute_task(req: TaskRequest, request: Request):
    """
    Execute a task with optional persona injection.

    Primary path: Hermes Runs API (POST /v1/runs + SSE stream).
    Fallback: hermes -z subprocess (if HERMES_GATEWAY_URL is unset).
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4())[:8])
    timeout = req.timeout or TASK_TIMEOUT_SECONDS
    turn_id = req.turn_id or f"turn-{str(uuid.uuid4())[:8]}"
    role = req.role or "agent"
    model = req.model or LITELLM_MODEL
    start = time.monotonic()

    profile = req.profile  # Phase 3a: Hermes profile for role isolation

    logger.info(
        f"[{request_id}] Received task={req.task_id} | "
        f"role={role} profile={profile or 'default'} turn={turn_id} model={model} "
        f"mode={'api' if HERMES_GATEWAY_URL else 'cli'} "
        f"context={'yes' if req.context else 'no'}"
    )

    if HERMES_GATEWAY_URL:
        # Primary: Runs API path
        status, result, usage, trace_count, run_id = await _run_via_api(
            description=req.description,
            role_prompt=req.role_prompt,
            context=req.context,
            task_id=req.task_id,
            turn_id=turn_id,
            role=role,
            model=model,
            request_id=request_id,
            profile=profile,
        )
    else:
        # Fallback: hermes -z subprocess
        status, result, usage, trace_count, run_id = await _run_hermes(
            description=req.description,
            role_prompt=req.role_prompt,
            context=req.context,
            timeout=timeout,
            request_id=request_id,
            task_id=req.task_id,
            turn_id=turn_id,
            role=role,
            profile=profile,
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
        # Phase 1 fields
        turn_id=turn_id,
        run_id=run_id,
        action="contribute" if status == TaskStatus.completed else None,
        entries=None,              # Entries parsing deferred to Phase 2/3
        usage=usage,
        trace_count=trace_count,
        artifacts=None,            # Artifact sync deferred to Phase 2F
        envelope_fallback=None,
        # Phase 5: stateful turns
        response_id=run_id,        # Hermes run_id doubles as response_id
    )
