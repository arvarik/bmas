# /opt/bmas/agent/api_server.py
"""Execution API for the classic bMAS runtime.

The starter backend calls LiteLLM directly without tools. Advanced nodes can
execute through the Hermes Runs API or the Hermes CLI. The service translates
execution events and sends bounded logs and traces to the daemon.
"""

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Configuration ──────────────────────────────────────────────────────────

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000/v1")
LITELLM_MODEL = os.getenv("LITELLM_MODEL", "medium")
LITELLM_API_KEY = os.getenv(
    "LITELLM_API_KEY", os.getenv("LITELLM_MASTER_KEY", "")
)
HERMES_BIN = os.getenv("HERMES_BIN", "/usr/local/bin/hermes")
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "120"))
NODE_ID = os.getenv("NODE_ID", "agent-node1")
EXECUTION_BACKEND = os.getenv("BMAS_EXECUTION_BACKEND", "auto").strip().lower()

if EXECUTION_BACKEND not in {"auto", "litellm", "hermes"}:
    raise RuntimeError(
        "BMAS_EXECUTION_BACKEND must be auto, litellm, or hermes"
    )

# ── Phase 1: Runs API configuration ───────────────────────────────────────
# Set HERMES_GATEWAY_URL to enable the Runs API path.
# If unset, falls back to hermes -z subprocess (doc 06 §8).
HERMES_GATEWAY_URL = os.getenv("HERMES_GATEWAY_URL")  # e.g. http://localhost:8642
HERMES_GATEWAY_KEY = os.getenv("HERMES_GATEWAY_KEY", os.getenv("API_SERVER_KEY", ""))
DAEMON_INGEST_URL = os.getenv("DAEMON_INGEST_URL")    # e.g. http://192.168.4.240:9000
BMAS_NODE_KEY = os.getenv("BMAS_NODE_KEY", "")
BMAS_EXECUTE_KEY = os.getenv("BMAS_EXECUTE_KEY", "")

# SSE consume timeout — how long to wait for the next SSE event before
# considering the connection stalled (seconds).  Hermes runs can take
# minutes with tool calls, so this is generous.
SSE_READ_TIMEOUT = int(os.getenv("SSE_READ_TIMEOUT", "600"))
CANCELLATION_TIMEOUT_SECONDS = float(
    os.getenv("CANCELLATION_TIMEOUT_SECONDS", "5")
)

# Trace batch settings
TRACE_BATCH_SIZE = int(os.getenv("TRACE_BATCH_SIZE", "10"))
TRACE_FLUSH_INTERVAL = float(os.getenv("TRACE_FLUSH_INTERVAL", "2.0"))
TRACE_FLUSH_RETRIES = int(os.getenv("TRACE_FLUSH_RETRIES", "3"))
TRACE_RETRY_BASE_SECONDS = float(os.getenv("TRACE_RETRY_BASE_SECONDS", "0.25"))
TRACE_SPOOL_DIR = Path(os.getenv("TRACE_SPOOL_DIR", "/tmp/bmas-trace-spool"))
TRACE_DRAIN_TIMEOUT_SECONDS = float(os.getenv("TRACE_DRAIN_TIMEOUT_SECONDS", "5"))
TRACE_SPOOL_MAX_FILES = int(os.getenv("TRACE_SPOOL_MAX_FILES", "10000"))
TRACE_SPOOL_MAX_BYTES = int(os.getenv("TRACE_SPOOL_MAX_BYTES", str(256 * 1024 * 1024)))
TRACE_EVENT_MAX_BYTES = int(os.getenv("TRACE_EVENT_MAX_BYTES", str(64 * 1024)))
TRACE_MEMORY_MAX_EVENTS = int(os.getenv("TRACE_MEMORY_MAX_EVENTS", "1000"))
LOG_RECORD_MAX_BYTES = int(os.getenv("LOG_RECORD_MAX_BYTES", str(64 * 1024)))
LOG_BUFFER_MAX_RECORDS = int(os.getenv("LOG_BUFFER_MAX_RECORDS", "1000"))

