[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Control Unit](05-control-unit.md) | [➡️ Next: Data Model](07-data-model.md)

# 06 — Agent Traces (The Observability Prerequisite)

> [!IMPORTANT]
> **This document must be implemented before the UI documents.** Today the system collects almost no agent-level data ([Gap G5](01-gap-analysis.md#7-the-silent-observability-failure-root-cause-for-the-ui-work)). You cannot visualize traces that aren't being captured. This is the first build phase in the [migration plan](10-migration-and-rollout.md).

---

## 1. The root cause, precisely

The edge agent runs Hermes as a one-shot subprocess and returns only final stdout:

```161:206:agent/api_server.py
        # Build the hermes command
        cmd = [
            HERMES_BIN, "-z", description,
            "--model", LITELLM_MODEL,
        ]
        ...
        output = stdout.decode("utf-8", errors="replace").strip()
        ...
        return TaskStatus.completed, output
```

Consequences:
- **No reasoning steps, no tool calls, no token deltas** ever leave the node.
- The daemon's `TaskResponse` has **no `usage` field** (`api_server.py` lines 76–84), so the cost-capture branch in `orchestrator._dispatch_agent` (lines 505–518) is dead code — the Cost tab shows `$0.00`.
- The Logs tab renders only daemon breadcrumbs.

## 2. The enabler: the Hermes Runs API

Our own [HERMES_API.md](../HERMES_API.md#appendix-a-gateway-api-server-port-8642) documents exactly the capability we need — the Gateway **Runs API** with an SSE event stream:

| Method | Path | Use |
|:--|:--|:--|
| `POST` | `/v1/runs` | submit a task, get `run_id` |
| `GET` | `/v1/runs/{run_id}/events` | **SSE: tool progress, token deltas, lifecycle** |
| `GET` | `/v1/runs/{run_id}` | poll final state + `usage` |
| `POST` | `/v1/runs/{run_id}/stop` | interrupt (powers HITL abort) |

The doc even names this as the integration point: *"The Runs API is the most promising integration point for daemon-to-agent communication."* This replaces the `hermes -z` subprocess.

> [!WARNING] Verify before building
> Confirm on a live node: `API_SERVER_ENABLED=true`, the exact event schema emitted by `/v1/runs/{id}/events`, and that `usage` is populated on completion. The shapes below are the *target contract*; adjust field names to the runtime's actual output. Tracked in [10 §Open Questions](10-migration-and-rollout.md#open-questions-verify-before-building).

## 3. Rearchitected agent server

`agent/api_server.py` changes from "run subprocess, return string" to "submit run, **stream events to the daemon**, return structured result." Two viable transports — pick per the verification:

**Option A (preferred): the agent streams trace events to the daemon as it consumes the Hermes SSE.** The daemon exposes a lightweight ingest endpoint (or the agent writes directly to Redis). The agent becomes a *translator* from Hermes events → bMAS trace events.

```python
# agent/api_server.py  (sketch, /execute rewritten)
async def execute_task(req: TaskRequest):
    run = await hermes.post("/v1/runs", json={
        "model": LITELLM_MODEL,
        "input": req.description,
        "instructions": req.role_prompt,     # persona
        "metadata": {"board_index": req.context.get("board_index") if req.context else None},
    })
    run_id = run["run_id"]

    patches, trace_seq = [], 0
    async for ev in hermes.stream(f"/v1/runs/{run_id}/events"):   # SSE
        bmas_ev = translate(ev, req.task_id, req.turn_id, seq=trace_seq)
        trace_seq += 1
        await emit_trace(req, bmas_ev)        # → daemon ingest or Redis (see §5)
        if bmas_ev["type"] == "final":
            patches = extract_patches(bmas_ev)  # agent returns JSON-Patch proposal

    final = await hermes.get(f"/v1/runs/{run_id}")
    return TaskResponse(
        task_id=req.task_id, status="completed",
        patches=patches,                       # NEW: structured proposal (doc 04 §3)
        usage=final.get("usage"),              # NEW: real token/cost data
        trace_count=trace_seq,
        node_id=NODE_ID, ...,
    )
```

`TaskResponse` gains `patches`, `usage`, and `trace_count`. This single change resurrects the cost path *and* delivers the patch-based mutations from [04](04-blackboard-protocol.md).

## 4. The bMAS trace event schema

One normalized shape, regardless of the Hermes event that produced it. This is what the UI renders ([09](09-ui-agent-trace-inspector.md)).

```jsonc
{
  "trace_id": "trace-turn-7",
  "task_id": "task-a8f2",
  "turn_id": "turn-7",
  "seq": 14,                       // monotonic within a turn (ordering)
  "ts": "2026-06-06T…Z",
  "role": "critic",
  "node": "node-2",
  "type": "reasoning",             // see types below
  "data": { … },                   // type-specific
  "tokens": { "in": 0, "out": 128 },
  "cost_usd": 0.0003
}
```

| `type` | `data` shape | Renders as |
|:--|:--|:--|
| `turn_start` | `{objective, phase}` | timeline header |
| `reasoning` | `{text}` (model thinking / message delta) | streamed text line |
| `tool_call` | `{tool, args}` | tool card (collapsed) |
| `tool_result` | `{tool, ok, summary, artifact_ref}` | tool card (expandable) |
| `token_delta` | `{out}` | live token meter |
| `patch_proposed` | `{ops}` | "proposes N changes" chip |
| `final` | `{summary, patches, usage}` | turn footer + cost |
| `error` | `{message}` | red trace line |

## 5. Transport & persistence

Trace volume can be high (token deltas). Keep it off the hot snapshot path:

```
Agent ──(SSE translate)──► Redis Stream  bmas:traces:{task}:{turn}   (capped, TTL 24h)
                                  │
                                  ├─► Pub/Sub  bmas:events:{task}  event:"trace"  → SSE → UI (live)
                                  └─► batched flush ──► SQLite agent_traces (durable, doc 07)
```

- **Live**: traces ride the *existing* `bmas:events:{task_id}` channel as `event: trace`, so `routes/events.py` needs no structural change ([04 §8](04-blackboard-protocol.md#8-new-sse-event-types-additive)).
- **Durable**: batch-insert to SQLite `agent_traces` on `final` (and periodically for long turns), mirroring the existing dual-write discipline. `reasoning`/`token_delta` spam can be sampled or summarized before archival to control DB growth.
- **Cost**: on `final`, the daemon inserts a real `cost_entry` from `usage` — fixing the dead path — and updates `budget_spent` for the CU's budget ceiling ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).

## 6. Correlation: traces ↔ board ↔ turns

Every artifact shares `task_id` + `turn_id`, and patches carry `trace_id` ([04 §3](04-blackboard-protocol.md#3-json-patch-mutations-rfc-6902)). This lets the UI answer the questions that matter:

- "Which agent reasoning produced *this* board entry?" → entry `created_by_turn` → trace by `turn_id`.
- "What did the Critic *do* before it posted that critique?" → trace stream for the turn, ending in `patch_proposed`.
- "Why was this patch rejected?" → `patch_rejected` event references the `op`; the trace shows the agent's intent.

This correlation is the backbone of the worker-activity visualization ([08 §4](08-ui-blackboard-visualization.md#4-the-worker-activity-lane)).

## 7. Daemon-side changes

- `orchestrator._dispatch_agent` → `control_unit._act`: still POSTs to the node, but now (a) sends the **board index** in `context`, (b) receives `patches` + `usage`, (c) forwards `patches` to the kernel, (d) records cost.
- Subscribe to / ingest the node's trace stream and re-emit as `trace` events.
- On HITL abort, call the node's `POST /v1/runs/{id}/stop` instead of killing a subprocess.

## 8. Graceful degradation

If a node only supports legacy `hermes -z` (Runs API disabled), the agent server falls back to the old subprocess path and emits a **single synthetic trace** (`turn_start` → `final` with the full stdout as one `reasoning` block, `usage` unknown). The UI then shows a coarse trace rather than nothing — no crashes, consistent with the system's fail-open philosophy.

➡️ Continue to [07 — Data Model](07-data-model.md).
