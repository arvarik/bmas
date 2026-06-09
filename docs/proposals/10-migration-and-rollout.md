[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ UI: Agent Trace Inspector](09-ui-agent-trace-inspector.md)

# 10 — Migration, Rollout & Open Questions

> [!ABSTRACT]
> How to ship the inversion without a big-bang rewrite or a broken dashboard. Phased, feature-flagged, additive-first. Includes the sequencing rationale, risk register, verification checklist for unverified Hermes capabilities, and acceptance criteria.

> [!NOTE] Current state of the system (live, as of 2026-06-07)
> What's already true on the cluster, so the plan starts from reality:
> - ✅ **Runs API gateway** enabled + boot-persistent on all 3 agent nodes (`:8642`); `/v1/capabilities.features.*` verified, and a **live run was submitted (2026-06-08)** to confirm the actual SSE event names and `usage` shape. The single hard Phase-1 prerequisite is **cleared**.
> - ⚠️ **Cost caveat (found via the live run):** `usage` returns token counts but **no dollar cost** — the daemon must compute `cost_usd` itself ([Q2](#3-open-questions-verify-before-building), [doc 06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)).
> - ✅ **`:9119` dashboards** active on all 3 nodes.
> - ✅ **All 3 nodes confirmed identical** — generic shared `SOUL.md`, no profiles, no per-node customization. Role identity is still runtime-injected (`AGENTS.md`), i.e. the orchestrator-worker mapping is still in force.
> - ⚠️ **Profile dispatch caveat:** Hermes profiles are real, but live `/v1/runs` has **no verified per-request `profile` selector**. A gateway is profile-scoped. Phase 3 must choose and verify per-profile gateways/ports, a local `hermes --profile` bridge, or another supported dispatch mechanism before role-profile isolation can be claimed.
> - ⚠️ **Open capability gaps** (design around, not blockers): `jobs_admin` and `memory_write_api` are **not** exposed over the API server ([Q9/Q10](#3-open-questions-verify-before-building)).
> - ⏳ **Not yet started:** the code work — Phases 0–5 below. No profiles authored, no kernel, no `CoordinationStrategy`, traces still via `hermes -z`.

---

## 1. Sequencing rationale

The order is dictated by one hard dependency and one risk principle:

1. **Traces before visualization.** You cannot render data you don't collect ([06](06-agent-traces.md)). The trace pipeline is also the *cheapest* high-value win: it improves the current system immediately, before any architectural change.
2. **Substrate before behavior.** The deterministic kernel + board model ([04](04-blackboard-protocol.md)) must exist and be unit-tested before the Control Unit ([05](05-control-unit.md)) drives agents through it.

> [!IMPORTANT] Showcase/novelty constraint: V1 must not foreclose V2
> The novel contribution lives in **V2 (the stigmergic regime)** and in the showcase demos it enables ([doc 15](15-novelty-and-research-directions.md)) — but V2 is built *separately and later*. Therefore the **[seams checklist](11-extensibility-and-variants.md#6-the-seams-checklist-enforce-in-v1) is a hard merge gate for every V1 PR**, not advice. If a V1 phase hard-codes roles, bakes "Control Unit" into the kernel/board/traces/UI, or makes decay non-pluggable, the novel work becomes a rewrite and the artifact loses its point. Each phase's definition of done includes *"passes the seams checklist."* V1 builds the **hooks**; it does not build V2.

## 2. Phases

### Phase 0 — Foundations (no behavior change yet)
- [ ] SQLite migration v2 ([07](07-data-model.md)) — additive tables/columns, `SCHEMA_VERSION=2`. This must land before Phase 1 persists traces into `agent_traces`.
- [ ] Redis v2 keys + new SSE event names registered (no emitters yet) ([04 §7–8](04-blackboard-protocol.md#7-redis-schema-v2)).
- [ ] `config.py`: add the `coordination.*` block (`strategy`, `view_budget_tokens`, `control_unit.*` — the complete key set in [05 §3](05-control-unit.md#3-consensus--termination) — and `stigmergic.*`) and `pressure.weights` with fail-fast validation ([11 §7](11-extensibility-and-variants.md#7-config-sketch-both-variants-visible)); `strategy` default `legacy_pipeline`. Add a `blackboard_v2` build flag (gates the new substrate independently of which strategy runs). Add the **`BMAS_NODE_KEY`** shared bearer secret for all node↔daemon surfaces ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)) — fail-fast if unset.
- [ ] Add model pricing config (input/output token price per model alias, plus optional LiteLLM-cost source) so Phase 1 can compute `cost_usd` from Hermes token counts. The live Runs API returns no dollar cost.
- [ ] Establish the `CoordinationStrategy` seam ([11 §2](11-extensibility-and-variants.md#2-the-seam-coordinationstrategy)) and the [seams checklist](11-extensibility-and-variants.md#6-the-seams-checklist-enforce-in-v1) — opaque `author` strings, capability-based kernel auth, pluggable decay — **before** writing strategy logic, or the stigmergic variant becomes a rewrite.

### Phase 1 — Agent traces (ship standalone, high ROI) ⭐
- [x] **Enable the Hermes Runs API on every node** — ✅ **DONE 2026-06-07.** `API_SERVER_*` set in `.env` + `hermes gateway install --system --run-as-user root`; `hermes-gateway.service` active/enabled on all 3 nodes (`:8642`), `/v1/capabilities.features.*` verified ([doc 12 §4](12-hermes-and-node-topology.md#4-enabling-the-runs-api-the-phase-1-unblocker--done-on-all-3-nodes-2026-06-07)). This was the hard prerequisite — traces are now possible.
- [x] Confirm the exact `/v1/runs/{id}/events` and terminal `/v1/runs/{id}` payload shapes on a live node ([§3 checklist](#3-open-questions-verify-before-building)). Keep the captured payload as a parser fixture when implementing Phase 1.
- [ ] Rewrite `agent/api_server.py` to use the Runs API and stream trace events; add `usage`/`patches`/`trace_count` to `TaskResponse` ([06 §3](06-agent-traces.md#3-rearchitected-agent-server)).
- [ ] Daemon ingests traces → Redis + SQLite; resurrect the cost path using token counts from `usage` **with `cost_usd` computed daemon-side** (price table or LiteLLM cost — Hermes returns no cost; [06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)). **Capture cost per-task *and* per-model *and* per-node**, not just a total — the [cost/locality-frontier demo](15-novelty-and-research-directions.md#5-concrete-demos--experiments-to-build-ranked-by-wow-per-effort) needs this breakdown.
- [ ] **(Showcase hook) Energy/telemetry per task** — record per-node power draw over each task window from the existing Beszel telemetry, and store `joules_estimate` alongside cost. Enables the "joules per solved task" metric ([doc 15 §4](15-novelty-and-research-directions.md#4-the-distributed-angle-what-only-physical-distribution-gives-you)). Additive; no behavior change.
- [ ] Legacy `hermes -z` fallback for nodes without the Runs API ([06 §8](06-agent-traces.md#8-graceful-degradation)).
- [ ] UI: Logs tab Trace/Raw toggle + trace timeline ([09](09-ui-agent-trace-inspector.md)). **Works on the existing pipeline** — immediate value even before the blackboard inversion.

> [!NOTE]
> Phase 1 is independently shippable and reversible. Even if the blackboard inversion stalls, the system gains real traces and working cost tracking — strictly better than today.

### Phase 2 — The board substrate (behind `blackboard_v2`)
- [ ] `daemon/src/models/schemas.py` — JSON Schemas per entry type ([04 §1](04-blackboard-protocol.md#entry-types)).
- [ ] `daemon/src/core/kernel.py` — deterministic kernel + authorization matrix + CAS (with the lost-update-safe retry rule, [04 §5](04-blackboard-protocol.md#5-optimistic-concurrency)) + `test`-op preconditions ([04 §3.1](04-blackboard-protocol.md#31-test-ops-explicit-stale-view-preconditions)) + salience. **Unit-test with an in-memory fake** (no LLM, no Redis): feed proposals, assert committed/rejected ([04 §4, §9](04-blackboard-protocol.md#4-the-deterministic-kernel)).
- [ ] Kernel computes + stores the rolling **board state hash** after each commit ([04 §4](04-blackboard-protocol.md#the-board-state-hash-livelock-support)) — the deterministic signal the livelock circuit-breaker ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)) and the UI consume.
- [ ] Rewrite `blackboard.py` to the v2 API ([04 §9](04-blackboard-protocol.md#9-blackboard-api-surface-replaces-ad-hoc-methods)); keep old methods until UI cutover.
- [ ] Emit `board_patch` / `patch_rejected` events.
- [ ] Define the event-log durability contract: task-local monotonic `seq`, write ordering between Redis Stream and SQLite, and explicit recovery behavior if one write succeeds and the other fails. `board_patches` is the durable source of truth, so these writes cannot be casual best-effort like legacy logs.
- [ ] **(Showcase hook) Event log supports fork-from-event, not just linear replay.** Persist the full patch event-log with stable offsets and a `fork(task_id, at_event_n, mutate_fn)` that re-materializes board state up to event *n*. This is what enables [counterfactual replay](15-novelty-and-research-directions.md#35-causality--replay-enabled-by-event-sourcing) ("suppress agent X's turn, re-run") — design it in now; building the linear scrubber later on top is then trivial.

### Phase 3 — The Control Unit (the V1 `ControlUnitStrategy`)
- [ ] `daemon/src/core/coordination.py` — the `CoordinationStrategy` interface + `ControlUnitStrategy` ([11 §2](11-extensibility-and-variants.md#2-the-seam-coordinationstrategy)).
- [ ] `daemon/src/core/control_unit.py` — OODA loop reading the **pressure field**, two-tier DECIDE, consensus scorer ([05](05-control-unit.md)).
- [ ] **Author + deploy the role profiles** (planner/expert/critic/conflict_resolver/cleaner/decider, `SOUL.md` + toolset-scoped `config.yaml`) + a `universal` profile (+ optional `coordinator`); replicate to all 3 nodes; add the role→(preferred_host, profile, dispatch_endpoint) registry ([doc 12 §2.5–3](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)).
- [ ] **Verify profile-aware dispatch before relying on profiles.** Live `/v1/runs` has no per-request `profile` field. Pick per-profile gateways/ports, a local `hermes --profile` bridge, or another verified mechanism and record the exact request/command shape in doc 12.
- [ ] New role personas + authorization (capability) profiles in `personas.py` ([05 §2](05-control-unit.md#2-the-paper-role-group)).
- [ ] **Cost + progress rails in the same PR**: budget ceiling, round/duration caps, concurrency cap, **and the livelock circuit-breaker** (`stall_rounds` on board-hash/no-op/rejection streaks) ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)). Daemon-side decline-gating (deterministic → cheap LiteLLM; never a Hermes run) ships here too.
- [ ] Dual-write board→legacy `debate_entries` so the old Blackboard tab keeps rendering.
- [ ] A/B: run `legacy_pipeline` vs `blackboard_v2` on identical tasks; compare quality, latency, cost.

### Phase 4 — Blackboard visualization

> [!NOTE] Parallelism constraint
> Phase 4 coding may begin in parallel with Phases 2–3 (it touches `mission-control/` only — different files). However, Phase 4 should **not be verified or merged** until Phase 1 (traces) and Phase 2 (kernel) are merged, because the UI components need real trace data and board entries to render against. Stubbed/mock data is acceptable during development, but the PR's "VERIFY" step must run against a live task with the real trace + board pipeline.
- [ ] Agent-role tokens added to `DESIGN.md` + `design-tokens.ts` + `globals.css` ([08 §8](08-ui-blackboard-visualization.md#8-token--primitive-additions-do-this-first)).
- [ ] `BlackboardGraph`, `WorkerLane`, `ConsensusMeter`; blackboard tab Graph/Stream toggle ([08](08-ui-blackboard-visualization.md)).
- [ ] Turn Inspector slide-over and graph/worker-card cross-links ([09](09-ui-agent-trace-inspector.md)).
- [ ] `useTaskStream` handles new events with batching ([09 §8 note](09-ui-agent-trace-inspector.md#8-files)).
- [ ] Replay scrubber from `board/replay` (linear) — built on the Phase-2 fork-capable event log; the counterfactual-fork UI is a later/optional extension.

### Phase 5 — Advanced & cutover
- [ ] Private sub-boards + Conflict-Resolver ([05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution)).
- [ ] Budget gauge on Cost tab ([09](09-ui-agent-trace-inspector.md)).
- [ ] Flip `coordination.strategy` default to `control_unit`; deprecate legacy `debate_entries` dual-write.
- [ ] Mission cockpit view + parallel trace lanes + pressure heatmap + firehose ([doc 13](13-ui-showcase-density.md)).

### Phase E (cross-cutting — begin at Phase 1) — Evaluation, A/B & showcase instrumentation

> [!NOTE] This is the spine of the "novel artifact" claim. Without it, the novelty is an assertion; with it, it's a result. Build it incrementally alongside the phases above, not at the end.

- [ ] **Benchmark harness** — reproduce the paper's evaluation surface ([doc 15 §3.4](15-novelty-and-research-directions.md#34-llm-mas-scaling--diversity)): a runner that submits a labeled dataset (start with **GSM8K + MMLU subset**, the paper's beds) through bMAS and scores accuracy. Store runs with full config (strategy, models, rounds) for comparability.
- [ ] **Metrics capture per run** — accuracy, tokens, **$**, latency, rounds-to-consensus, consensus-reached %, and `joules_estimate` (from the Phase-1 hook). One row per task; one summary per benchmark. This is the table that goes in the writeup.
- [ ] **A/B harness (regime comparison)** — same dataset, swap only `coordination.strategy`: `legacy_pipeline` vs `control_unit` now, and `stigmergic` later. Emit a side-by-side report. (The *UI* side-by-side demo — two live regimes on one query — is a [doc 13](13-ui-showcase-density.md) surface; this is the data engine behind it.)
- [ ] **(For the V2 robustness experiment) Failure-injection tooling** — a way to drop/partition a node mid-task and record degradation, so the [kill-a-node resilience demo](15-novelty-and-research-directions.md#4-the-distributed-angle-what-only-physical-distribution-gives-you) is repeatable, not a one-off. The hook is V1 (node-health + graceful degradation in the substrate); the *experiment* needs V2 to be interesting.

### Phase 6 (optional, future) — Pure-stigmergic variant
- [ ] Implement `StigmergicStrategy` against the same substrate ([11 §4](11-extensibility-and-variants.md#4-the-stigmergic-variant-specified)): roleless `universal` actors, exponential pheromone decay, parallel patch competition, basin-based termination.
- [ ] Pull-mode self-activation via Hermes crons on the pressure field ([12 §6](12-hermes-and-node-topology.md#6-pull-mode-crons-for-the-stigmergic-future)) — **note ([Q9](#3-open-questions-verify-before-building)): crons are CLI/`config.yaml`-managed, not HTTP-managed** (`features.jobs_admin=false`), so the daemon provisions them via SSH/CLI, not an API call.
- [ ] `coordination.strategy: stigmergic` — runs on the *unchanged* kernel/board/traces/UI if the [seams checklist](11-extensibility-and-variants.md#6-the-seams-checklist-enforce-in-v1) was honored.

## 3. Open Questions (verify before building)

> [!WARNING]
> These assumptions gate real engineering time. Verify each on a live node/cluster and record the answer before the dependent phase starts.

| # | Question | Status | Detail |
|:--|:--|:--|:--|
| Q1 | Hermes Runs API exists with `/v1/runs` + `/v1/runs/{id}/events` SSE? | ✅ **Confirmed + LIVE (run submitted 2026-06-08)** | Gateway enabled on all 3 nodes. `/v1/capabilities` booleans live under `features.*`. **Real SSE event names**: `message.delta`, `reasoning.available`, `tool.started`/`tool.completed`, `approval.request`/`responded`, `run.completed`/`failed`/`cancelled` — *not* the OpenAI-style names early drafts assumed ([doc 06 §2](06-agent-traces.md#2-the-enabler-the-hermes-runs-api)) |
| Q2 | Does run completion return populated `usage` / cost? | ✅/⚠️ **Confirmed live, with a caveat** | `usage` returns **token counts** (`input/output/total_tokens`) — but **no cost field** (provider `custom`/`gemini`). **Daemon must compute `cost_usd`** from tokens × price (or LiteLLM cost), not read it from Hermes ([doc 06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)) |
| Q3 | Can Hermes reliably emit a **structured JSON-Patch proposal** as final output? | ⚠️ **Hard Phase-2 gate** | Prompt-test against the live Runs API before the CU uses patches. If unreliable, implement one explicit fallback: daemon-side extraction + kernel validation + retry-with-rejection-feedback. Do not enter Phase 3 assuming clean patches without evidence. |
| Q4 | Rely on kernel rejection vs prompt for authorization? | ✅ **Decided** | Rely on the **kernel** (prompt advisory). Confirm rejection UX. |
| Q5 | Latency/cost of multi-round cyclic execution vs the 3-call pipeline? | ⚠️ **Measure** | A/B; tune `max_rounds` per triage tier. **Also raise the timeouts:** today's `httpx.AsyncClient(timeout=120)` ([`orchestrator.py`](../daemon/src/core/orchestrator.py)) and `TASK_TIMEOUT_SECONDS=120` ([`agent/api_server.py`](../agent/api_server.py)) bound a *single* call; a 4-round loop with concurrent agents and board reads will exceed that. Switch the agent to a **long-lived SSE consume** of `/v1/runs/{id}/events` (no fixed per-turn wall-clock) and make the daemon's turn timeout a per-turn, not per-task, budget. |
| Q6 | Hermes profiles for the role group? | ✅/⚠️ **Profiles confirmed; dispatch unresolved** | Profiles are fully isolated (own SOUL/config/skills), but none exist yet and live `/v1/runs` has no per-request `profile` selector. Author profiles, then verify a profile-aware dispatch path before relying on them ([doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)). |
| Q7 | `POST /v1/runs/{id}/stop` + `/approval` for HITL? | ✅ **Confirmed in source** | Wire stop = abort, approval = inline operator gate ([doc 12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)). Test live once gateway is on. |
| Q8 | Responses API `previous_response_id` for cross-turn agent memory? | ✅ **Confirmed** | Use `session_id={task}:{role}` for correlation ([doc 12 §5.2](12-hermes-and-node-topology.md#52-stateful-turns-via-the-responses-api)) |
| Q9 | Manage Hermes **crons** over the API server (for V2 pull-mode)? | ❌/⚠️ **Do not assume HTTP writes** | `/v1/capabilities.features.jobs_admin=false`. `GET /api/jobs` lists jobs, but create/update/delete are not advertised as supported and must be live-tested before use. Conservative path: create crons via Hermes **CLI / `config.yaml`** on each node ([doc 12 §6](12-hermes-and-node-topology.md#6-pull-mode-crons-for-the-stigmergic-future)). Affects **V2 only** — no V1 impact. |
| Q10 | Read/write agent **memory** over the API server (for the UI "minds")? | ❌ **Confirmed NO** | `/v1/capabilities.features.memory_write_api=false` (and `features.admin_config_rw=false`). Display memory via the `:9119` dashboard / CLI; do not plan an HTTP memory-write path from the daemon. |
| Q11 | How do agents pull board entries by ID? | ✅ **Decided** | Default: **`prehydrated` bounded views** under `coordination.view_budget_tokens` (daemon materializes index + selected full payloads; smallest-budget-wins evidence from [PatchBoard 2026](https://arxiv.org/abs/2605.29313)), with `daemon_api` pull-by-ID as the large-board escape hatch. All node↔daemon surfaces (board read, trace ingest) authenticated via the shared `BMAS_NODE_KEY` bearer secret. Full contract in [doc 03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target). |

## 4. Risk register

| Risk | Likelihood | Impact | Mitigation |
|:--|:--|:--|:--|
| Cyclic loop runs up cloud cost | Med | High | Budget ceiling + round/duration caps shipped *with* the loop; **daemon-side** decline gating (the ~16k-token Hermes context floor makes dispatched declines expensive); bounded board views ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)) |
| Accepted-but-no-progress livelock (rounds spin below the caps) | Med | Med | Deterministic circuit-breaker on board-hash repeats / hash cycles / rejection streaks (`stall_rounds`, [05 §5](05-control-unit.md#5-cost-governance--safety-rails)); kernel emits the hash ([04 §4](04-blackboard-protocol.md#the-board-state-hash-livelock-support)) |
| Agents can't emit clean patches (Q3) | Med | High | Daemon-side patch extractor fallback; kernel rejects malformed → visible, recoverable |
| Trace volume bloats SQLite | Med | Med | Cap/TTL Redis streams; sample/summarize `reasoning`/`token_delta`; retention job ([07 §5](07-data-model.md#5-retention--size-control)) |
| `useTaskStream` re-render storm | Med | Med | Batch trace events (rAF/debounce) ([09 §8](09-ui-agent-trace-inspector.md#8-files)) |
| Dashboard breaks during migration | Low | High | Additive schema; dual-write debate→board; new SSE events are additive; everything behind `blackboard_v2` |
| Concurrency bugs in kernel CAS | Med | High | Deterministic kernel is fully unit-testable without LLM/Redis; property-test concurrent proposals ([04 §9](04-blackboard-protocol.md#9-blackboard-api-surface-replaces-ad-hoc-methods)) |
| Profile dispatch assumption is wrong | Med | High | Verify profile-aware dispatch in Phase 3 before depending on SOUL/toolset isolation; fall back to `instructions` identity only if profile isolation is explicitly deferred |
| Quality regresses vs pipeline | Low | Med | A/B `legacy_pipeline` flag; keep escape hatch through Phase 5 |

## 5. Backward-compatibility contract

- **SQLite**: additive only (new tables, `ADD COLUMN`). Existing queries untouched.
- **SSE**: new event *names*; `routes/events.py` forwards any `{event,data}` unchanged ([04 §8](04-blackboard-protocol.md#8-new-sse-event-types-additive)). Legacy `debate`/`phase`/`subtask`/`cost`/`complete` keep firing through Phase 4.
- **UI**: new components live behind toggles inside existing tabs; the current `DebateList`/`DAGVisualizer`/`TaskLogTerminal` keep working until explicitly replaced.
- **Config**: new keys validated fail-fast (matching `config.py` style); `legacy_pipeline: true` reproduces today's behavior exactly.

## 6. Definition of done (maps to README §4)

- [ ] An agent reads a peer's board entry and posts a critique **without daemon instruction** (Phase 3).
- [ ] ≥2 agents contribute concurrently to one task; graph shows interleaving (Phase 3/4).
- [ ] CU halts on consensus threshold, not a fixed pipeline end; convergence meter reflects it (Phase 3/4).
- [ ] Opening a running task shows live reasoning + tool calls + token deltas (Phase 1).
- [ ] Blackboard tab renders a live graph animating as patches land (Phase 4).
- [ ] Daemon restart mid-task replays the board from the patch log without corruption (Phase 2).
- [ ] Per-task cost is real and bounded by `budget_ceiling_usd` (Phase 1 + 3).

## 7. Estimated effort (rough, for sequencing — not a commitment)

| Phase | Surface | Relative size |
|:--|:--|:--|
| 0 | schema/config scaffolding | S |
| 1 | agent traces + cost + Logs UI | M |
| 2 | kernel + board model + tests | L |
| 3 | control unit + roles + cost rails | L |
| 4 | blackboard graph + worker lane + tokens | M–L |
| 5 | private boards, inspector, cutover, advanced | M |

> [!TIP]
> Ship **Phase 1 first and independently**. It de-risks the Hermes integration (Q1–Q2), delivers immediate operator value (real traces, working cost), and produces the data the later visualization phases need — all without committing to the architectural inversion. If Phase 1 reveals the Runs API isn't viable, you learn it cheaply, before building the kernel and CU.