# A completed activation stays available for daemon retries. The cache is local
# to this agent server. Stable activation IDs also prevent concurrent runs.
ACTIVATION_CACHE_TTL_SECONDS = float(
    os.getenv("ACTIVATION_CACHE_TTL_SECONDS", "3600")
)
ACTIVATION_CACHE_MAX_ENTRIES = int(
    os.getenv("ACTIVATION_CACHE_MAX_ENTRIES", "1000")
)
ACTIVATION_CACHE_MAX_BYTES = int(
    os.getenv("ACTIVATION_CACHE_MAX_BYTES", str(64 * 1024 * 1024))
)
ACTIVATION_CACHE_DIR = Path(
    os.getenv("ACTIVATION_CACHE_DIR", str(TRACE_SPOOL_DIR / "activations"))
)
ACTIVATION_RUNNING_TTL_SECONDS = float(
    os.getenv("ACTIVATION_RUNNING_TTL_SECONDS", "7200")
)
ACTIVATION_UNCERTAIN_TTL_SECONDS = float(
    os.getenv("ACTIVATION_UNCERTAIN_TTL_SECONDS", "21600")
)

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
    activation_id: Optional[str] = Field(
        None, description="Stable activation identifier for idempotent execution"
    )
    session_id: Optional[str] = Field(
        None, description="Daemon-supplied actor session identifier"
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
    execution_backend: str


def _selected_execution_backend() -> str:
    """Return the active execution backend identifier."""
    if EXECUTION_BACKEND == "litellm":
        return "litellm"
    if HERMES_GATEWAY_URL:
        return "hermes-runs-api"
    if Path(HERMES_BIN).exists():
        return "hermes-cli"
    return "unavailable"


def _result_envelope(result: str) -> tuple[str | None, list[dict] | None]:
    """Read the optional entries contract from a completed model result."""
    text = result.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    action = parsed.get("action")
    resolved_action = (
        str(action)
        if action in ("contribute", "decline", "clean", "condense")
        else None
    )
    entries = parsed.get("entries")
    resolved_entries = (
        [entry for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else None
    )
    return resolved_action, resolved_entries


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


def _log_hermes_event(log: "LogEmitter", event_name: str, data: dict) -> None:
    """Translate a Hermes SSE event into a structured agent log line.

    This function writes reasoning, tool results, and errors as structured
    records. The log emitter bounds each record before storage or delivery.
    """
    try:
        if event_name == "reasoning.available":
            text = str(data.get("text", ""))
            if text.strip():
                log.log(f"Reasoning: {text[:160]}", level="info",
                        event="reasoning", text=text)
        elif event_name == "tool.started":
            tool = data.get("name", data.get("tool", "unknown"))
            args = data.get("arguments", data.get("args", {}))
            log.log(f"Tool call → {tool}", level="info",
                    event="tool_call", tool=tool, args=args)
        elif event_name == "tool.completed":
            tool = data.get("name", data.get("tool", "unknown"))
            result = data.get("result", data.get("output", ""))
            ok = not data.get("error", False)
            log.log(
                f"Tool result ← {tool} ({'ok' if ok else 'error'})",
                level="info" if ok else "warning",
                event="tool_result", tool=tool, ok=ok,
                result=result if isinstance(result, (str, int, float, bool)) else json.dumps(result),
            )
        elif event_name in ("approval.request", "approval.responded"):
            log.log(f"Approval {event_name.split('.')[1]}: {data.get('action', 'unknown')}",
                    level="warning", event="approval",
                    action=data.get("action"), args=data.get("args", {}))
        elif event_name in ("run.failed", "run.cancelled"):
            log.log(f"Run {event_name.split('.')[1]}: {data.get('error', '')}",
                    level="error", event="run_error", error=data.get("error", ""))
    except Exception:
        # Logging must never disrupt the run.
        pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write private JSON data with an atomic replace and disk flush."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    pending = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        pending.unlink(missing_ok=True)
        raise


def _json_size(value: object) -> int:
    """Return the compact UTF-8 JSON size for one value."""
    return len(
        json.dumps(value, default=str, separators=(",", ":")).encode("utf-8")
    )


def _truncate_utf8(value: object, max_bytes: int) -> str:
    """Return a UTF-8-safe preview that fits the byte limit."""
    encoded = str(value).encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(value)
    return encoded[:max(0, max_bytes)].decode("utf-8", errors="ignore")


def _bound_trace(trace: dict) -> dict:
    """Bound one telemetry event without changing the agent result."""
    limit = max(512, TRACE_EVENT_MAX_BYTES)
    if _json_size(trace) <= limit:
        return trace
    bounded = dict(trace)
    for key in ("trace_id", "task_id", "turn_id", "ts", "role", "node"):
        if key in bounded:
            bounded[key] = _truncate_utf8(bounded[key], 128)
    data = trace.get("data", {})
    original_bytes = _json_size(data)
    reserved = _json_size({**bounded, "data": {}}) + 160
    preview_limit = max(64, limit - reserved)
    if bounded.get("type") == "final" and isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "model": _truncate_utf8(usage.get("model", ""), 128),
            }
        bounded["data"] = {
            "summary": _truncate_utf8(data.get("summary", ""), preview_limit),
            "usage": usage,
            "truncated": True,
            "original_bytes": original_bytes,
        }
    else:
        bounded["data"] = {
            "preview": _truncate_utf8(
                json.dumps(data, default=str, separators=(",", ":")),
                preview_limit,
            ),
            "truncated": True,
            "original_bytes": original_bytes,
        }
    while _json_size(bounded) > limit:
        value_key = "summary" if bounded.get("type") == "final" else "preview"
        current = str(bounded["data"].get(value_key, ""))
        excess = _json_size(bounded) - limit
        reduced = _truncate_utf8(current, max(0, len(current.encode("utf-8")) - excess - 8))
        if reduced == current:
            break
        bounded["data"][value_key] = reduced
    if _json_size(bounded) > limit:
        bounded["data"] = {
            "truncated": True,
            "original_bytes": original_bytes,
        }
    if _json_size(bounded) > limit:
        bounded = {
            "type": _truncate_utf8(bounded.get("type", "unknown"), 32),
            "seq": bounded.get("seq", 0),
            "data": {"truncated": True, "original_bytes": original_bytes},
        }
    return bounded


def _bound_log_record(record: dict) -> dict:
    """Bound one log record and mark truncated fields."""
    limit = max(512, LOG_RECORD_MAX_BYTES)
    if _json_size(record) <= limit:
        return record
    bounded = dict(record)
    for key in ("agent_role", "level", "node", "turn_id", "request_id", "ts"):
        if key in bounded:
            bounded[key] = _truncate_utf8(bounded[key], 128)
    fields = record.get("fields", {})
    original_bytes = _json_size(fields)
    reserved = _json_size({**bounded, "fields": {}}) + 160
    bounded["fields"] = {
        "preview": _truncate_utf8(
            json.dumps(fields, default=str, separators=(",", ":")),
            max(64, limit - reserved),
        ),
        "truncated": True,
        "original_bytes": original_bytes,
    }
    bounded["message"] = _truncate_utf8(bounded.get("message", ""), 512)
    while _json_size(bounded) > limit:
        current = str(bounded.get("message", ""))
        excess = _json_size(bounded) - limit
        reduced = _truncate_utf8(current, max(0, len(current.encode("utf-8")) - excess - 8))
        if reduced == current:
            break
        bounded["message"] = reduced
    if _json_size(bounded) > limit:
        bounded["fields"] = {
            "truncated": True,
            "original_bytes": original_bytes,
        }
    if _json_size(bounded) > limit:
        bounded = {
            "level": _truncate_utf8(bounded.get("level", "info"), 16),
            "message": "Telemetry record truncated",
            "fields": {"truncated": True, "original_bytes": original_bytes},
        }
    return bounded


@contextmanager
def _trace_spool_lock():
    """Serialize trace capacity checks and writes across agent processes."""
    TRACE_SPOOL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = TRACE_SPOOL_DIR / ".trace-spool.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _trace_file_priority(path: Path) -> int:
    """Return one for files that contain a final or error trace."""
    try:
        record = json.loads(path.read_text())
        traces = record.get("traces", []) if isinstance(record, dict) else []
        return int(any(
            isinstance(trace, dict) and trace.get("type") in ("final", "error")
            for trace in traces
        ))
    except Exception:
        return 0


def _reserve_trace_spool(incoming_bytes: int, incoming_priority: int) -> bool:
    """Evict old trace files until one bounded spool record fits."""
    TRACE_SPOOL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = [
        path
        for pattern in ("*.json", "*.bad")
        for path in TRACE_SPOOL_DIR.glob(pattern)
        if path.is_file()
    ]
    sending_files = [
        path for path in TRACE_SPOOL_DIR.glob("*.sending") if path.is_file()
    ]
    total_bytes = sum(path.stat().st_size for path in [*files, *sending_files])
    total_files = len(files) + len(sending_files)
    files.sort(key=lambda path: (_trace_file_priority(path), path.stat().st_mtime))
    if incoming_priority == 0:
        files = [path for path in files if _trace_file_priority(path) == 0]
    while files and (
        total_files + 1 > max(0, TRACE_SPOOL_MAX_FILES)
        or total_bytes + incoming_bytes > max(0, TRACE_SPOOL_MAX_BYTES)
    ):
        victim = files.pop(0)
        try:
            size = victim.stat().st_size
            victim.unlink()
            total_bytes -= size
            total_files -= 1
        except FileNotFoundError:
            pass
    return (
        total_files + 1 <= max(0, TRACE_SPOOL_MAX_FILES)
        and total_bytes + incoming_bytes <= max(0, TRACE_SPOOL_MAX_BYTES)
    )


# ── Trace Emitter ──────────────────────────────────────────────────────────

class TraceEmitter:
    """Batches and POSTs trace events to the daemon's ingest endpoint.

    Failed final batches move to a durable spool. A later emitter retries them.
    """

    def __init__(self, client: httpx.AsyncClient, task_id: str, turn_id: str):
        self.client = client
        self.task_id = task_id
        self.turn_id = turn_id
        self.buffer: list[dict] = []
        self._all_traces: list[dict] = []  # full record for final flush
        self._trace_count = 0
        self._enabled = bool(DAEMON_INGEST_URL and BMAS_NODE_KEY)
        self._drain_task: Optional[asyncio.Task] = None

    async def emit(self, trace: dict) -> None:
        """Add a trace event to the buffer; flush if batch full."""
        trace = _bound_trace(trace)
        self._trace_count += 1
        self._all_traces.append(trace)
        if len(self._all_traces) > max(1, TRACE_MEMORY_MAX_EVENTS):
            removable = next(
                (
                    index
                    for index, item in enumerate(self._all_traces)
                    if item.get("type") not in ("final", "error")
                ),
                0,
            )
            self._all_traces.pop(removable)
        if not self._enabled:
            return
        self.buffer.append(trace)
        if len(self.buffer) >= TRACE_BATCH_SIZE:
            self._spool_buffer()
            self._start_background_drain()
        while len(self.buffer) > max(1, TRACE_MEMORY_MAX_EVENTS):
            removable = next(
                (
                    index
                    for index, item in enumerate(self.buffer)
                    if item.get("type") not in ("final", "error")
                ),
                None,
            )
            if removable is None:
                self.buffer.pop(0)
                logger.error("Dropped the oldest terminal trace after spool backpressure")
                continue
            self.buffer.pop(removable)
            logger.error("Dropped one non-terminal trace after spool backpressure")

    async def _post_batch(self, task_id: str, turn_id: str, batch: list[dict]) -> bool:
        """Post one batch. Return true only after the daemon accepts it."""
        try:
            resp = await self.client.post(
                f"{DAEMON_INGEST_URL}/ingest/traces/{task_id}/{turn_id}",
                json=batch,
                headers={"Authorization": f"Bearer {BMAS_NODE_KEY}"},
                timeout=10.0,
            )
            if 200 <= resp.status_code < 300:
                return True
            logger.warning(
                f"Trace ingest returned {resp.status_code}: {resp.text[:200]}"
            )
        except Exception as exc:
            logger.warning(f"Trace ingest failed: {exc}")
        return False

    async def _post_with_retries(
        self, task_id: str, turn_id: str, batch: list[dict]
    ) -> bool:
        """Retry one trace batch with a short exponential delay."""
        attempts = max(1, TRACE_FLUSH_RETRIES)
        for attempt in range(attempts):
            if await self._post_batch(task_id, turn_id, batch):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(TRACE_RETRY_BASE_SECONDS * (2 ** attempt))
        return False

    def _spool(self, task_id: str, turn_id: str, batch: list[dict]) -> bool:
        """Write one failed batch to an atomic disk spool file."""
        try:
            spool_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
            ready_path = TRACE_SPOOL_DIR / f"{spool_id}.json"
            payload = {
                "task_id": task_id,
                "turn_id": turn_id,
                "traces": batch,
            }
            priority = int(any(
                trace.get("type") in ("final", "error") for trace in batch
            ))
            with _trace_spool_lock():
                if not _reserve_trace_spool(_json_size(payload), priority):
                    logger.error("Trace spool capacity is exhausted")
                    return False
                _atomic_write_json(ready_path, payload)
            logger.debug(
                f"Spool saved {len(batch)} traces for task={task_id} turn={turn_id}"
            )
            return True
        except Exception as exc:
            logger.error(f"Trace spool write failed: {exc}")
            return False

    def _spool_buffer(self) -> bool:
        """Move the current memory buffer to the durable trace queue."""
        if not self.buffer:
            return True
        batch = self.buffer[:]
        if not self._spool(self.task_id, self.turn_id, batch):
            return False
        del self.buffer[:len(batch)]
        return True

    def _start_background_drain(self) -> None:
        """Start one best-effort trace sender without blocking execution."""
        if not self._enabled:
            return
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_spool())

    async def _drain_spool(self) -> None:
        """Claim and retry durable trace batches from earlier requests."""
        if not TRACE_SPOOL_DIR.is_dir():
            return
        for ready_path in sorted(TRACE_SPOOL_DIR.glob("*.json")):
            claimed_path = ready_path.with_suffix(".sending")
            try:
                os.replace(ready_path, claimed_path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning(f"Trace spool claim failed: {exc}")
                continue

            try:
                record = json.loads(claimed_path.read_text())
                if not isinstance(record, dict) or not isinstance(
                    record.get("traces"), list
                ):
                    raise ValueError("Invalid trace spool record")
                accepted = await self._post_with_retries(
                    str(record["task_id"]),
                    str(record["turn_id"]),
                    list(record["traces"]),
                )
            except asyncio.CancelledError:
                try:
                    os.replace(claimed_path, ready_path)
                except OSError as exc:
                    logger.error(f"Trace spool cancellation release failed: {exc}")
                raise
            except Exception as exc:
                logger.error(f"Trace spool read failed for {claimed_path.name}: {exc}")
                bad_path = claimed_path.with_suffix(".bad")
                try:
                    os.replace(claimed_path, bad_path)
                except OSError as move_exc:
                    logger.error(f"Trace spool quarantine failed: {move_exc}")
                continue

            if accepted:
                claimed_path.unlink(missing_ok=True)
                continue

            try:
                os.replace(claimed_path, ready_path)
            except OSError as exc:
                logger.error(f"Trace spool release failed: {exc}")
            break

    async def flush(self) -> bool:
        """Queue buffered traces, then attempt durable queue delivery."""
        if not self._enabled:
            return True
        if not self._spool_buffer():
            return False
        await self._drain_spool()
        return not any(TRACE_SPOOL_DIR.glob("*.json"))

    async def flush_all(
        self,
        delivery_timeout: Optional[float] = TRACE_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        """Queue every trace and use a bounded delivery attempt."""
        if not self._enabled:
            return
        self._spool_buffer()
        if delivery_timeout is not None and delivery_timeout <= 0:
            return

        deadline = (
            None
            if delivery_timeout is None
            else asyncio.get_running_loop().time() + delivery_timeout
        )
        existing = self._drain_task
        if existing is not None and not existing.done():
            try:
                if deadline is None:
                    await existing
                else:
                    remaining = max(
                        0.0, deadline - asyncio.get_running_loop().time()
                    )
                    await asyncio.wait_for(existing, timeout=remaining)
            except TimeoutError:
                logger.warning("Trace spool drain exceeded its delivery deadline")

        if deadline is not None and deadline <= asyncio.get_running_loop().time():
            return
        drain = asyncio.create_task(self._drain_spool())
        try:
            if deadline is None:
                await drain
            else:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                await asyncio.wait_for(drain, timeout=remaining)
        except TimeoutError:
            logger.warning("Trace spool drain exceeded its delivery deadline")

    @property
    def trace_count(self) -> int:
        return self._trace_count

    @property
    def all_traces(self) -> list[dict]:
        return self._all_traces


def _recover_trace_spool_claims() -> None:
    """Restore trace batches that a previous server stopped while sending."""
    if not TRACE_SPOOL_DIR.is_dir():
        return
    for claimed_path in TRACE_SPOOL_DIR.glob("*.sending"):
        ready_path = claimed_path.with_suffix(".json")
        try:
            os.replace(claimed_path, ready_path)
        except OSError as exc:
            logger.warning(f"Trace spool recovery failed: {exc}")


# ── Structured Log Emitter ───────────────────────────────────────────────

class LogEmitter:
    """Emits structured, per-agent log records to the daemon's log collector.

    Each agent node owns its own logs (doc 04 seam rule 3): we POST structured
    records to the daemon's /ingest/logs/{task_id} endpoint so they flow into
    the same Redis stream + SQLite archive as daemon logs and show up,
    attributed to this agent/persona, in the Mission Control Logs tab.

    Records are buffered and flushed in batches; nothing here ever blocks or
    raises into the run path. Logs are also echoed to the local Python logger
    so container stdout stays useful.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        role: str,
        turn_id: str,
        request_id: str,
    ) -> None:
        self.client = client
        self.task_id = task_id
        self.role = role
        self.turn_id = turn_id
        self.request_id = request_id
        self.buffer: list[dict] = []
        self._enabled = bool(DAEMON_INGEST_URL and BMAS_NODE_KEY)

    def log(self, message: str, level: str = "info", **fields) -> None:
        """Buffer one bounded structured log record."""
        rec = {
            "agent_role": self.role,
            "level": level,
            "message": message,
            "node": NODE_ID,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "fields": {"node": NODE_ID, "turn_id": self.turn_id, **fields},
        }
        bounded = _bound_log_record(rec)
        self.buffer.append(bounded)
        if len(self.buffer) > max(1, LOG_BUFFER_MAX_RECORDS):
            self.buffer.pop(0)
            logger.warning("Dropped the oldest agent log after buffer backpressure")
        # Mirror to container stdout for local debugging.
        getattr(logger, level if level in ("info", "warning", "error", "debug") else "info")(
            f"[{self.request_id}] {self.role}: {bounded.get('message', '')}"
        )

    async def flush(self) -> None:
        """POST buffered log records to the daemon collector (best-effort)."""
        if not self.buffer:
            return
        batch = self.buffer[:]
        self.buffer.clear()
        if not self._enabled:
            return
        try:
            resp = await self.client.post(
                f"{DAEMON_INGEST_URL}/ingest/logs/{self.task_id}",
                json=batch,
                headers={"Authorization": f"Bearer {BMAS_NODE_KEY}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Log ingest returned {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"Log ingest failed: {e}")


async def _post_logs_oneshot(
    task_id: str, role: str, turn_id: str, request_id: str, records: list[tuple],
) -> None:
    """Post a batch of (message, level, fields) log records with a fresh client.

    Used by the subprocess fallback path where no long-lived client exists.
    Best-effort — never raises.
    """
    if not (DAEMON_INGEST_URL and BMAS_NODE_KEY) or not records:
        return
    payload = [
        _bound_log_record({
            "agent_role": role,
            "level": level,
            "message": message,
            "node": NODE_ID,
            "turn_id": turn_id,
            "request_id": request_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "fields": {"node": NODE_ID, "turn_id": turn_id, **(fields or {})},
        })
        for (message, level, fields) in records
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{DAEMON_INGEST_URL}/ingest/logs/{task_id}",
                json=payload,
                headers={"Authorization": f"Bearer {BMAS_NODE_KEY}"},
            )
    except Exception as e:
        logger.warning(f"One-shot log ingest failed: {e}")


# ── Lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify Hermes binary exists on startup; check Runs API availability."""
    has_runs_api = bool(HERMES_GATEWAY_URL)
    backend = _selected_execution_backend()

    if backend == "unavailable":
        logger.error(
            f"Neither Hermes binary ({HERMES_BIN}) nor HERMES_GATEWAY_URL is available"
        )
        raise RuntimeError("No execution backend configured")

    logger.info(
        f"bMAS Agent API starting | node={NODE_ID} model={LITELLM_MODEL} "
        f"backend={backend}"
    )
    if has_runs_api:
        logger.info(f"  Gateway: {HERMES_GATEWAY_URL}")
        logger.info(f"  Daemon ingest: {DAEMON_INGEST_URL or 'DISABLED'}")
    _recover_trace_spool_claims()
    yield
    logger.info("bMAS Agent API shutting down")


app = FastAPI(
    title="bMAS Execution Agent",
    version="3.0.0",
    description="Idempotent classic role execution through LiteLLM or Hermes.",
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

def _normalize_usage(usage: Optional[dict], model: str) -> Optional[dict]:
    """Return stable token fields and always include the selected model."""
    if not usage:
        return None
    normalized = {
        "prompt_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
        "completion_tokens": usage.get(
            "output_tokens", usage.get("completion_tokens", 0)
        ),
        "total_tokens": usage.get("total_tokens", 0),
        "model": model,
    }
    if normalized["total_tokens"] == 0:
        normalized["total_tokens"] = (
            normalized["prompt_tokens"] + normalized["completion_tokens"]
        )
    return normalized


async def _run_via_litellm(
    description: str,
    role_prompt: Optional[str],
    context: Optional[dict],
    task_id: str,
    turn_id: str,
    role: str,
    model: str,
    request_id: str,
    timeout: int = TASK_TIMEOUT_SECONDS,
) -> tuple[TaskStatus, str, Optional[dict], int, Optional[str]]:
    """Execute one tool-free starter activation through LiteLLM."""
    messages: list[dict[str, str]] = []
    if role_prompt:
        messages.append({"role": "system", "content": role_prompt})

    input_text = description
    if context:
        input_text += (
            "\n\n## Blackboard Context\n```json\n"
            f"{json.dumps(context, indent=2)}\n```"
        )
    messages.append({"role": "user", "content": input_text})

    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        log = LogEmitter(client, task_id, role, turn_id, request_id)
        log.log(
            f"Starting direct LiteLLM activation | model={model}",
            event="litellm_submit",
            model=model,
            objective=description[:500],
        )
        emitter = TraceEmitter(client, task_id, turn_id)
        try:
            response = await client.post(
                f"{LITELLM_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages},
            )
            response.raise_for_status()
            body = response.json()
            result = str(body["choices"][0]["message"]["content"])
            usage = _normalize_usage(body.get("usage"), model)
            await emitter.emit(
                translate(
                    "run.completed",
                    {"output": result, "usage": usage or {}},
                    task_id,
                    turn_id,
                    seq=1,
                    role=role,
                    node=NODE_ID,
                )
            )
            log.log(
                "Direct LiteLLM activation completed",
                event="litellm_complete",
                model=model,
            )
            await log.flush()
            await emitter.flush_all()
            return TaskStatus.completed, result, usage, emitter.trace_count, None
        except Exception as exc:
            message = f"LiteLLM execution failed: {exc}"
            await emitter.emit(
                translate(
                    "run.failed",
                    {"error": message},
                    task_id,
                    turn_id,
                    seq=1,
                    role=role,
                    node=NODE_ID,
                )
            )
            log.log(message, level="error", event="litellm_error", model=model)
            await log.flush()
            await emitter.flush_all()
            return TaskStatus.failed, message, None, emitter.trace_count, None

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
    session_id: Optional[str] = None,
    timeout: int = TASK_TIMEOUT_SECONDS,
    activation_key: Optional[str] = None,
    activation_fingerprint: str = "",
    resume_run_id: Optional[str] = None,
) -> tuple[TaskStatus, str, Optional[dict], int, Optional[str]]:
    """Execute a task via the Hermes Runs API (POST /v1/runs + SSE).

    Returns:
        (status, result_text, usage_dict, trace_count, run_id)
    """
    # Build the run input
    input_text = description
    if context:
        input_text += f"\n\n## Blackboard Context\n```json\n{json.dumps(context, indent=2)}\n```"

    actor_session_id = session_id or f"{task_id}:{role}"
    run_payload = {
        "input": input_text,
        "model": model,
        "session_id": actor_session_id,
    }
    if role_prompt:
        run_payload["instructions"] = role_prompt

    # Phase 5: Stateful turns — include previous_response_id for
    # cross-round memory via the Responses API (doc 12 §5.2)
    prev_response_id = (context or {}).get("previous_response_id")
    if prev_response_id:
        run_payload["previous_response_id"] = prev_response_id

    client_timeout = httpx.Timeout(
        connect=10.0,
        read=float(SSE_READ_TIMEOUT),
        write=10.0,
        pool=10.0,
    )
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        headers = {}
        if HERMES_GATEWAY_KEY:
            headers["Authorization"] = f"Bearer {HERMES_GATEWAY_KEY}"
        log = LogEmitter(client, task_id, role, turn_id, request_id)
        log.log(
            f"Starting run | model={model} profile={profile or 'default'}",
            level="info",
            event="run_submit",
            model=model,
            profile=profile or "default",
            session_id=run_payload["session_id"],
            objective=description[:500],
        )
        logger.info(
            f"[{request_id}] POST /v1/runs | model={model} "
            f"session={run_payload['session_id']} profile={profile or 'default'}"
        )

        run_id: Optional[str] = resume_run_id
        emitter = TraceEmitter(client, task_id, turn_id)
        trace_seq = 2_000_000_000 if resume_run_id else 1
        final_output = ""
        final_usage: Optional[dict] = None
        status = TaskStatus.failed
        saw_terminal = False

        async def consume_events(events: list[tuple[str, dict]]) -> None:
            """Translate events and update the run result."""
            nonlocal trace_seq, final_output, final_usage, status, saw_terminal
            for event_name, event_data in events:
                trace_data = dict(event_data)
                if event_name == "run.completed":
                    final_output = str(event_data.get("output", ""))
                    final_usage = _normalize_usage(event_data.get("usage"), model)
                    trace_data["usage"] = final_usage or {}
                    status = TaskStatus.completed
                    saw_terminal = True
                elif event_name in ("run.failed", "run.cancelled"):
                    final_output = str(
                        event_data.get("error", f"Run {event_name.split('.')[1]}")
                    )
                    status = TaskStatus.failed
                    saw_terminal = True

                bmas_trace = translate(
                    event_name,
                    trace_data,
                    task_id,
                    turn_id,
                    seq=trace_seq,
                    role=role,
                    node=NODE_ID,
                )
                trace_seq += 1
                await emitter.emit(bmas_trace)
                _log_hermes_event(log, event_name, trace_data)

        async def poll_until_terminal() -> None:
            """Poll after an SSE disconnect until Hermes reports a final state."""
            nonlocal trace_seq, final_output, final_usage, status, saw_terminal
            while not saw_terminal and run_id:
                poll_resp = await client.get(
                    f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}",
                    headers=headers,
                    timeout=10.0,
                )
                if poll_resp.status_code == 200:
                    poll_data = poll_resp.json()
                    poll_status = str(poll_data.get("status", "")).lower()
                    if poll_status in ("completed", "failed", "cancelled"):
                        event_name = (
                            "run.completed"
                            if poll_status == "completed"
                            else f"run.{poll_status}"
                        )
                        await consume_events([(event_name, poll_data)])
                        return
                elif poll_resp.status_code == 404:
                    await consume_events([(
                        "run.failed",
                        {"error": "Hermes lost the run after the SSE stream ended"},
                    )])
                    return
                elif resume_run_id:
                    raise RuntimeError(
                        f"Hermes reconciliation returned HTTP {poll_resp.status_code}"
                    )
                await asyncio.sleep(1.0)

        try:
            async with asyncio.timeout_at(deadline):
                if run_id is None:
                    try:
                        resp = await client.post(
                            f"{HERMES_GATEWAY_URL}/v1/runs",
                            json=run_payload,
                            headers=headers,
                        )
                        resp.raise_for_status()
                    except Exception as exc:
                        logger.error(f"[{request_id}] Failed to submit run: {exc}")
                        log.log(
                            f"Run submission failed: {exc}",
                            level="error",
                            event="run_submit_failed",
                            error=str(exc),
                        )
                        final_output = f"Run submission failed: {exc}"
                        status = TaskStatus.failed
                    else:
                        run_data = resp.json()
                        run_id = str(
                            run_data.get("run_id", run_data.get("id", "unknown"))
                        )
                        if activation_key:
                            persisted = await asyncio.to_thread(
                                _persist_activation_state,
                                activation_key,
                                "running",
                                activation_fingerprint,
                                run_id=run_id,
                            )
                            if not persisted:
                                record = await asyncio.to_thread(
                                    _load_activation_record,
                                    activation_key,
                                    time.time(),
                                )
                                if record and record.get("state") == "cancelled":
                                    await _stop_remote_run(
                                        client, run_id, headers, request_id
                                    )
                                    raise asyncio.CancelledError
                                raise RuntimeError(
                                    "The activation rejected its Hermes run ID"
                                )
                        logger.info(f"[{request_id}] Run created: {run_id}")
                        log.log(
                            f"Run created: {run_id}",
                            level="info",
                            event="run_created",
                            run_id=run_id,
                        )
                        turn_start_trace = translate(
                            "__synthetic_turn_start",
                            {},
                            task_id,
                            turn_id,
                            seq=0,
                            role=role,
                            node=NODE_ID,
                        )
                        turn_start_trace["type"] = "turn_start"
                        turn_start_trace["data"] = {
                            "objective": description[:200],
                            "phase": "execute",
                            "round": int((context or {}).get("round", 1) or 1),
                        }
                        await emitter.emit(turn_start_trace)
                else:
                    logger.info(f"[{request_id}] Reconciling Hermes run {run_id}")
                    log.log(
                        f"Reconciling run: {run_id}",
                        level="info",
                        event="run_resume",
                        run_id=run_id,
                    )
                    poll_resp = await client.get(
                        f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}",
                        headers=headers,
                        timeout=10.0,
                    )
                    if poll_resp.status_code == 200:
                        poll_data = poll_resp.json()
                        poll_status = str(poll_data.get("status", "")).lower()
                        if poll_status in ("completed", "failed", "cancelled"):
                            event_name = (
                                "run.completed"
                                if poll_status == "completed"
                                else f"run.{poll_status}"
                            )
                            await consume_events([(event_name, poll_data)])
                    elif poll_resp.status_code == 404:
                        await consume_events([(
                            "run.failed",
                            {"error": "Hermes no longer has the recorded run"},
                        )])
                    else:
                        raise RuntimeError(
                            f"Hermes reconciliation returned HTTP "
                            f"{poll_resp.status_code}"
                        )

                if run_id and not saw_terminal:
                    try:
                        async with client.stream(
                            "GET",
                            f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}/events",
                            headers=headers,
                        ) as stream:
                            stream.raise_for_status()
                            line_buffer: list[str] = []
                            async for raw_line in stream.aiter_lines():
                                line_buffer.append(raw_line)
                                if raw_line.strip() == "":
                                    await consume_events(
                                        parse_sse_line_buffer(line_buffer)
                                    )
                                    line_buffer.clear()
                                    if saw_terminal:
                                        break
                            if line_buffer:
                                await consume_events(parse_sse_line_buffer(line_buffer))
                    except httpx.ReadTimeout:
                        raise
                    except Exception as exc:
                        logger.warning(
                            f"[{request_id}] SSE stream ended with an error: {exc}"
                        )

                    if not saw_terminal:
                        logger.warning(
                            f"[{request_id}] SSE stream ended before a terminal event"
                        )
                        await poll_until_terminal()

                if run_id and final_usage is None and status == TaskStatus.completed:
                    poll_resp = await client.get(
                        f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}",
                        headers=headers,
                        timeout=10.0,
                    )
                    if poll_resp.status_code == 200:
                        poll_data = poll_resp.json()
                        final_usage = _normalize_usage(
                            poll_data.get("usage"), model
                        )
                        if not final_output:
                            final_output = str(poll_data.get("output", ""))

        except asyncio.CancelledError:
            if run_id:
                await _stop_remote_run(client, run_id, headers, request_id)
            if not saw_terminal:
                cancelled_trace = translate(
                    "run.cancelled",
                    {"error": "Task cancelled by the daemon"},
                    task_id,
                    turn_id,
                    seq=trace_seq,
                    role=role,
                    node=NODE_ID,
                )
                cancelled_trace["data"]["status"] = "cancelled"
                await emitter.emit(cancelled_trace)
            await emitter.flush_all()
            logger.warning(f"[{request_id}] Task cancelled by the daemon")
            raise
        except (TimeoutError, asyncio.TimeoutError):
            if saw_terminal:
                logger.warning(
                    f"[{request_id}] Completion reached the {timeout}s deadline"
                )
            else:
                logger.warning(f"[{request_id}] Task timed out after {timeout}s")
                status = TaskStatus.timeout
                final_output = f"Task timed out after {timeout}s"
                if run_id:
                    await _stop_remote_run(client, run_id, headers, request_id)
                timeout_trace = translate(
                    "run.failed",
                    {"error": final_output},
                    task_id,
                    turn_id,
                    seq=trace_seq,
                    role=role,
                    node=NODE_ID,
                )
                timeout_trace["data"]["status"] = "timeout"
                await emitter.emit(timeout_trace)
        except httpx.ReadTimeout:
            logger.warning(f"[{request_id}] SSE stream timed out after {SSE_READ_TIMEOUT}s")
            status = TaskStatus.timeout
            final_output = f"SSE stream timed out after {SSE_READ_TIMEOUT}s"
            if run_id:
                await _stop_remote_run(client, run_id, headers, request_id)
            timeout_trace = translate(
                "run.failed",
                {"error": final_output},
                task_id,
                turn_id,
                seq=trace_seq,
                role=role,
                node=NODE_ID,
            )
            timeout_trace["data"]["status"] = "timeout"
            await emitter.emit(timeout_trace)
        except Exception as exc:
            logger.error(f"[{request_id}] Runs API error: {exc}")
            if resume_run_id and not saw_terminal:
                raise HTTPException(
                    503,
                    "Hermes run reconciliation failed",
                    headers={"Retry-After": "5"},
                ) from exc
            status = TaskStatus.failed
            final_output = f"Runs API error: {exc}"

        if run_id and not saw_terminal and status == TaskStatus.failed:
            await _stop_remote_run(client, run_id, headers, request_id)

        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await emitter.flush_all(
            delivery_timeout=min(TRACE_DRAIN_TIMEOUT_SECONDS, remaining)
        )

        logger.info(
            f"[{request_id}] Run {run_id} {status.value} | "
            f"traces={emitter.trace_count} output_len={len(final_output)}"
        )
        log.log(
            f"Run {status.value} | {len(final_output)} chars, "
            f"{emitter.trace_count} traces",
            level="error" if status in (TaskStatus.failed, TaskStatus.timeout) else "info",
            event="run_completed",
            run_id=run_id,
            status=status.value,
            output=final_output,
            output_chars=len(final_output),
            usage=final_usage,
            trace_count=emitter.trace_count,
        )
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining > 0:
            try:
                await asyncio.wait_for(log.flush(), timeout=remaining)
            except TimeoutError:
                logger.warning(f"[{request_id}] Final log delivery timed out")

        return status, final_output, final_usage, emitter.trace_count, run_id


