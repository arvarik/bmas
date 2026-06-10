[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Control Unit](05-control-unit.md) | [➡️ Next: Data Model](07-data-model.md)

# 06 — Agent Traces (The Observability Prerequisite)

> [!IMPORTANT]
> **This document must be implemented before the UI documents.** Today the system collects almost no agent-level data ([Gap G5](01-gap-analysis.md#7-the-silent-observability-failure-root-cause-for-the-ui-work)). You cannot visualize traces that aren't being captured. This is the first build phase in the [migration plan](10-migration-and-rollout.md), and it is variant-agnostic — the same trace pipeline serves the traditional core and both future variants.

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

This replaces the `hermes -z` subprocess.

> [!NOTE] Verified live (2026-06-08) — the real event contract
> Confirmed on `agent-node1` by submitting a run through `:8642`. `API_SERVER_ENABLED=true` on all 3 nodes and `usage` **is** returned on completion — but only as token counts (no cost; see §3.1). The SSE stream emits these **actual** event names (not the OpenAI-style names earlier docs assumed):
>
> | Hermes SSE event | bMAS trace `type` (after `translate()`) |
> |:--|:--|
> | `message.delta` (`{delta}`) | `reasoning` (assistant content stream) |
> | `reasoning.available` (`{text}`) | `reasoning` (thinking text) |
> | `tool.started` | `tool_call` |
> | `tool.completed` | `tool_result` |
> | `approval.request` / `approval.responded` | `approval_request` ([doc 12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)) |
> | `run.completed` (`{output, usage}`) | `final` (carries `usage`) |
> | `run.failed` / `run.cancelled` | `error` |
>
> There is **no** `turn_start` or `token_delta` event from Hermes — synthesize `turn_start` in the agent when the run begins, and derive the live token meter from `message.delta` counts. Full list in [HERMES_API.md Appendix A](../HERMES_API.md#appendix-a-gateway-api-server-port-8642).

## 3. Rearchitected agent server

`agent/api_server.py` changes from "run subprocess, return string" to "submit run, **stream events to the daemon**, return structured result."

> [!WARNING] Profile isolation is not provided by the default `/v1/runs` call
> Live source inspection shows `POST /v1/runs` accepts `input`, optional `session_id`, `instructions`, `conversation_history`, `previous_response_id`, and `model`, but no per-request `profile`. The sketch below preserves role identity through `instructions`; it does **not** by itself select a role-specific SOUL/toolset/memory profile. Phase 3a must provide the verified profile-aware dispatch mechanism from [doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles) before the system can claim profile isolation.

**The agent streams trace events to the daemon as it consumes the Hermes SSE.** The daemon exposes a lightweight, batched ingest endpoint — `POST /ingest/traces/{task_id}/{turn_id}` — authenticated with the shared node bearer secret (`BMAS_NODE_KEY`, [03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)). Nodes must **not** write to Redis directly: that would punch a second writer through the daemon/gateway boundary the whole design establishes, hand Redis credentials and topology to every node, and bypass the single ingest point where the daemon stamps ordering, computes `cost_usd` ([§3.1](#31-updated-taskresponse-schema)), and re-emits SSE. The agent is a *translator* from Hermes events → bMAS trace events; the daemon remains the only Redis writer.

```python
# agent/api_server.py  (sketch, /execute rewritten)
async def execute_task(req: TaskRequest):
    workspace = prepare_workspace(req)        # stage attachments, task dir (doc 17 §5)
    run = await hermes.post("/v1/runs", json={
        "model": req.model or LITELLM_MODEL,  # daemon passes the pool-drawn model (05 §2.1); env is the fallback
        "input": render_turn_input(req),      # objective + board + response contract (03 §4)
        "instructions": req.role_prompt,      # persona / generated expert identity
        "session_id": f"{req.task_id}:{req.role}",
    })
    run_id = run["run_id"]

    entries, trace_seq = [], 0
    async for ev in hermes.stream(f"/v1/runs/{run_id}/events"):   # SSE
        bmas_ev = translate(ev, req.task_id, req.turn_id, seq=trace_seq)
        trace_seq += 1
        await emit_trace(req, bmas_ev)        # → daemon ingest endpoint (bearer-auth; §5)
        if bmas_ev["type"] == "final":
            entries = extract_entries(bmas_ev)  # entries_v1 envelope; free-text fallback (04 §3)

    final = await hermes.get(f"/v1/runs/{run_id}")
    artifacts = await sync_artifacts(req, workspace)   # new files → daemon (doc 17 §6)
    return TaskResponse(
        task_id=req.task_id, status="completed",
        entries=entries,                       # NEW: proposed board entries (doc 04 §3)
        usage=final.get("usage"),              # NEW: real token counts (daemon adds cost_usd; §3.1)
        trace_count=trace_seq, artifacts=artifacts,
        node_id=NODE_ID, ...,
    )
```

`TaskResponse` gains `entries`, `usage`, `trace_count`, and `artifacts`. This delivers real **token counts** (the daemon converts them to dollars — §3.1) *and* the structured-envelope contributions from [04 §3](04-blackboard-protocol.md#3-the-agent-response-contract).

### 3.1 Updated `TaskResponse` schema

The current `TaskResponse` ([`api_server.py` L76–84](../agent/api_server.py)) has no `usage` field, which is why the cost path is dead. The Runs API integration replaces it with a richer contract:

```jsonc
// TaskResponse (v2) — agent → daemon
{
  "task_id": "task-a8f2",
  "turn_id": "turn-7",                    // NEW: stable turn identifier for trace correlation
  "run_id": "run-hermes-abc123",          // NEW: Hermes run ID (debugging / cross-referencing)
  "status": "completed",                   // completed | declined | failed | timeout
  "result": "…",                           // final output text (backward compat + free-text fallback)
  "action": "contribute",                  // contribute | decline | clean (doc 04 §3)
  "entries": [                             // NEW: proposed entries (entries_v1)
    { "type": "critique", "title": "…", "body": "…", "refs": ["e-12"], "confidence": 0.66 }
  ],
  "remove": null,                          // cleaner turns only: ["e-7","e-9"]
  "envelope_fallback": false,              // true → entries were wrapped from free text
  "usage": {                               // NEW: token data from the Runs API (verified live)
    "prompt_tokens": 16842,                 // Hermes `usage.input_tokens`
    "completion_tokens": 567,               // Hermes `usage.output_tokens`
    "total_tokens": 17409,                  // Hermes `usage.total_tokens`
    "cost_usd": 0.0214,                    // ⚠️ NOT from Hermes — computed by the daemon (see note)
    "model": "gemini-2.5-flash"            // actual model used (resolved by LiteLLM)
  },
  "trace_id": "trace-turn-7",             // NEW: links this response to its trace stream (§6)
  "trace_count": 42,                       // NEW: number of trace events emitted during the turn
  "artifacts": [                           // NEW: files created this turn, already synced (doc 17 §6)
    { "artifact_id": "a-3", "path": "src/main.py", "bytes": 2048 }
  ],
  "node_id": "node-2",
  "request_id": "a1b2c3",
  "duration_ms": 3412,
  "timestamp": "2026-06-10T…Z"
}
```

> [!IMPORTANT] Dollar cost is computed by the daemon, not read from Hermes (verified live 2026-06-08)
> A live `run.completed` returns `usage: {input_tokens, output_tokens, total_tokens}` and **no cost field**; Hermes's own `/api/analytics/*` report `estimated_cost`/`actual_cost` as `0` for the LiteLLM-backed `custom`/`gemini` provider. So **the cost-path fix is two parts, not one**:
> 1. The agent returns real **token counts** from `usage` (this part Hermes gives us).
> 2. The **daemon computes `cost_usd`** = `input_tokens × price_in + output_tokens × price_out`, using a per-model price table in `bmas.yaml` (the models are already declared there), **or** by reading LiteLLM's response cost (`x-litellm-response-cost` header / `response_cost` in the LiteLLM usage). Do **not** rely on a `cost_usd` from the Hermes payload — it will be absent/zero, silently re-breaking the Cost tab and the `budget_ceiling_usd` rail ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).
>
> Treat `cost_usd` above as a **daemon-derived** field, set during ingestion, never trusted from the node.

> [!NOTE]
> - `entries` may be the free-text fallback wrap ([04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)) — the turn still contributes, flagged `envelope_fallback: true`. A turn produces **no** board mutation only on `decline` or hard failure; the trace is captured regardless.
> - `usage` may be `null` under the legacy `hermes -z` fallback ([§8](#8-graceful-degradation)). The daemon must handle `null` gracefully — log a warning but don't crash the cost path.
> - `result` is kept for backward compatibility and the degradation path; the gateway reads `entries`, not `result`.
> - **Context cost is non-trivial even for tiny tasks:** a live "17+25" run consumed ~16k input tokens (full system prompt + skills + memory loaded per run). This is why the Cleaner must actually run, why entry bodies are capped ([04 §4](04-blackboard-protocol.md#4-the-board-gateway)), and why per-turn budget accounting matters from round one ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).

## 4. The bMAS trace event schema

One normalized shape, regardless of the Hermes event that produced it. This is what the UI renders ([09](09-ui-agent-trace-inspector.md)).

```jsonc
{
  "trace_id": "trace-turn-7",
  "task_id": "task-a8f2",
  "turn_id": "turn-7",
  "seq": 14,                       // monotonic within a turn (ordering)
  "ts": "2026-06-10T…Z",
  "role": "critic",                // opaque actor string (seam rule 3)
  "node": "node-2",
  "type": "reasoning",             // see types below
  "data": { … },                   // type-specific
  "tokens": { "in": 0, "out": 128 },
  "cost_usd": 0.0003
}
```

| `type` | `data` shape | Renders as |
|:--|:--|:--|
| `turn_start` | `{objective, phase, round}` | timeline header |
| `reasoning` | `{text}` (model thinking / message delta) | streamed text line |
| `tool_call` | `{tool, args}` | tool card (collapsed) |
| `tool_result` | `{tool, ok, summary, artifact_ref}` | tool card (expandable) |
| `token_delta` | `{out}` (synthesized from `message.delta`) | live token meter |
| `entries_posted` | `{count, types, ids}` | "posted 1 critique → e-12" chip |
| `approval_request` | `{action, args}` | inline Approve/Deny ([12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)) |
| `final` | `{summary, entries, usage}` | turn footer + cost |
| `error` | `{message}` | red trace line |

## 5. Transport & persistence

Trace volume can be high (token deltas). Keep it off the hot snapshot path:

```
Agent ──(translate + batch)──► Daemon ingest  POST /ingest/traces/{task}/{turn}  (BMAS_NODE_KEY bearer)
                                  │
                                  ▼
                          Redis Stream  bmas:traces:{task}:{turn}   (capped, TTL 24h; daemon is sole writer)
                                  │
                                  ├─► Pub/Sub  bmas:events:{task}  event:"trace"  → SSE → UI (live)
                                  └─► batched flush ──► SQLite agent_traces (durable, doc 07)
```

- **Live**: traces ride the *existing* `bmas:events:{task_id}` channel as `event: trace`, so `routes/events.py` needs no structural change ([04 §9](04-blackboard-protocol.md#9-new-sse-event-types-additive)).
- **Durable**: batch-insert to SQLite `agent_traces` on `final` (and periodically for long turns), mirroring the existing dual-write discipline. `reasoning`/`token_delta` spam can be sampled or summarized before archival to control DB growth.
- **Cost**: on `final`, the daemon inserts a real `cost_entry` — token counts from `usage`, **dollar cost computed daemon-side** (price table or LiteLLM response cost; §3.1) — fixing the dead path and updating `budget_spent` for the CU's budget ceiling ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).

## 6. Correlation: traces ↔ board ↔ turns

Every artifact shares `task_id` + `turn_id`, and entries carry `created_by_turn` ([04 §1](04-blackboard-protocol.md#1-board-entries-typed-envelopes-natural-language-bodies)). This lets the UI answer the questions that matter:

- "Which agent reasoning produced *this* board entry?" → entry `created_by_turn` → trace by `turn_id`.
- "What did the Critic *do* before it posted that critique?" → trace stream for the turn, ending in `entries_posted`.
- "Why did this turn contribute nothing?" → the trace shows `decline` or the `entry_rejected` event references the proposal.
- "Where did this artifact come from?" → artifact rows carry `turn_id` ([17 §6](17-files-and-artifacts.md#6-artifacts-agent-created-files)).

This correlation is the backbone of the worker-activity visualization ([08 §4](08-ui-blackboard-visualization.md#4-the-worker-activity-lane)).

## 7. Daemon-side changes

- `orchestrator._dispatch_agent` → the variant's `_act`: still POSTs to the node, but now (a) sends the **board serialization** (full or budgeted, [03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)) plus the **pool-drawn `model`**, (b) receives `entries` + `usage` + `artifacts`, (c) forwards `entries` to the Board Gateway, (d) records cost.
- Subscribe to / ingest the node's trace stream and re-emit as `trace` events.
- On HITL abort, call the node's `POST /v1/runs/{id}/stop` instead of killing a subprocess.
- **Raise the timeouts:** today's `httpx.AsyncClient(timeout=120)` (`orchestrator.py`) and `TASK_TIMEOUT_SECONDS=120` (`agent/api_server.py`) bound a *single* call; a multi-round cycle with tool-using agents will exceed that. The agent consumes the run SSE without a fixed wall-clock; the daemon's timeout becomes a per-turn budget (config `coordination.turn_timeout_s`, default 600).

## 8. Graceful degradation

If a node only supports legacy `hermes -z` (Runs API disabled), the agent server falls back to the old subprocess path and emits a **single synthetic trace** (`turn_start` → `final` with the full stdout as one `reasoning` block, `usage` unknown) and wraps the stdout via the free-text fallback ([04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)). The UI then shows a coarse trace rather than nothing — no crashes, consistent with the system's fail-open philosophy.

➡️ Continue to [07 — Data Model](07-data-model.md).
