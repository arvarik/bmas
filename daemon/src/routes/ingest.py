# /opt/bmas/daemon/src/routes/ingest.py
"""
Trace ingest endpoint — receives agent trace events from edge nodes.

Authenticates via BMAS_NODE_KEY bearer token. Writes traces to:
1. Redis Stream (live SSE → UI)
2. SQLite agent_traces (durable archive)

On `final` events, computes cost_usd from MODEL_PRICING and inserts
cost entries with per-task/per-model/per-node breakdown.

See doc 06 §5 for the transport architecture.
"""

import contextlib
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import require_node_key
from config import BMAS_NODE_KEY

router = APIRouter()
logger = logging.getLogger("bmas.ingest")


def _canon_level(level: str | None) -> str:
    """Canonicalize a log level for archival."""
    from core.log_levels import normalize_level
    return normalize_level(level)


def _verify_bearer(request: Request) -> None:
    """Validate the BMAS_NODE_KEY bearer token.

    Delegates to the shared auth module (auth.require_node_key).
    Raises HTTPException(401) if the token is missing or invalid.
    """
    try:
        require_node_key(request, BMAS_NODE_KEY)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


def _compute_cost(
    usage: dict | None,
    model: str | None,
) -> tuple[float, str]:
    """Compute cost_usd from token counts × MODEL_PRICING.

    Returns (cost_usd, price_source).
    The daemon is the sole authority on dollar cost (doc 06 §3.1).
    """
    from config import MODEL_PRICING

    if not usage or not model:
        return 0.0, "none"

    pricing = MODEL_PRICING.get(model, {})
    if not pricing:
        logger.warning(
            f"No pricing configured for model '{model}' — cost_usd will be 0.0"
        )
        return 0.0, "missing"

    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))

    cost = (
        input_tokens * float(pricing.get("input_cost_per_token", 0))
        + output_tokens * float(pricing.get("output_cost_per_token", 0))
    )

    source = str(pricing.get("source", "bmas.yaml"))
    return round(cost, 8), source