async def _stop_remote_run(
    client: httpx.AsyncClient,
    run_id: str,
    headers: dict,
    request_id: str,
) -> bool:
    """Ask Hermes to stop a remote run. Return false when it cannot stop it."""
    try:
        response = await client.post(
            f"{HERMES_GATEWAY_URL}/v1/runs/{run_id}/stop",
            headers=headers,
            timeout=CANCELLATION_TIMEOUT_SECONDS,
        )
        if 200 <= response.status_code < 300 or response.status_code == 409:
            logger.info(f"[{request_id}] Stop requested for run {run_id}")
            return True
        if response.status_code in (404, 405, 501):
            logger.warning(
                f"[{request_id}] Hermes does not support stopping run {run_id} "
                f"(HTTP {response.status_code})"
            )
            return False
        logger.warning(
            f"[{request_id}] Hermes rejected stop for run {run_id}: "
            f"HTTP {response.status_code}"
        )
    except Exception as exc:
        logger.warning(f"[{request_id}] Failed to stop run {run_id}: {exc}")
    return False


# ── Core Execution: hermes -z Fallback (doc 06 §8) ────────────────────────

async def _emit_cli_traces(
    task_id: str,
    turn_id: str,
    role: str,
    description: str,
    status: TaskStatus,
    output: str,
    round_no: int,
) -> int:
    """Queue coarse CLI traces through the same durable trace path."""
    if not (DAEMON_INGEST_URL and BMAS_NODE_KEY and task_id and turn_id):
        return 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        emitter = TraceEmitter(client, task_id, turn_id)
        traces = [{
            "trace_id": f"trace-{turn_id}",
            "task_id": task_id,
            "turn_id": turn_id,
            "seq": 0,
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "node": NODE_ID,
            "type": "turn_start",
            "data": {
                "objective": description[:200],
                "phase": "execute",
                "round": round_no,
            },
            "tokens": {"in": 0, "out": 0},
            "cost_usd": 0.0,
        }]
        if status == TaskStatus.completed:
            traces.extend([
                {
                    "trace_id": f"trace-{turn_id}",
                    "task_id": task_id,
                    "turn_id": turn_id,
                    "seq": 1,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "role": role,
                    "node": NODE_ID,
                    "type": "reasoning",
                    "data": {"text": output},
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
                    "data": {"summary": output, "usage": None},
                    "tokens": {"in": 0, "out": 0},
                    "cost_usd": 0.0,
                },
            ])
        else:
            traces.append({
                "trace_id": f"trace-{turn_id}",
                "task_id": task_id,
                "turn_id": turn_id,
                "seq": 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "node": NODE_ID,
                "type": "error",
                "data": {"message": output, "status": status.value},
                "tokens": {"in": 0, "out": 0},
                "cost_usd": 0.0,
            })
        for trace in traces:
            await emitter.emit(trace)
        await emitter.flush_all(delivery_timeout=0)
        return emitter.trace_count


