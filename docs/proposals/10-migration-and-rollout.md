[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ UI: Agent Trace Inspector](09-ui-agent-trace-inspector.md) | [➡️ Next: Variant — PatchBoard](11-variant-patchboard.md)

# 10 — Migration, Rollout & Open Questions

> [!ABSTRACT]
> How to ship the inversion without a big-bang rewrite or a broken dashboard. Phased, feature-flagged, additive-first. Includes the sequencing rationale, risk register, verification checklist for unverified Hermes capabilities, and acceptance criteria. The target of this plan is the **traditional LbMAS variant** ([03](03-target-architecture.md)–[05](05-control-unit.md)) plus files/artifacts ([17](17-files-and-artifacts.md)); the PatchBoard ([11](11-variant-patchboard.md)) and stigmergic ([16](16-variant-stigmergic.md)) variants are *post-V1* drop-ins enabled by the seams enforced here.

> [!NOTE] Current state of the system (live, as of 2026-06-07)
> What's already true on the cluster, so the plan starts from reality:
> - ✅ **Runs API gateway** enabled + boot-persistent on all 3 agent nodes (`:8642`); `/v1/capabilities.features.*` verified, and a **live run was submitted (2026-06-08)** to confirm the actual SSE event names and `usage` shape. The single hard Phase-1 prerequisite is **cleared**.
> - ⚠️ **Cost caveat (found via the live run):** `usage` returns token counts but **no dollar cost** — the daemon must compute `cost_usd` itself ([Q2](#3-open-questions-verify-before-building), [doc 06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)).
> - ✅ **`:9119` dashboards** active on all 3 nodes.
> - ✅ **All 3 nodes confirmed identical** — generic shared `SOUL.md`, no profiles, no per-node customization. Role identity is still runtime-injected (`AGENTS.md`), i.e. the orchestrator-worker mapping is still in force.
> - ⚠️ **Profile dispatch caveat:** Hermes profiles are real, but live `/v1/runs` has **no verified per-request `profile` selector**. A gateway is profile-scoped. Phase 3 must choose and verify per-profile gateways/ports, a local `hermes --profile` bridge, or another supported dispatch mechanism before role-profile isolation can be claimed.
> - ⚠️ **Open capability gaps** (design around, not blockers): `jobs_admin` and `memory_write_api` are **not** exposed over the API server ([Q9/Q10](#3-open-questions-verify-before-building)).
> - ⏳ **Not yet started:** the code work — Phases 0–6 below. No profiles authored, no gateway, no `CoordinationVariant`, no storage config, traces still via `hermes -z`.

---

## 1. Sequencing rationale

The order is dictated by one hard dependency and one risk principle:

1. **Traces before visualization.** You cannot render data you don't collect ([06](06-agent-traces.md)). The trace pipeline is also the *cheapest* high-value win: it improves the current system immediately, before any architectural change.
2. **Substrate before behavior.** The Board Gateway + event-sourced board model ([04](04-blackboard-protocol.md)) must exist and be unit-tested before the Control Unit ([05](05-control-unit.md)) drives agents through it.

> [!IMPORTANT] Extensibility constraint: V1 must not foreclose the variants
> The PatchBoard and stigmergic variants are built *separately and later* — but only if V1 honors the **[seams checklist](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)**, which is a **hard merge gate for every V1 PR**, not advice. If a V1 phase hard-codes role names, bakes the Control Unit into the gateway/board/traces/UI shell, or makes derived-field computation non-pluggable, each future paradigm becomes a rewrite. Each phase's definition of done includes *"passes the seams checklist."* V1 builds the **hooks** (variant interface, opaque actors, capability auth, panel registry, dropdown); it does not build the variants.

## 2. Phases

### Phase 0 — Foundations (no behavior change yet)
- [x] SQLite migration v2 ([07](07-data-model.md)) — additive tables/columns (`board_entries`, `board_events`, `agent_traces`, `turns`, `task_files`, `artifacts`; `tasks`/`cost_entries` columns), `SCHEMA_VERSION=2`. This must land before Phase 1 persists traces into `agent_traces`.
- [x] Redis v2 keys + new SSE event names registered (no emitters yet) ([04 §8–9](04-blackboard-protocol.md#8-redis-schema-v2)).
- [x] `config.py`: add the `coordination.*` block (`variant`, `view_budget_tokens`, `round_execution`, the complete `traditional.*` key set from [05 §3](05-control-unit.md#3-consensus--termination)) with fail-fast validation; `variant` default `legacy_pipeline` until cutover. Add a `blackboard_v2` build flag (gates the new substrate independently of which variant runs).
- [x] `config.py`: add the **`storage.*` block** (`user_media_dir`, `artifacts_dir`, size caps, allowed types, `pdf_extraction` — [17 §2](17-files-and-artifacts.md#2-storage-configuration-the-brain-node-owns-disk)) with directory-writability checks at startup.
- [x] Add the **`BMAS_NODE_KEY`** shared bearer secret for all node↔daemon surfaces ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)) — fail-fast if unset.
- [x] Add model pricing config (input/output token price per model alias, plus optional LiteLLM-cost source) so Phase 1 can compute `cost_usd` from Hermes token counts. The live Runs API returns no dollar cost.
- [x] Establish the **`CoordinationVariant` seam** (`daemon/src/core/variants/__init__.py`, [03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)) and adopt the seams checklist as the PR merge gate — opaque `author` strings, capability-based gateway auth, pluggable `recompute_derived` — **before** writing variant logic.

### Phase 1 — Agent traces (ship standalone, high ROI) ⭐
- [x] **Enable the Hermes Runs API on every node** — ✅ **DONE 2026-06-07.** `API_SERVER_*` set in `.env` + `hermes gateway install --system --run-as-user root`; `hermes-gateway.service` active/enabled on all 3 nodes (`:8642`), `/v1/capabilities.features.*` verified ([doc 12 §4](12-hermes-and-node-topology.md#4-enabling-the-runs-api-the-phase-1-unblocker---done-on-all-3-nodes-2026-06-07)). This was the hard prerequisite — traces are now possible.
- [x] Confirm the exact `/v1/runs/{id}/events` and terminal `/v1/runs/{id}` payload shapes on a live node ([§3 checklist](#3-open-questions-verify-before-building)). Keep the captured payload as a parser fixture when implementing Phase 1.
- [ ] Rewrite `agent/api_server.py` to use the Runs API and stream trace events; add `usage`/`entries`/`trace_count`/`artifacts` to `TaskResponse` ([06 §3](06-agent-traces.md#3-rearchitected-agent-server)).
- [ ] Daemon ingests traces → Redis + SQLite; resurrect the cost path using token counts from `usage` **with `cost_usd` computed daemon-side** (price table or LiteLLM cost — Hermes returns no cost; [06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)). **Capture cost per-task *and* per-model *and* per-node**, not just a total — the [cost/locality-frontier demo](15-novelty-and-research-directions.md#5-concrete-demos--experiments-to-build-ranked-by-wow-per-effort) needs this breakdown.
- [ ] **(Showcase hook) Energy/telemetry per task** — record per-node power draw over each task window from the existing Beszel telemetry, and store `joules_estimate` alongside cost. Enables the "joules per solved task" metric ([doc 15 §4](15-novelty-and-research-directions.md#4-the-distributed-angle-what-only-physical-distribution-gives-you)). Additive; no behavior change.
- [ ] Legacy `hermes -z` fallback for nodes without the Runs API ([06 §8](06-agent-traces.md#8-graceful-degradation)).
- [ ] UI: Logs tab Trace/Raw toggle + trace timeline ([09](09-ui-agent-trace-inspector.md)). **Works on the existing pipeline** — immediate value even before the blackboard inversion.

> [!NOTE]
> Phase 1 is independently shippable and reversible. Even if the blackboard inversion stalls, the system gains real traces and working cost tracking — strictly better than today.

### Phase 2 — The board substrate (behind `blackboard_v2`)
- [x] `daemon/src/models/board.py` — the entry envelope model + per-type validation rules ([04 §1](04-blackboard-protocol.md#1-board-entries-typed-envelopes-natural-language-bodies)). *(Implemented as `daemon/src/core/entry.py` + `daemon/src/core/protocol.py`.)*
- [x] `daemon/src/core/gateway.py` — the **Board Gateway**: normalize → validate envelope → capability authorization → commit → emit, plus `remove`/`set_status`/`set_meta` and the pluggable `recompute_derived` hook (salience) ([04 §4](04-blackboard-protocol.md#4-the-board-gateway), [§7](04-blackboard-protocol.md#7-salience-a-cheap-explainable-importance-signal)). **Unit-test with an in-memory fake** (no LLM, no Redis): feed proposals, assert committed/rejected with reasons. *(41 unit tests in `test_gateway.py`.)*
- [x] Define and implement the **event-log durability contract**: task-local monotonic `seq`, SQLite-first write ordering vs the Redis stream, and explicit recovery behavior if one write succeeds and the other fails ([04 §5.1](04-blackboard-protocol.md#51-durability-and-ordering-contract)). `board_events` is the durable source of truth, so these writes cannot be casual best-effort like legacy logs.
- [ ] Rewrite `blackboard.py` to the v2 API ([04 §10](04-blackboard-protocol.md#10-blackboard-api-surface-replaces-ad-hoc-methods)); keep old methods until UI cutover.
- [x] Emit `board_entry` / `entry_removed` / `entry_status_changed` / `entry_rejected` SSE events ([04 §9](04-blackboard-protocol.md#9-new-sse-event-types-additive)).
- [ ] **(Showcase hook) Event log supports fork-from-event, not just linear replay.** Persist stable per-task offsets and a `fork(task_id, at_seq, mutate_fn)` that re-materializes board state up to event *n* ([04 §5.2](04-blackboard-protocol.md#52-fork-from-event-counterfactual-replay)). This is what enables [counterfactual replay](15-novelty-and-research-directions.md#35-causality--replay-enabled-by-event-sourcing) ("suppress agent X's turn, re-run") — design it in now; the linear scrubber later is then trivial.

### Phase 2F — Files & artifacts (parallel-friendly)

The **input half ships standalone** (like Phase 1); the output half needs the Phase-1 agent rewrite.

- [x] Upload pipeline: `POST /tasks/{id}/files` route + UI attach control + validation + PDF extraction (`pymupdf`) + `task_files` rows ([17 §3](17-files-and-artifacts.md#3-the-upload-path)). Storage in `storage.user_media_dir/{task_id}/`. *(21-test smoke suite verifies end-to-end.)*
- [ ] `attachment` board entries at genesis + node-side staging into `/opt/bmas-workspace/{task_id}/inputs/` ([17 §4–5](17-files-and-artifacts.md#4-attachments-on-the-board)). *(Board entry creation wired; genesis-time creation requires Phase 3 CU.)*
- [x] Artifact sync: agent `outputs/` → `POST /ingest/artifacts` → `storage.artifacts_dir/{task_slug}/` + `artifacts` rows + `artifact` board entries ([17 §6](17-files-and-artifacts.md#6-artifacts-agent-created-files)). *(SHA-256 verification, versioning via `.bmas-versions/`, path-traversal rejection, bearer auth.)*
- [x] UI: attachments rail + artifact browser + download routes ([17 §8](17-files-and-artifacts.md#8-ui-surfaces)). *(AttachmentRail, ArtifactBrowser components + Next.js API proxy routes.)*

### Phase 3 — The Control Unit (the `TraditionalVariant`)
- [ ] `daemon/src/core/variants/traditional.py` — `genesis` (triage → AG experts → objective + attachments), `step` (deterministic guards → CU selection call), `finalize` (Decider path / SolE) ([05](05-control-unit.md)).
- [ ] **Author + deploy the role profiles** (planner/expert/critic/conflict_resolver/cleaner/decider, `SOUL.md` + toolset-scoped `config.yaml`) + a `universal` profile (+ optional `coordinator`); replicate to all 3 nodes; add the role→(preferred_host, profile, dispatch_endpoint) registry ([doc 12 §2.5–3](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)).
- [ ] **Verify profile-aware dispatch before relying on profiles.** Live `/v1/runs` has no per-request `profile` field. Pick per-profile gateways/ports, a local `hermes --profile` bridge, or another verified mechanism and record the exact request/command shape in doc 12.
- [ ] New role personas + capability profiles in `personas.py` ([05 §2](05-control-unit.md#2-the-agent-group), [04 §4](04-blackboard-protocol.md#capability-profiles-who-may-write-what)).
- [ ] **Make triage routing real**: pass the triage-selected model (tier pool, [05 §2.1](05-control-unit.md#21-expert-generation-ag-and-model-pool-diversity)) into the run request — today it never reaches the agent ([05 §8](05-control-unit.md#8-migration-of-the-existing-flows)).
- [ ] **Cost + progress rails in the same PR**: budget ceiling, round/duration caps, concurrency cap, the **stall breaker**, and daemon-side decline-gating (deterministic → cheap LiteLLM; never a Hermes run) ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).
- [ ] Raise/restructure timeouts for multi-round execution ([Q5](#3-open-questions-verify-before-building)).
- [ ] Dual-write board→legacy `debate_entries` so the old Blackboard tab keeps rendering.
- [ ] A/B: run `legacy_pipeline` vs `traditional` on identical tasks; compare quality, latency, cost.

### Phase 4 — Blackboard visualization

> [!NOTE] Parallelism constraint
> Phase 4 coding may begin in parallel with Phases 2–3 (it touches `mission-control/` only — different files). However, Phase 4 should **not be verified or merged** until Phase 1 (traces) and Phase 2 (gateway) are merged, because the UI components need real trace data and board entries to render against. Stubbed/mock data is acceptable during development, but the PR's "VERIFY" step must run against a live task with the real trace + board pipeline.

- [ ] Agent-role tokens + deterministic author-color fallback added to `DESIGN.md` + `design-tokens.ts` + `globals.css` ([08 §8](08-ui-blackboard-visualization.md#8-token--primitive-additions-do-this-first)).
- [ ] **Variant plumbing first**: `GET /capabilities` endpoint ([07 §4](07-data-model.md#4-new-rest-endpoints-daemon-routes)), the composer **variant dropdown** (one enabled option in V1), `variant` on `/submit` + task chip, and the **panel-registry skeleton** with the `traditional` adapter ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)). Acceptance: a dummy adapter with one fake panel requires zero edits outside `variants.ts` + the adapter file.
- [ ] `BlackboardGraph`, `WorkerLane`, `ConsensusMeter`; blackboard tab Graph/Stream toggle ([08](08-ui-blackboard-visualization.md)).
- [ ] Turn Inspector slide-over and graph/worker-card cross-links ([09](09-ui-agent-trace-inspector.md)).
- [ ] `useTaskStream` handles new events with batching ([09 §8 note](09-ui-agent-trace-inspector.md#8-files)).
- [ ] Replay scrubber from `board/replay` (linear) — built on the Phase-2 fork-capable event log; the counterfactual-fork UI is a later/optional extension.

### Phase 5 — Advanced & cutover
- [ ] Private sub-boards + Conflict-Resolver ([05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution)).
- [ ] Budget gauge on Cost tab ([09 §5](09-ui-agent-trace-inspector.md#5-cost-integration)).
- [ ] HITL upgrades: directive entries, pause-at-round-boundary, steer (salience boost / retract) ([05 §6](05-control-unit.md#6-hitl-during-the-loop)); Hermes run-approvals inline in the trace UI ([12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)).
- [ ] Flip `coordination.variant` default to `traditional`; deprecate the legacy `debate_entries` dual-write.
- [ ] Mission cockpit view + parallel trace lanes + firehose + convergence strip ([doc 13](13-ui-showcase-density.md)).

### Phase E (cross-cutting — begin at Phase 1) — Evaluation, A/B & showcase instrumentation

> [!NOTE] This is the spine of the "novel artifact" claim. Without it, the novelty is an assertion; with it, it's a result. Build it incrementally alongside the phases above, not at the end.

- [ ] **Benchmark harness** — reproduce the paper's evaluation surface ([doc 15 §3.4](15-novelty-and-research-directions.md#34-llm-mas-scaling--diversity)): a runner that submits a labeled dataset (start with **GSM8K + MMLU subset**, the paper's beds) through bMAS and scores accuracy. Store runs with full config (variant, models, rounds) for comparability.
- [ ] **Metrics capture per run** — accuracy, tokens, **$**, latency, rounds-to-termination, terminated-by breakdown (solution vs SolE vs caps), and `joules_estimate` (from the Phase-1 hook). One row per task; one summary per benchmark. This is the table that goes in the writeup.
- [ ] **A/B harness (variant comparison)** — same dataset, swap only `coordination.variant`: `legacy_pipeline` vs `traditional` now; `patchboard` and `stigmergic` later. Emit a side-by-side report. (The *UI* side-by-side demo — two live variants on one query — is a [doc 13](13-ui-showcase-density.md) surface; this is the data engine behind it.)
- [ ] **(For the robustness experiment) Failure-injection tooling** — a way to drop/partition a node mid-task and record degradation, so the [kill-a-node resilience demo](15-novelty-and-research-directions.md#4-the-distributed-angle-what-only-physical-distribution-gives-you) is repeatable, not a one-off. The hook is V1 (node-health + graceful degradation in the substrate); the *experiment* gets most interesting under the stigmergic variant.

### Phase 6 (post-V1) — The variants
- [ ] **PatchBoard** ([doc 11](11-variant-patchboard.md)): `PatchBoardVariant` against the same engine — Architect genesis, generated schemas, the patch kernel with per-path CAS, dynamic workers, circuit policy; UI adapter (blueprint inspector, transaction log, state-tree view).
- [ ] **Stigmergic** ([doc 16](16-variant-stigmergic.md)): `StigmergicVariant` — pressure field as the derived hook, roleless `universal` actors, decay, threshold self-activation; optional pull-mode via Hermes crons ([12 §6](12-hermes-and-node-topology.md#6-pull-mode-crons-for-the-stigmergic-future)) — **note ([Q9](#3-open-questions-verify-before-building)): crons are CLI/`config.yaml`-managed, not HTTP-managed** (`features.jobs_admin=false`), so the daemon provisions them via SSH/CLI, not an API call.
- [ ] Both run on the *unchanged* gateway/event-log/traces/UI shell **iff** the seams checklist was honored — that is the test of this whole plan.

## 3. Open Questions (verify before building)

> [!WARNING]
> These assumptions gate real engineering time. Verify each on a live node/cluster and record the answer before the dependent phase starts.

| # | Question | Status | Detail |
|:--|:--|:--|:--|
| Q1 | Hermes Runs API exists with `/v1/runs` + `/v1/runs/{id}/events` SSE? | ✅ **Confirmed + LIVE (run submitted 2026-06-08)** | Gateway enabled on all 3 nodes. `/v1/capabilities` booleans live under `features.*`. **Real SSE event names**: `message.delta`, `reasoning.available`, `tool.started`/`tool.completed`, `approval.request`/`responded`, `run.completed`/`failed`/`cancelled` — *not* the OpenAI-style names early drafts assumed ([doc 06 §2](06-agent-traces.md#2-the-enabler-the-hermes-runs-api)) |
| Q2 | Does run completion return populated `usage` / cost? | ✅/⚠️ **Confirmed live, with a caveat** | `usage` returns **token counts** (`input/output/total_tokens`) — but **no cost field** (provider `custom`/`gemini`). **Daemon must compute `cost_usd`** from tokens × price (or LiteLLM cost), not read it from Hermes ([doc 06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)) |
| Q3 | Can Hermes reliably emit the **structured entry envelope** (`entries_v1` JSON) as final output? | ⚠️ **Phase-2/3 gate, with a built-in fallback** | Prompt-test against the live Runs API before the CU depends on clean envelopes. Unlike JSON-Patch, the failure mode is benign: the response contract specifies a deterministic **free-text wrap** (`envelope_fallback: true` → one entry of the role's default type, [04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)), so a sloppy model degrades to paper-style prose instead of a rejected turn. Measure the fallback rate per model; it is also a model-quality signal. |
| Q4 | Rely on gateway rejection vs prompt for authorization? | ✅ **Decided** | Rely on the **gateway** (prompt advisory). Confirm rejection UX ([08 §6](08-ui-blackboard-visualization.md#6-convergence-meter--rejection-overlay)). |
| Q5 | Latency/cost of multi-round cyclic execution vs the 3-call pipeline? | ⚠️ **Measure** | A/B; tune `max_rounds` per triage tier. **Also raise the timeouts:** today's `httpx.AsyncClient(timeout=120)` ([`orchestrator.py`](../daemon/src/core/orchestrator.py)) and `TASK_TIMEOUT_SECONDS=120` ([`agent/api_server.py`](../agent/api_server.py)) bound a *single* call; a 4-round loop with concurrent agents and board reads will exceed that. Switch the agent to a **long-lived SSE consume** of `/v1/runs/{id}/events` (no fixed per-turn wall-clock) and make the daemon's turn timeout a per-turn, not per-task, budget. |
| Q6 | Hermes profiles for the role group? | ✅/⚠️ **Profiles confirmed; dispatch unresolved** | Profiles are fully isolated (own SOUL/config/skills), but none exist yet and live `/v1/runs` has no per-request `profile` selector. Author profiles, then verify a profile-aware dispatch path before relying on them ([doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)). |
| Q7 | `POST /v1/runs/{id}/stop` + `/approval` for HITL? | ✅ **Confirmed in source** | Wire stop = abort, approval = inline operator gate ([doc 12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)). Test live once wired. |
| Q8 | Responses API `previous_response_id` for cross-turn agent memory? | ✅ **Confirmed** | Use `session_id={task}:{role}` for correlation ([doc 12 §5.2](12-hermes-and-node-topology.md#52-stateful-turns-via-the-responses-api)) |
| Q9 | Manage Hermes **crons** over the API server (for stigmergic pull-mode)? | ❌/⚠️ **Do not assume HTTP writes** | `/v1/capabilities.features.jobs_admin=false`. `GET /api/jobs` lists jobs, but create/update/delete are not advertised as supported and must be live-tested before use. Conservative path: create crons via Hermes **CLI / `config.yaml`** on each node ([doc 12 §6](12-hermes-and-node-topology.md#6-pull-mode-crons-for-the-stigmergic-future)). Affects the **stigmergic variant only** — no V1 impact. |
| Q10 | Read/write agent **memory** over the API server (for the UI "minds")? | ❌ **Confirmed NO** | `/v1/capabilities.features.memory_write_api=false` (and `features.admin_config_rw=false`). Display memory via the `:9119` dashboard / CLI; do not plan an HTTP memory-write path from the daemon. |
| Q11 | How do agents receive board content? | ✅ **Decided** | Default: **full board in the payload** (the paper's contract — Cleaner + debate-sized entries keep it tractable). Above `coordination.view_budget_tokens`: **budgeted mode** (full bodies for top-salience entries, index lines for the rest, authenticated pull-by-id endpoint for overflow). All node↔daemon surfaces (board read, trace ingest, file fetch, artifact ingest) authenticated via the shared `BMAS_NODE_KEY` bearer secret. Full contract in [doc 03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target). |
| Q12 | PDF extraction quality (`pymupdf`) on real uploads? | ⚠️ **Test in Phase 2F** | Validate text extraction on representative PDFs (papers, earnings reports, scans). Scanned/image PDFs extract nothing — the attachment entry must say so honestly rather than feeding agents an empty body ([17 §3](17-files-and-artifacts.md#3-the-upload-path)). OCR is explicitly out of scope for V1. |

## 4. Risk register

| Risk | Likelihood | Impact | Mitigation |
|:--|:--|:--|:--|
| Cyclic loop runs up cloud cost | Med | High | Budget ceiling + round/duration caps shipped *with* the loop; **daemon-side** decline gating (the ~16k-token Hermes context floor makes dispatched declines expensive); full-board payload bounded by the Cleaner + budgeted mode ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)) |
| Accepted-but-no-progress livelock (rounds spin below the caps) | Med | Med | Deterministic stall breaker on no-accepted-entries / near-duplicate rounds (`stall_rounds`, [05 §5](05-control-unit.md#5-cost-governance--safety-rails)) — append-only entries make this simple log arithmetic |
| Agents can't emit clean entry envelopes (Q3) | Med | **Low–Med** | The contract's free-text fallback wraps prose into a valid entry (`envelope_fallback`), so the loop never blocks; fallback rate is monitored per model ([04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)) |
| CU selection call returns garbage / drifts | Med | Med | One retry → deterministic fallback table ([05 §1.1](05-control-unit.md#11-the-cu-selection-call-precisely)); selection rationale logged + visible ([05 §1.2](05-control-unit.md#12-the-coordinator-narration-lane-optional-showcase-flourish)) |
| Trace volume bloats SQLite | Med | Med | Cap/TTL Redis streams; sample/summarize `reasoning`/`token_delta`; retention job ([07 §5](07-data-model.md#5-retention--size-control)) |
| `useTaskStream` re-render storm | Med | Med | Batch trace events (rAF/debounce) ([09 §8](09-ui-agent-trace-inspector.md#8-files)) |
| Dashboard breaks during migration | Low | High | Additive schema; dual-write board→legacy debate; new SSE events are additive; everything behind `blackboard_v2` |
| Event-log split-brain (SQLite/Redis divergence) | Low–Med | High | SQLite-first commit ordering + recovery rules defined up front ([04 §5.1](04-blackboard-protocol.md#51-durability-and-ordering-contract)); replay from SQLite is always authoritative |
| File-pipeline abuse / disk fill | Low–Med | Med | Size caps + type allowlist + per-task quotas; artifact sync rejects path traversal; retention policy ([17 §7](17-files-and-artifacts.md#7-limits-security-and-retention)) |
| Profile dispatch assumption is wrong | Med | High | Verify profile-aware dispatch in Phase 3 before depending on SOUL/toolset isolation; fall back to `instructions` identity only if profile isolation is explicitly deferred |
| Quality regresses vs pipeline | Low | Med | A/B `legacy_pipeline` flag; keep escape hatch through Phase 5 |

## 5. Backward-compatibility contract

- **SQLite**: additive only (new tables, `ADD COLUMN`). Existing queries untouched.
- **SSE**: new event *names*; `routes/events.py` forwards any `{event,data}` unchanged ([04 §9](04-blackboard-protocol.md#9-new-sse-event-types-additive)). Legacy `debate`/`phase`/`subtask`/`cost`/`complete` keep firing through Phase 4.
- **UI**: new components live behind toggles inside existing tabs; the current `DebateList`/`DAGVisualizer`/`TaskLogTerminal` keep working until explicitly replaced.
- **Config**: new keys validated fail-fast (matching `config.py` style); `coordination.variant: legacy_pipeline` reproduces today's behavior exactly.

## 6. Definition of done (maps to README §4)

- [ ] An agent reads a peer's board entry and posts a critique **without daemon instruction** (Phase 3).
- [ ] ≥2 agents contribute concurrently to one task; the graph shows interleaving (Phase 3/4).
- [ ] The loop halts via the Decider or SolE, not a fixed pipeline end; the convergence meter reflects progress honestly (Phase 3/4).
- [ ] Opening a running task shows live reasoning + tool calls + token deltas (Phase 1).
- [ ] Blackboard tab renders a live graph animating as entries land — including the Cleaner visibly removing entries (Phase 4).
- [ ] Daemon restart mid-task replays the board from `board_events` without corruption (Phase 2).
- [ ] Per-task cost is real and bounded by `budget_ceiling_usd` (Phase 1 + 3).
- [ ] A user attaches a PDF at submit; agents cite its content; the attachment renders on the board (Phase 2F).
- [ ] A "write me a codebase" task materializes files under `{storage.artifacts_dir}/{task_slug}/`, browsable and downloadable in the UI (Phase 2F).
- [ ] The composer shows the variant dropdown (one enabled option), `variant` persists on the task, and a dummy UI adapter mounts with zero shell edits (Phase 4).

## 7. Estimated effort (rough, for sequencing — not a commitment)

| Phase | Surface | Relative size |
|:--|:--|:--|
| 0 | schema/config scaffolding | S |
| 1 | agent traces + cost + Logs UI | M |
| 2 | gateway + board model + event log + tests | L |
| 2F | files & artifacts pipeline | M |
| 3 | traditional variant + roles + cost rails | L |
| 4 | blackboard graph + worker lane + variant plumbing | M–L |
| 5 | private boards, HITL, cutover, cockpit | M |

> [!TIP]
> Ship **Phase 1 first and independently**. It de-risks the Hermes integration (Q1–Q2), delivers immediate operator value (real traces, working cost), and produces the data the later visualization phases need — all without committing to the architectural inversion. If Phase 1 reveals the Runs API isn't viable, you learn it cheaply, before building the gateway and CU. The upload half of **Phase 2F** is the second-best standalone slice — PDF input works even on the legacy pipeline (extracted text appended to the prompt) and the storage plumbing carries forward unchanged.