@router.post("/ingest/logs/{task_id}")
async def ingest_logs(task_id: str, request: Request):
    """Receive structured log records emitted by a distributed agent node.

    Bearer auth via BMAS_NODE_KEY. Accepts either a single JSON log object or
    a JSON array of them. Each record flows into the same collector pipeline
    as daemon logs (Redis streams + Pub/Sub for live SSE + SQLite archive),
    attributed to the emitting agent/persona — making the Logs tab a true
    distributed agent-swarm collector rather than a daemon-only view.

    Record shape (all optional except message):
        {
          "agent_role": "expert.valuation",  # opaque actor/persona id
          "level": "info",                    # info|warning|error|debug
          "message": "…",                     # full text, never truncated
          "node": "agent-node1",              # originating node id
          "turn_id": "turn-abc",              # correlation id
          "ts": "<iso8601>",                  # emitted timestamp
          "fields": { … }                     # arbitrary structured payload
        }
    """
    _verify_bearer(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None

    records = body if isinstance(body, list) else [body]
    if not records:
        return JSONResponse({"status": "ok", "ingested": 0})

    import database as db
    from app import app

    orch = app.state.orchestrator
    ingested = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        agent_role = str(rec.get("agent_role") or rec.get("actor") or rec.get("role") or "agent")
        message = str(rec.get("message") or rec.get("msg") or "")
        if not message:
            continue
        level = rec.get("level", "info")
        node = rec.get("node") or rec.get("node_id")
        turn_id = rec.get("turn_id")
        fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else None

        # Live stream + Pub/Sub (best-effort)
        with contextlib.suppress(Exception):
            await orch.bb.publish_log(
                agent_role, message, task_id=task_id,
                level=level, fields=fields, node=node, turn_id=turn_id,
            )
        # Durable archive (best-effort)
        try:
            await db.insert_log_entry(
                task_id, agent_role, _canon_level(level), message,
                fields=fields, node=node, turn_id=turn_id,
            )
        except Exception as e:
            logger.warning(f"Log archive failed for {task_id}/{agent_role}: {e}")
        ingested += 1

    return JSONResponse({"status": "ok", "ingested": ingested})


@router.post("/ingest/traces/{task_id}/{turn_id}")
async def ingest_traces(task_id: str, turn_id: str, request: Request):
    """Receive a batch of bMAS trace events from an agent node.

    Bearer auth via BMAS_NODE_KEY. Accepts a JSON array of trace events
    matching the schema in doc 06 §4.

    For each trace:
    - Writes to Redis Stream bmas:traces:{task_id}:{turn_id} (capped, TTL 24h)
    - Publishes to bmas:events:{task_id} as event:trace for live SSE

    On `final` events:
    - Batch-inserts all traces to SQLite agent_traces
    - Computes cost_usd from usage × MODEL_PRICING
    - Inserts cost_entries with per-task/model/node breakdown
    - Updates task cost totals
    """
    _verify_bearer(request)

    try:
        traces = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None

    if not isinstance(traces, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of trace events")

    if not traces:
        return JSONResponse({"status": "ok", "ingested": 0})

    import database as db
    from app import app

    orch = app.state.orchestrator

    # Track whether we see a final event for cost processing
    final_trace = None
    db_rows = []

    for trace in traces:
        # Validate required fields
        if not isinstance(trace, dict):
            continue
        trace_type = trace.get("type", "unknown")

        # 1. Write to Redis Stream (live transport; capped)
        try:
            stream_key = f"bmas:traces:{task_id}:{turn_id}"
            await orch.bb.redis.xadd(
                stream_key,
                {
                    "type": trace_type,
                    "data": json.dumps(trace.get("data", {})),
                    "seq": str(trace.get("seq", 0)),
                    "role": trace.get("role", ""),
                    "node": trace.get("node", ""),
                    "ts": trace.get("ts", datetime.now(UTC).isoformat()),
                },
                maxlen=5000,
                approximate=True,
            )
            # Set TTL (24h) — idempotent
            await orch.bb.redis.expire(stream_key, 86400)
        except Exception as e:
            logger.warning(f"Redis trace write failed for {task_id}/{turn_id}: {e}")

        # 2. Publish to Pub/Sub for live SSE (event: trace)
        try:
            await orch.bb.publish_event(task_id, "trace", trace)
        except Exception as e:
            logger.warning(f"Trace Pub/Sub failed for {task_id}: {e}")

        # 3. Collect for SQLite batch insert
        tokens = trace.get("tokens", {})
        db_rows.append({
            "task_id": task_id,
            "turn_id": turn_id,
            "seq": trace.get("seq", 0),
            "role": trace.get("role", "agent"),
            "node": trace.get("node"),
            "type": trace_type,
            "data": trace.get("data"),
            "model": None,  # Set from usage on final
            "tokens_in": tokens.get("in", 0),
            "tokens_out": tokens.get("out", 0),
            "cost_usd": 0.0,  # Computed on final
        })

        # 4. Check for final event
        if trace_type == "final":
            final_trace = trace

    # 5. Batch-insert to SQLite
    try:
        await db.insert_agent_traces(db_rows)
    except Exception as e:
        logger.warning(f"SQLite trace insert failed for {task_id}/{turn_id}: {e}")

    # 6. On final: compute and record cost
    if final_trace:
        usage = (final_trace.get("data") or {}).get("usage")
        node_id = final_trace.get("node")

        # Determine model from usage or trace context
        model = None
        if usage and isinstance(usage, dict):
            model = usage.get("model")

        cost_usd, price_source = _compute_cost(usage, model)

        if usage and model:
            try:
                input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
                output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))

                await db.insert_cost_entry_v2(
                    task_id=task_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    phase="trace",
                    node_id=node_id,
                    turn_id=turn_id,
                    provider=None,  # Could be inferred from model config
                    price_source=price_source,
                    joules_estimate=0.0,  # Beszel integration stub
                )
                await db.update_task_cost_totals(task_id)
                logger.info(
                    f"Cost recorded: {task_id}/{turn_id} model={model} "
                    f"in={input_tokens} out={output_tokens} cost=${cost_usd:.6f} "
                    f"source={price_source}"
                )
            except Exception as e:
                logger.warning(f"Cost entry insert failed for {task_id}/{turn_id}: {e}")

            # Publish cost event for live SSE
            with contextlib.suppress(Exception):
                await orch.bb.publish_event(task_id, "cost", {
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost_usd,
                    "node_id": node_id,
                    "turn_id": turn_id,
                    "price_source": price_source,
                })
        elif usage is None:
            # Legacy hermes -z fallback — usage unknown (doc 06 §3.1 note)
            logger.warning(
                f"No usage in final trace for {task_id}/{turn_id} — "
                f"likely hermes -z fallback. Cost entry skipped."
            )

    return JSONResponse({
        "status": "ok",
        "ingested": len(db_rows),
        "has_final": final_trace is not None,
    })