async def _run_hermes_inner(
    description: str,
    role_prompt: Optional[str],
    context: Optional[dict],
    timeout: int,
    request_id: str,
    task_id: str = "",
    turn_id: str = "",
    role: str = "agent",
    profile: Optional[str] = None,
    model: str = LITELLM_MODEL,
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
            "--model", model,
        ]

        logger.info(
            f"[{request_id}] Executing hermes -z (fallback) | "
            f"model={model} profile={profile or 'default'} "
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

        await _post_logs_oneshot(task_id, role, turn_id, request_id, [
            (f"Executing (fallback) | model={model} "
             f"profile={profile or 'default'} timeout={timeout}s",
             "info", {"event": "run_submit", "model": model,
                      "profile": profile or "default",
                      "objective": description[:500], "mode": "hermes-z"}),
        ])

        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            logger.warning(f"[{request_id}] Task cancelled by the daemon")
            raise

        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error(
                f"[{request_id}] hermes exited with code {proc.returncode} | "
                f"stderr={errors[:500]}"
            )
            await _post_logs_oneshot(task_id, role, turn_id, request_id, [
                (f"Run failed | exit code {proc.returncode}", "error",
                 {"event": "run_error", "exit_code": proc.returncode, "stderr": errors}),
            ])
            error_output = errors or f"Exit code {proc.returncode}"
            trace_count = await _emit_cli_traces(
                task_id,
                turn_id,
                role,
                description,
                TaskStatus.failed,
                error_output,
                int((context or {}).get("round", 1) or 1),
            )
            return TaskStatus.failed, error_output, None, trace_count, None

        # Emit synthetic traces (doc 06 §8: coarse trace rather than nothing)
        trace_count = await _emit_cli_traces(
            task_id,
            turn_id,
            role,
            description,
            TaskStatus.completed,
            output,
            int((context or {}).get("round", 1) or 1),
        )

        logger.info(f"[{request_id}] Task completed (fallback) | output_len={len(output)}")
        await _post_logs_oneshot(task_id, role, turn_id, request_id, [
            (f"Run completed | {len(output)} chars", "info",
             {"event": "run_completed", "status": "completed",
              "output": output, "output_chars": len(output)}),
        ])

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
    model: str = LITELLM_MODEL,
) -> tuple[TaskStatus, str, Optional[dict], int, Optional[str]]:
    """Run the complete CLI operation under one request deadline."""
    try:
        async with asyncio.timeout(timeout):
            return await _run_hermes_inner(
                description,
                role_prompt,
                context,
                timeout,
                request_id,
                task_id,
                turn_id,
                role,
                profile,
                model,
            )
    except TimeoutError:
        output = f"Task timed out after {timeout}s"
        trace_count = await _emit_cli_traces(
            task_id,
            turn_id,
            role,
            description,
            TaskStatus.timeout,
            output,
            int((context or {}).get("round", 1) or 1),
        )
        return TaskStatus.timeout, output, None, trace_count, None
    except asyncio.CancelledError:
        await asyncio.shield(_emit_cli_traces(
            task_id,
            turn_id,
            role,
            description,
            TaskStatus.failed,
            "Task cancelled by the daemon",
            int((context or {}).get("round", 1) or 1),
        ))
        raise


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

_activation_lock = asyncio.Lock()
_activation_inflight: dict[str, asyncio.Task[TaskResponse]] = {}
_activation_results: dict[str, tuple[float, TaskResponse]] = {}
_activation_result_fingerprints: dict[str, str] = {}


def _authorize_execute(request: Request) -> None:
    """Require execute authentication only when an execute key is configured."""
    if not BMAS_EXECUTE_KEY:
        return

    authorization = request.headers.get("Authorization", "")
    bearer = ""
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    header_key = request.headers.get("X-BMAS-Execute-Key", "")
    if any(
        candidate and hmac.compare_digest(candidate, BMAS_EXECUTE_KEY)
        for candidate in (bearer, header_key)
    ):
        return
    raise HTTPException(
        status_code=401,
        detail="Missing or invalid execute credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _activation_key(req: TaskRequest) -> Optional[str]:
    """Return a stable retry key when the daemon supplied one."""
    stable_id = req.activation_id or req.turn_id
    if not stable_id:
        return None
    return f"{req.task_id}:{stable_id}"


def _request_fingerprint(req: TaskRequest) -> str:
    """Hash the execution fields that one activation ID must identify."""
    payload = req.model_dump(mode="json", exclude_none=True)
    payload.pop("timeout", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _prune_activation_results(now: float) -> None:
    """Remove expired or excess activation responses from memory."""
    expired = [
        key
        for key, (saved_at, _) in _activation_results.items()
        if now - saved_at >= ACTIVATION_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _activation_results.pop(key, None)
        _activation_result_fingerprints.pop(key, None)

    overflow = len(_activation_results) - max(0, ACTIVATION_CACHE_MAX_ENTRIES)
    if overflow > 0:
        oldest = sorted(
            _activation_results,
            key=lambda key: _activation_results[key][0],
        )[:overflow]
        for key in oldest:
            _activation_results.pop(key, None)
            _activation_result_fingerprints.pop(key, None)

    total_bytes = sum(
        _json_size(response.model_dump(mode="json"))
        for _, response in _activation_results.values()
    )
    for key in sorted(
        _activation_results,
        key=lambda item: _activation_results[item][0],
    ):
        if total_bytes <= max(0, ACTIVATION_CACHE_MAX_BYTES):
            break
        _, response = _activation_results.pop(key)
        total_bytes -= _json_size(response.model_dump(mode="json"))
        _activation_result_fingerprints.pop(key, None)


def _activation_cache_path(key: str) -> Path:
    """Map an activation key to a fixed safe file name."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return ACTIVATION_CACHE_DIR / f"{digest}.json"


@contextmanager
def _activation_cache_lock():
    """Serialize activation state changes across agent processes."""
    ACTIVATION_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = ACTIVATION_CACHE_DIR / ".activation-cache.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _activation_record_is_evictable(path: Path, now: float) -> bool:
    """Return true when one cache file reached a bounded terminal policy."""
    try:
        data = json.loads(path.read_text())
        age = now - path.stat().st_mtime
        if "state" not in data:
            return age >= ACTIVATION_CACHE_TTL_SECONDS
        state = str(data.get("state", "uncertain"))
        if state not in ("running", "uncertain"):
            return age >= ACTIVATION_CACHE_TTL_SECONDS
        if state == "uncertain":
            return age >= ACTIVATION_UNCERTAIN_TTL_SECONDS
        return age >= (
            ACTIVATION_RUNNING_TTL_SECONDS + ACTIVATION_UNCERTAIN_TTL_SECONDS
        )
    except Exception:
        return False


def _reserve_activation_cache(path: Path, incoming_bytes: int) -> bool:
    """Evict old terminal results until one activation record fits."""
    ACTIVATION_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = [item for item in ACTIVATION_CACHE_DIR.glob("*.json") if item.is_file()]
    existing_size = path.stat().st_size if path.exists() else 0
    total_bytes = sum(item.stat().st_size for item in files)
    total_files = len(files) + (0 if path.exists() else 1)
    projected_bytes = total_bytes - existing_size + incoming_bytes
    now = time.time()
    candidates = sorted(
        (
            item
            for item in files
            if item != path and _activation_record_is_evictable(item, now)
        ),
        key=lambda item: item.stat().st_mtime,
    )
    while candidates and (
        total_files > max(0, ACTIVATION_CACHE_MAX_ENTRIES)
        or projected_bytes > max(0, ACTIVATION_CACHE_MAX_BYTES)
    ):
        victim = candidates.pop(0)
        try:
            size = victim.stat().st_size
            victim.unlink()
            total_files -= 1
            projected_bytes -= size
        except FileNotFoundError:
            pass
    return (
        total_files <= max(0, ACTIVATION_CACHE_MAX_ENTRIES)
        and projected_bytes <= max(0, ACTIVATION_CACHE_MAX_BYTES)
    )


def _load_activation_record_unlocked(key: str, now: float) -> dict | None:
    """Load one durable activation state while the caller owns the file lock."""
    path = _activation_cache_path(key)
    try:
        age = now - path.stat().st_mtime
        data = json.loads(path.read_text())
        if "state" not in data:
            response = TaskResponse.model_validate(data)
            if age >= ACTIVATION_CACHE_TTL_SECONDS:
                path.unlink(missing_ok=True)
                return None
            return {
                "version": 1,
                "key": key,
                "state": response.status.value,
                "fingerprint": "",
                "task_id": response.task_id,
                "turn_id": response.turn_id,
                "run_id": response.run_id,
                "response": response.model_dump(mode="json"),
                "updated_at": path.stat().st_mtime,
            }
        state = str(data.get("state", "uncertain"))
        if state == "running" and age >= ACTIVATION_RUNNING_TTL_SECONDS:
            data["state"] = "uncertain"
            data["error"] = "The prior agent process stopped during this activation"
            data["updated_at"] = now
            _atomic_write_json(path, data)
        elif (
            state == "uncertain"
            and not data.get("run_id")
            and age >= ACTIVATION_UNCERTAIN_TTL_SECONDS
        ):
            data["state"] = "quarantined"
            data["error"] = (
                "The activation stayed uncertain beyond its quarantine limit"
            )
            data["updated_at"] = now
            _atomic_write_json(path, data)
        elif state not in ("running", "uncertain") and age >= ACTIVATION_CACHE_TTL_SECONDS:
            path.unlink(missing_ok=True)
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"Activation cache read failed for {path.name}: {exc}")
        uncertain = {
            "version": 1,
            "key": key,
            "state": "uncertain",
            "fingerprint": "",
            "task_id": key.rsplit(":", 1)[0],
            "turn_id": key.rsplit(":", 1)[-1],
            "run_id": None,
            "response": None,
            "error": "The durable activation record is corrupt",
            "updated_at": now,
        }
        try:
            _atomic_write_json(path, uncertain)
        except Exception as write_exc:
            logger.error(f"Activation quarantine write failed: {write_exc}")
        return uncertain


def _load_activation_record(key: str, now: float) -> dict | None:
    """Load one durable activation state after an agent restart."""
    with _activation_cache_lock():
        return _load_activation_record_unlocked(key, now)


def _load_activation_result(key: str, now: float) -> TaskResponse | None:
    """Load one terminal activation response for backward compatibility."""
    record = _load_activation_record(key, now)
    if not record or not isinstance(record.get("response"), dict):
        return None
    return TaskResponse.model_validate(record["response"])


def _claim_activation(
    key: str,
    fingerprint: str,
    task_id: str,
    turn_id: str,
) -> bool:
    """Create one cross-process activation claim without replacing a peer."""
    path = _activation_cache_path(key)
    record = {
        "version": 1,
        "key": key,
        "state": "running",
        "fingerprint": fingerprint,
        "task_id": task_id,
        "turn_id": turn_id,
        "run_id": None,
        "response": None,
        "updated_at": time.time(),
    }
    with _activation_cache_lock():
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        if not _reserve_activation_cache(path, _json_size(record)):
            raise HTTPException(
                503,
                "Activation cache capacity is exhausted",
                headers={"Retry-After": "30"},
            )
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    return True


def _persist_activation_state(
    key: str,
    state: str,
    fingerprint: str,
    *,
    response: Optional[TaskResponse] = None,
    run_id: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """Write one allowed activation transition under a cross-process lock."""
    path = _activation_cache_path(key)
    with _activation_cache_lock():
        current = _load_activation_record_unlocked(key, time.time())
        if current is None:
            current = {
                "version": 1,
                "key": key,
                "task_id": response.task_id if response else key.rsplit(":", 1)[0],
                "turn_id": response.turn_id if response else key.rsplit(":", 1)[-1],
            }
        else:
            saved_fingerprint = str(current.get("fingerprint", ""))
            if fingerprint and saved_fingerprint and fingerprint != saved_fingerprint:
                raise RuntimeError("Activation fingerprint changed during execution")
            current_state = str(current.get("state", "uncertain"))
            current_is_terminal = current_state not in ("running", "uncertain")
            same_terminal = current_is_terminal and current_state == state
            quarantine_cancel = current_state == "quarantined" and state == "cancelled"
            if current_state == "cancelled" and state != "cancelled":
                logger.warning(
                    f"Ignored activation transition cancelled -> {state} for {key}"
                )
                return False
            if current_is_terminal and not (same_terminal or quarantine_cancel):
                logger.warning(
                    f"Ignored activation transition {current_state} -> {state} for {key}"
                )
                return False
            if current_state == "uncertain" and state == "running":
                logger.warning(
                    f"Ignored activation transition uncertain -> running for {key}"
                )
                return False

        current.update({
            "state": state,
            "fingerprint": fingerprint or str(current.get("fingerprint", "")),
            "updated_at": time.time(),
        })
        if run_id is not None:
            current["run_id"] = run_id
        if response is not None:
            current["response"] = response.model_dump(mode="json")
            current["run_id"] = response.run_id or current.get("run_id")
        if error:
            current["error"] = error
        if not _reserve_activation_cache(path, _json_size(current)):
            if response is None:
                raise RuntimeError("Activation cache capacity is exhausted")
            current["response"] = None
            current["error"] = (
                "The terminal response exceeded the activation cache capacity"
            )
            if not _reserve_activation_cache(path, _json_size(current)):
                raise RuntimeError("Activation cache capacity is exhausted")
        _atomic_write_json(path, current)
        return True


def _persist_activation_result(
    key: str,
    response: TaskResponse,
    fingerprint: str = "",
) -> bool:
    """Save one terminal activation result with an atomic file replace."""
    return _persist_activation_state(
        key,
        response.status.value,
        fingerprint,
        response=response,
    )


def _cancel_durable_activation_records(task_id: str) -> list[str]:
    """Mark orphaned running records cancelled and return Hermes run IDs."""
    run_ids: list[str] = []
    if not ACTIVATION_CACHE_DIR.is_dir():
        return run_ids
    for path in ACTIVATION_CACHE_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            if record.get("task_id") != task_id:
                continue
            if record.get("state") not in ("running", "uncertain", "quarantined"):
                continue
            key = str(record.get("key", ""))
            if not key:
                continue
            run_id = record.get("run_id")
            _persist_activation_state(
                key,
                "cancelled",
                str(record.get("fingerprint", "")),
                run_id=str(run_id) if run_id else None,
                error="The daemon cancelled this activation",
            )
            if run_id:
                run_ids.append(str(run_id))
        except Exception as exc:
            logger.warning(f"Activation cancellation scan failed: {exc}")
    return run_ids


def _activation_response_from_record(
    record: dict,
    task_id: str = "",
    turn_id: str = "",
) -> TaskResponse | None:
    """Build a stable response for one terminal activation record."""
    response_data = record.get("response")
    if isinstance(response_data, dict):
        return TaskResponse.model_validate(response_data)
    if record.get("state") != "cancelled":
        return None
    return TaskResponse(
        task_id=str(record.get("task_id", task_id)),
        status=TaskStatus.failed,
        result="The daemon cancelled this activation",
        node_id=NODE_ID,
        request_id="activation-cache",
        duration_ms=0,
        timestamp=datetime.now(timezone.utc).isoformat(),
        turn_id=record.get("turn_id") or turn_id,
        run_id=record.get("run_id"),
    )

async def _run_durable_activation(
    key: str,
    fingerprint: str,
    factory: Callable[[], Awaitable[TaskResponse]],
) -> TaskResponse:
    """Run one claimed activation and persist every terminal outcome."""
    try:
        response = await factory()
    except asyncio.CancelledError:
        try:
            await asyncio.to_thread(
                _persist_activation_state,
                key,
                "cancelled",
                fingerprint,
                error="The daemon cancelled this activation",
            )
        except Exception as exc:
            logger.warning(f"Activation cancellation write failed: {exc}")
        raise
    except BaseException as exc:
        try:
            await asyncio.to_thread(
                _persist_activation_state,
                key,
                "uncertain",
                fingerprint,
                error=str(exc),
            )
        except Exception as write_exc:
            logger.warning(f"Activation failure write failed: {write_exc}")
        raise

    try:
        persisted = await asyncio.to_thread(
            _persist_activation_result, key, response, fingerprint
        )
    except Exception as exc:
        logger.warning(f"Activation cache write failed: {exc}")
        persisted = False
    if not persisted:
        record = await asyncio.to_thread(_load_activation_record, key, time.time())
        durable = (
            _activation_response_from_record(record, response.task_id, response.turn_id or "")
            if record
            else None
        )
        if durable is None:
            raise HTTPException(
                409,
                "The activation state changed before the result became durable",
                headers={"Retry-After": "5"},
            )
        response = durable
    async with _activation_lock:
        _activation_results[key] = (time.time(), response)
        _activation_result_fingerprints[key] = fingerprint
        _prune_activation_results(time.time())
    return response


async def _remove_inflight_activation(
    key: str,
    task: asyncio.Task[TaskResponse],
) -> None:
    """Remove one finished task without touching a replacement task."""
    async with _activation_lock:
        if _activation_inflight.get(key) is task:
            _activation_inflight.pop(key, None)


def _schedule_activation_removal(
    key: str,
    task: asyncio.Task[TaskResponse],
) -> None:
    """Schedule in-memory cleanup on the task's event loop."""
    task.get_loop().create_task(_remove_inflight_activation(key, task))


async def _execute_idempotent(
    key: str,
    factory: Callable[[], Awaitable[TaskResponse]],
    *,
    resume_factory: Optional[Callable[[dict], Awaitable[TaskResponse]]] = None,
    fingerprint: str = "",
    task_id: str = "",
    turn_id: str = "",
) -> TaskResponse:
    """Coalesce local retries and reject uncertain cross-process retries."""
    now = time.time()
    async with _activation_lock:
        _prune_activation_results(now)
        cached = _activation_results.get(key)
        if cached:
            cached_fingerprint = _activation_result_fingerprints.get(key, "")
            if cached_fingerprint and fingerprint != cached_fingerprint:
                raise HTTPException(409, "Activation ID reused with different input")
            return cached[1]
        task = _activation_inflight.get(key)
        if task is None:
            record = await asyncio.to_thread(_load_activation_record, key, now)
            if record is not None:
                saved_fingerprint = str(record.get("fingerprint", ""))
                if saved_fingerprint and fingerprint != saved_fingerprint:
                    raise HTTPException(
                        409, "Activation ID reused with different input"
                    )
                durable = _activation_response_from_record(record, task_id, turn_id)
                if durable is not None:
                    _activation_results[key] = (now, durable)
                    _activation_result_fingerprints[key] = saved_fingerprint
                    return durable
                state = str(record.get("state", "uncertain"))
                if (
                    state in ("running", "uncertain")
                    and record.get("run_id")
                    and resume_factory is not None
                ):
                    task = asyncio.create_task(
                        _run_durable_activation(
                            key,
                            fingerprint,
                            lambda: resume_factory(record),
                        )
                    )
                    _activation_inflight[key] = task
                    task.add_done_callback(
                        lambda done_task: _schedule_activation_removal(key, done_task)
                    )
                else:
                    raise HTTPException(
                        409,
                        f"Activation state is {state}. Refusing an uncertain duplicate.",
                        headers={"Retry-After": "5"},
                    )

            if task is None:
                claimed = await asyncio.to_thread(
                    _claim_activation,
                    key,
                    fingerprint,
                    task_id or key.rsplit(":", 1)[0],
                    turn_id or key.rsplit(":", 1)[-1],
                )
                if not claimed:
                    raise HTTPException(
                        409,
                        "Another process claimed this activation",
                        headers={"Retry-After": "5"},
                    )
                task = asyncio.create_task(
                    _run_durable_activation(key, fingerprint, factory)
                )
                _activation_inflight[key] = task
                task.add_done_callback(
                    lambda done_task: _schedule_activation_removal(key, done_task)
                )

    return await asyncio.shield(task)


async def _execute_task_once(
    req: TaskRequest,
    request_id: str,
    turn_id: str,
    activation_key: Optional[str] = None,
    activation_fingerprint: str = "",
    resume_run_id: Optional[str] = None,
) -> TaskResponse:
    """Execute one activation and build its stable response."""
    timeout = req.timeout or TASK_TIMEOUT_SECONDS
    role = req.role or "agent"
    model = req.model or LITELLM_MODEL
    profile = req.profile
    context_session_id = (req.context or {}).get("session_id")
    actor_session_id = req.session_id or context_session_id or f"{req.task_id}:{role}"
    start = time.monotonic()

    logger.info(
        f"[{request_id}] Received task={req.task_id} | "
        f"role={role} profile={profile or 'default'} turn={turn_id} model={model} "
        f"backend={_selected_execution_backend()} "
        f"context={'yes' if req.context else 'no'}"
    )

    backend = _selected_execution_backend()
    if backend == "litellm":
        if resume_run_id:
            raise HTTPException(
                409,
                "The LiteLLM execution path cannot reconcile a Hermes run",
            )
        status, result, usage, trace_count, run_id = await _run_via_litellm(
            description=req.description,
            role_prompt=req.role_prompt,
            context=req.context,
            task_id=req.task_id,
            turn_id=turn_id,
            role=role,
            model=model,
            request_id=request_id,
            timeout=timeout,
        )
    elif backend == "hermes-runs-api":
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
            session_id=str(actor_session_id),
            timeout=timeout,
            activation_key=activation_key,
            activation_fingerprint=activation_fingerprint,
            resume_run_id=resume_run_id,
        )
    elif backend == "hermes-cli":
        if resume_run_id:
            raise HTTPException(
                409,
                "The CLI execution path cannot reconcile a recorded Hermes run",
            )
        status, result, usage, trace_count, run_id = await _run_hermes(
            description=req.description,
            role_prompt=req.role_prompt,
            context=req.context,
            timeout=timeout,
            request_id=request_id,
            model=model,
            task_id=req.task_id,
            turn_id=turn_id,
            role=role,
            profile=profile,
        )
    else:
        raise HTTPException(503, "No execution backend is available")

    duration_ms = int((time.monotonic() - start) * 1000)
    envelope_action, envelope_entries = _result_envelope(result)
    return TaskResponse(
        task_id=req.task_id,
        status=status,
        result=result,
        node_id=NODE_ID,
        request_id=request_id,
        duration_ms=duration_ms,
        timestamp=datetime.now(timezone.utc).isoformat(),
        turn_id=turn_id,
        run_id=run_id,
        action=(
            envelope_action or "contribute"
            if status == TaskStatus.completed
            else None
        ),
        entries=envelope_entries,
        usage=usage,
        trace_count=trace_count,
        artifacts=None,
        envelope_fallback=None,
        response_id=run_id,
    )


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

    backend = _selected_execution_backend()
    execution_ok = backend == "litellm" or hermes_ok or runs_api_ok
    status = "healthy" if execution_ok and litellm_ok else "degraded"
    return HealthResponse(
        status=status,
        node_id=NODE_ID,
        hermes_available=hermes_ok,
        litellm_reachable=litellm_ok,
        litellm_url=LITELLM_URL,
        model=LITELLM_MODEL,
        runs_api_available=runs_api_ok,
        execution_backend=backend,
    )


@app.post("/execute", response_model=TaskResponse)
async def execute_task(req: TaskRequest, request: Request):
    """
    Execute a task with optional persona injection.

    The starter path calls LiteLLM directly without tools.
    Production nodes use the Hermes Runs API or Hermes CLI.
    """
    _authorize_execute(request)
    request_id = getattr(request.state, "request_id", str(uuid.uuid4())[:8])
    turn_id = req.turn_id or f"turn-{str(uuid.uuid4())[:8]}"
    key = _activation_key(req)
    fingerprint = _request_fingerprint(req)

    async def factory() -> TaskResponse:
        return await _execute_task_once(
            req,
            request_id,
            turn_id,
            activation_key=key,
            activation_fingerprint=fingerprint,
        )

    async def resume_factory(record: dict) -> TaskResponse:
        return await _execute_task_once(
            req,
            request_id,
            turn_id,
            activation_key=key,
            activation_fingerprint=fingerprint,
            resume_run_id=str(record["run_id"]),
        )

    if key:
        return await _execute_idempotent(
            key,
            factory,
            resume_factory=resume_factory,
            fingerprint=fingerprint,
            task_id=req.task_id,
            turn_id=turn_id,
        )
    return await factory()


@app.post("/tasks/{task_id}/cancel")
async def cancel_task_activations(task_id: str, request: Request):
    """Cancel every local activation that belongs to one daemon task."""
    _authorize_execute(request)
    prefix = f"{task_id}:"
    async with _activation_lock:
        matches = [
            (key, task)
            for key, task in _activation_inflight.items()
            if key.startswith(prefix) and not task.done()
        ]
        for _, task in matches:
            task.cancel()
    if matches:
        await asyncio.gather(
            *(task for _, task in matches),
            return_exceptions=True,
        )

    orphan_run_ids = await asyncio.to_thread(
        _cancel_durable_activation_records, task_id
    )
    if orphan_run_ids and HERMES_GATEWAY_URL:
        headers = (
            {"Authorization": f"Bearer {HERMES_GATEWAY_KEY}"}
            if HERMES_GATEWAY_KEY
            else {}
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            await asyncio.gather(*(
                _stop_remote_run(client, run_id, headers, "task-cancel")
                for run_id in orphan_run_ids
            ))
    return {
        "task_id": task_id,
        "cancelled": len(matches) + len(orphan_run_ids),
    }
