[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Extensibility & Variants](11-extensibility-and-variants.md) | [➡️ Next: UI Showcase Density](13-ui-showcase-density.md) | [📡 Hermes API](../HERMES_API.md)

# 12 — Hermes Integration & Node Topology

> [!ABSTRACT]
> bMAS runs on Hermes, but currently uses ~5% of what Hermes offers. This document (a) records the **verified live state** of the cluster, (b) answers the **"paper agents on 3 hosts via profiles"** question, (c) specifies a **per-role SOUL.md** identity model, and (d) maps every relevant Hermes feature onto a concrete bMAS need so the system becomes full-featured rather than a thin `hermes -z` wrapper.

---

## 1. Verified live state (inspected 2026-06-06)

Pulled directly from **all three agent nodes** (`.103`, `.112`, `.122`) — the control plane can SSH to every node. **All three are byte-for-byte identical** at the Hermes level (same SOUL, same 24 skills, no profiles, empty memory, gateway off). There is *no* per-agent customization on disk; the only thing that differs per node is `NODE_ID` and the `role` in `bmas.yaml`.

| Fact | Finding | Implication |
|:--|:--|:--|
| Hermes version | **v0.15.1** (2026.5.29) | [HERMES_API.md](../HERMES_API.md) documented v0.13.0 — **stale**, now refreshed |
| Listening ports | Now `:8000` (bMAS `hermes -z` bridge) **+ `:8642` Runs API + `:9119` dashboard** | All three healthy as of 2026-06-07 — the Runs API gateway was stood up and the dashboard restarted ([§4](#4-enabling-the-runs-api-the-phase-1-unblocker--done-on-all-3-nodes-2026-06-07)). Originally only `:8000` was up. |
| Deployed bridge | the `hermes -z` one-shot version | The [trace gap](06-agent-traces.md#1-the-root-cause-precisely) is live in production, not just in the repo |
| Profiles | `~/.hermes/profiles/` **does not exist** | Single default profile per node. No role isolation today |
| SOUL.md | one generic `~/.hermes/SOUL.md` ("Distributed Agent Node") | **Identical, role-agnostic identity** on every node. Roles exist only as per-task `AGENTS.md` |
| Skills | 24 installed | Procedural memory exists but is unused by bMAS |
| Crons | present (`~/.hermes/cron/`) | Pull-mode self-activation is feasible later |
| Model | provider `custom`/`gemini`; browser `camofox`; compression `auto` | Tooling (web/browser) is configured and available |

> [!IMPORTANT] Update — the Phase-1 unblocker is cleared
> The Hermes gateway was originally **not enabled** (only `:8000` was up), which made traces impossible regardless of daemon code. As of **2026-06-07 this is fixed**: the Runs API gateway is now installed as a boot-persistent system service on all three nodes ([§4](#4-enabling-the-runs-api-the-phase-1-unblocker--done-on-all-3-nodes-2026-06-07)), and the `:9119` dashboard (which had been left stopped after the v0.15.1 update) was restarted. Phase 1 can now proceed against a real Runs API.

## 2. Agents, personas, and nodes — clearing up the count

> [!IMPORTANT] The conceptual fix: an "agent" in the paper is a *persona*, not a machine
> This is the crux of the confusion. In [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701) an **agent = (a role prompt + an LLM) that reads and writes the blackboard**. It is purely logical — the paper has **no notion of physical hosts or machines at all**. Every agent is just an LLM API call with a particular persona.

What the paper (LbMAS) actually runs — verified against the [arXiv text](https://arxiv.org/abs/2507.01701):

- **5 *constant* agents** (always available): **planner, decider, critic, conflict-resolver, cleaner**.
- **`n` *query-related expert* agents**, **dynamically generated per query** by an "agent-generating agent" (AG) — the count and identities depend on the question, not a fixed number.
- **1 control unit** — itself an LLM — that, each round, **selects a *subset* of the available agents to act**. The number of agents acting in a given round is dynamic (could be one, could be several).
- Base LLMs are **chosen randomly from a pool** per agent (they used Llama-3.1-70B + Qwen-2.5-72B) for model diversity. Max cycle = 4 rounds; stop when the decider is satisfied or rounds run out.

So **there is no fixed "6 agents."** It's 5 constant roles + a variable number of experts + a control unit, and only a subset runs each round. (Our earlier "6 roles" shorthand was a simplification — treat the *role set* as the constant 5 + experts.)

### How your 3 nodes map onto that

Your three Hermes hosts are **not "3 agents."** They are **3 units of compute** — the substrate that *executes* whatever agent/persona is assigned at a given moment. Two important consequences:

1. **Today's setup collapses the paper.** bMAS currently pins node-1→Planner, node-2→Executor, node-3→Auditor — a *static* 1:1 host↔role mapping. That's the orchestrator-worker flaw ([doc 01](01-gap-analysis.md)). It even renamed the paper's roles: the paper has **no "executor" or "auditor"** — it has decider/critic/conflict-resolver/cleaner + experts.
2. **The fix decouples personas from hosts.** Each node carries the *full persona library* (as Hermes profiles); the control unit assigns a *role* to whichever host is free; experts are spun up on demand (ideally one per node for parallelism). The number of logical agents per task is dynamic and **independent of the fact that you have 3 boxes** — because the heavy LLM inference is remote (LiteLLM) anyway, a single host can embody several personas over a task's life.

In short: **profiles = personas = the paper's "agents"; nodes = compute.** That mental model resolves the "3 vs 6" tension entirely.

## 2.5. The "agents-on-3-hosts" answer: yes, via profiles

Hermes **profiles** implement the persona library cleanly. From the [official docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration): *"Each profile is a fully isolated Hermes instance with its own config, memory, skills, sessions, and SOUL.md. They share nothing by default."*

A profile is selected per invocation/gateway with `hermes --profile <name>`. So a single host can present **multiple distinct agent identities**.

> [!WARNING] Runs API profile selection is not per request (verified live 2026-06-08)
> The current `/v1/runs` handler accepts `input`, `instructions`, `conversation_history`, `previous_response_id`, `session_id`, and `model`; it has **no verified `profile` field**. The gateway process is scoped to its active Hermes profile. Therefore the design cannot assume one `:8642` gateway multiplexes every role profile on a host. V1 must choose and verify one of these dispatch mechanisms before profile-dependent work starts:
>
> - **Per-profile gateways:** run one gateway per profile on distinct ports, e.g. `planner:8642`, `critic:8643`, with the role registry targeting `(host, gateway_port)`.
> - **Profile-aware bMAS bridge:** keep a local node service that calls `hermes --profile <role> ...` or an equivalent profile-scoped API internally, while still translating Runs events when available.
> - **Upstream profile selector:** only if a future Hermes version exposes and verifies a per-request profile selector.
>
> Until this is resolved, role identity can still be injected through `instructions`/per-turn `AGENTS.md`, but that does **not** provide profile-scoped SOUL, memory, or toolset isolation.

### Recommended mapping: replicate all profiles to every node, assign a "home" per role

The key realization: **an agent LXC does almost no heavy compute.** The actual LLM inference is remote (every agent routes through LiteLLM at `:4000` → edge/cloud models). The Hermes process on the node only runs the agent runtime + tool calls. So **profile placement is not primarily a capacity-balancing problem** — once a profile-aware dispatch mechanism exists, a single host can run several role-profile turns comfortably. That changes the recommendation:

```
   profile library (identical on every node — profiles are just files):
   planner · expert · critic · conflict_resolver · cleaner · decider · universal
            │                    │                    │
   ┌──── node-1 (.103) ────┐ ┌── node-2 (.112) ──┐ ┌── node-3 (.122) ──┐
   │ home: planner, cleaner│ │ home: decider     │ │ home: conflict_res│
   │ + any role on demand  │ │ + any role        │ │ + any role        │
   └───────────────────────┘ └───────────────────┘ └───────────────────┘
        dispatch target must be profile-aware (per-profile gateway/port or bridge)
```

- **Install the full profile set on all 3 nodes.** Profiles are just `SOUL.md` + `config.yaml` + `skills/` directories — cheap to replicate (deploy like `api_server.py`). This directly realizes the paper's **"any node can assume any role"**: the dispatcher can pick whichever host is least busy and target the right `profile`.
- **This is what makes the parallel "discovery" / "debate" phases fast.** When the loop wants 3 experts at once, it targets the `expert` profile on node-1, node-2, *and* node-3 simultaneously through the verified profile-aware dispatch mechanism — true parallelism, one per host. Co-locating roles on a single host (the old even-split sketch) would have serialized them.
- **Assign a "home" node per singleton role** *only for skill/memory locality* — the decider's learned skills accumulate on node-2, the conflict-resolver's on node-3, etc. The `role → (preferred_host, profile, dispatch_endpoint)` registry encodes the home but allows fallback to any host. This keeps each role's procedural memory (`skills/`, `MEMORY.md`) coherent over time.
- **Experts share one `expert` profile, not three.** Experts are generated per task (domain, specialty). Give them a single neutral `expert` profile (full toolset) and inject the domain identity via the per-task `AGENTS.md` — the runtime-injection mechanism that *already works today*. No need for `expert_security`, `expert_perf`, … profiles.

So the profile set is **7**: `planner`, `expert`, `critic`, `conflict_resolver`, `cleaner`, `decider`, and `universal` (V2). The CU itself is **not** in this list — see [§2.1](#21-should-the-control-unit-be-a-profile-mostly-no).

> [!NOTE] Where does expert generation live? (the paper's "AG" function)
> The paper uses an **agent-generating agent (AG)** that dynamically creates expert personas per query. In our architecture, this function lives in the **daemon/CU scheduler** as a direct LiteLLM call (currently [`orchestrator._complex_research_flow` L369–393](../daemon/src/core/orchestrator.py)), **not** as a Hermes profile. The AG is a control-plane function (it decides *which* experts to spawn), not a knowledge-work agent — the same rationale as [§2.1](#21-should-the-control-unit-be-a-profile-mostly-no). The generated expert identities are injected into the shared `expert` profile via per-task `AGENTS.md`, which is the mechanism that already works today.

> [!NOTE] Toolset isolation per role (a real win)
> Profiles let you scope tools per role, which the [agent-integration roadmap](../roadmap/agent-integration.md) explicitly wants: give the **expert** profile `web` + `browser` + `code_exec`; give the **critic/decider** read-only/analysis tools; give **cleaner** no external tools. Enforced by each profile's `config.yaml.toolsets`, not by prompt.
>
> For the **stigmergic V2** ([doc 11](11-extensibility-and-variants.md)), the `universal` profile carries the full toolset and a roleless SOUL. V2 runs N copies of `universal`; V1 ignores it.

### 2.1 Should the Control Unit be a profile? (mostly: no)

Split the Control Unit into its two sub-functions, because they live in different places:

| CU sub-function | What it is | Where it belongs | Profile? |
|:--|:--|:--|:--|
| **Scheduler / OODA loop** | Deterministic-first control: who acts next, when to stop, cost gating, consensus *mechanics* ([05](05-control-unit.md)) | **Daemon (Python)** — it's orchestration, not knowledge work, and most of it isn't even an LLM call | **No.** It is the `ControlUnitStrategy`, not a Hermes agent. |
| **The Decider** | An LLM that reads the board and renders the *consensus / sufficiency judgment* | A **Hermes profile** — it's a genuine reasoning agent that reads the board, exactly like the other roles | **Yes** — it's the `decider` profile in the set above. |

Rationale: making the *scheduler* a Hermes profile would be an architectural mistake — you'd be sending the system's control plane out over the network to a node, losing the deterministic, observable, fail-fast properties that the whole [target architecture](03-target-architecture.md) depends on. The scheduler stays in the daemon where it can be unit-tested and where the [kernel](04-blackboard-protocol.md) lives. Only the *judgment* calls (Decider) and the occasional **LLM escalation** (the rare "the heuristics are ambiguous, ask a model which region is hottest" call) are LLM work.

> [!TIP] Optional showcase flourish — the `coordinator` profile (8th profile)
> If you want the scheduler's LLM-escalation calls to appear as a visible actor in the [trace UI](09-ui-agent-trace-inspector.md) (a "Coordinator" lane narrating *why* it picked the next agent), route those specific calls through a thin `coordinator` profile instead of a bare LiteLLM call. This is now spec'd in full — including the hard constraints (off the critical path, escalation-only, flag-gated, not the scheduler) — in **[doc 05 §1.1](05-control-unit.md#11-the-coordinator-narration-agent-optional-showcase-flourish)**. It adds an optional 8th profile to the set in [§2](#2-agents-personas-and-nodes--clearing-up-the-count); replicate it like the others.

## 3. SOUL.md per role (replace the single generic soul)

Today every node has the same generic SOUL ([§1](#1-verified-live-state-inspected-2026-06-06)). Move to **one SOUL.md per profile**, encoding durable role identity — distinct from the per-task `AGENTS.md` (operational instructions for *this* task). This matches the file-responsibility split confirmed in the web research (SOUL = persona/boundaries; AGENTS = operational rules).

| Layer | File | Scope | Example content |
|:--|:--|:--|:--|
| Identity (durable) | profile `SOUL.md` | who this agent *is* | "You are **the Critic**. Your purpose is to find errors, weak evidence, and hallucinations. You are adversarial but fair. You never propose solutions — only critiques." |
| Operation (per task) | per-turn `AGENTS.md` | what to do *now* | objective, phase, board index, the patch schema the kernel will accept this turn ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)) |
| Capability | profile `config.yaml` | what tools/model | toolsets, model affinity ([§2.5](#25-the-agents-on-3-hosts-answer-yes-via-profiles)) |

Keep the existing generic SOUL as the base for a `universal` profile (V2). Author the 6 role SOULs in-repo (e.g. `agent/profiles/{role}/SOUL.md`) and deploy them.

## 4. Enabling the Runs API (the Phase-1 unblocker) — ✅ DONE on all 3 nodes (2026-06-07)

> [!IMPORTANT] Mechanism correction (verified live)
> The Runs API is **not** a separate command and `hermes gateway` takes **no** `--host`/`--port` flags (those belong to `hermes dashboard`). The OpenAI-compatible **API server** — which serves `/v1/runs*`, `/v1/responses`, `/v1/chat/completions` — is a *platform hosted inside the messaging gateway process*, gated by an env flag. You enable it in `.env` and then run/install `hermes gateway`. This was confirmed by inspecting the live binary (`gateway/platforms/api_server.py`) and the [official API-server docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server).

The exact steps that were run (and that worked) on each of `.103/.112/.122`:

```bash
# 1) ~/.hermes/.env  — enable the API server (the Runs API lives here)
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0           # LAN-reachable from the control plane
API_SERVER_PORT=8642
API_SERVER_KEY=<shared secret>    # mandatory even for local binds; bMAS daemon presents this as a bearer token
# API_SERVER_CORS_ORIGINS left unset — bMAS proxies server-side, no browser CORS needed

# 2) Install + start the gateway as a boot-persistent system service.
#    NOTE the --run-as-user root flag: the agent LXCs run as root and the
#    installer refuses a root system-service without it.
hermes gateway install --system --force --run-as-user root   # answer "y" to both prompts
```

This writes `/etc/systemd/system/hermes-gateway.service` (Hermes-generated, not hand-written), enables it, and starts it. The gateway process now hosts the API server **alongside** the existing bMAS `:8000` bridge and the `:9119` dashboard — three independent services per node.

**Verified live result** (`curl -H "Authorization: Bearer $KEY" http://<node>:8642/v1/capabilities`, re-confirmed 2026-06-08 on all 3 nodes) — every capability *flag* bMAS depends on is `true` under the nested `features` object:

```
run_submission ✓  run_status ✓  run_events_sse ✓  run_stop ✓  run_approval_response ✓
tool_progress_events ✓  approval_events ✓  responses_api ✓  responses_streaming ✓
session_fork ✓  skills_api ✓     # but: admin_config_rw ✗  memory_write_api ✗  jobs_admin ✗  cors ✗
```

> [!WARNING] Capability flags confirm *endpoints exist* — not *payload shape*. Two things were checked separately by submitting a real run (do not skip these when building Phase 1):
> 1. **Event names.** The SSE stream emits `message.delta`, `reasoning.available`, `tool.started`, `tool.completed`, `approval.request`/`approval.responded`, `run.completed`/`run.failed`/`run.cancelled` — **not** the OpenAI-style names (`hermes.tool.progress`, `chat.completion.chunk`, …) that early drafts assumed. The `translate()` map is in [doc 06 §2](06-agent-traces.md#2-the-enabler-the-hermes-runs-api).
> 2. **`usage` has tokens but no cost.** `run.completed.usage` = `{input_tokens, output_tokens, total_tokens}` only. Dollar cost is computed daemon-side ([doc 06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema)). This closes Q2 (usage is populated) while sharpening it (cost is not).

> [!NOTE] Capability gaps to design around
> `features.jobs_admin: false` and `features.memory_write_api: false` mean cron admin writes and memory writes are not advertised as supported over the API server. `GET /api/jobs` currently lists jobs, but create/update/delete must not be assumed until live-tested. The [V2 pull-mode crons](#6-pull-mode-crons-for-the-stigmergic-future) must therefore be created via the Hermes **CLI/`config.yaml`** on each node (or by enabling and verifying those APIs), not via an unverified HTTP call from the daemon. Plan accordingly — it does not block V1.

> [!WARNING] CORS bug — irrelevant to bMAS
> A known Hermes bug omitted CORS headers on `/v1/runs/{id}/events`. **This does not affect bMAS** because the daemon consumes the SSE stream server-side, never from the browser (we leave `API_SERVER_CORS_ORIGINS` unset). v0.15.1 is recent enough regardless.

## 5. Hermes feature → bMAS need (leverage the whole API)

The system should use these, mapped to specific docs:

| Hermes feature | Endpoint / file | bMAS use | Doc |
|:--|:--|:--|:--|
| **Runs API** | `POST /v1/runs`, `GET /v1/runs/{id}/events` (SSE) | dispatch + **live traces** (`message.delta`, `reasoning.available`, `tool.started`/`tool.completed`) | [06](06-agent-traces.md) |
| **Run stop** | `POST /v1/runs/{id}/stop` | HITL abort mid-turn (replaces `proc.kill()`) | [05 §6](05-control-unit.md#6-hitl-during-the-loop) |
| **Run approval** | `POST /v1/runs/{id}/approval` | **native HITL gate**: agent pauses on a risky action, operator approves in Mission Control | new ([§5.1](#51-native-hitl-via-run-approvals)) |
| **Responses API** | `POST /v1/responses` (stateful, `previous_response_id`/`conversation_history`) | give an agent **memory across rounds** of the same task without re-stuffing the board each turn | [06](06-agent-traces.md), [§5.2](#52-stateful-turns-via-the-responses-api) |
| **Profiles** | `/api/profiles`, `--profile`; profile-scoped gateway/bridge TBD | the **role personas** (5 constant + experts + `universal`) on 3 hosts | [§2.5](#25-the-agents-on-3-hosts-answer-yes-via-profiles) |
| **Skills** | `/api/skills` | shared procedural memory; show which skills shaped a task | [agent-integration roadmap](../roadmap/agent-integration.md) |
| **Memory** | `/api/memory` (MEMORY.md/USER.md) | display each agent's learned state in the UI; debug stale behavior | [13](13-ui-showcase-density.md) |
| **Crons** | CLI/`config.yaml` for writes; `GET /api/jobs` only verified for listing | **pull-mode self-activation** for the stigmergic variant | [11 §4](11-extensibility-and-variants.md#4-the-stigmergic-variant-specified) |
| **Analytics** | `/api/analytics/usage`, `/models` | per-node token/cost truth alongside bMAS task cost | [09 §5](09-ui-agent-trace-inspector.md#5-cost-integration) |
| **Sessions** | `/api/sessions/search` | search what each agent did, in/out of bMAS tasks | [13](13-ui-showcase-density.md) |
| **Toolsets** | `/api/tools/toolsets` | show per-role capabilities; verify isolation | [13](13-ui-showcase-density.md) |
| **Events WS** | `WS /api/events` | alternative live trace transport if SSE proves limiting | [06 §3](06-agent-traces.md#3-rearchitected-agent-server) |

### 5.1 Native HITL via run approvals

Hermes can pause a run pending operator approval (`POST /v1/runs/{id}/approval`, surfaced as `approval_events` in the stream). This is strictly better than today's pause-flag-read-on-resume model: an agent about to run a destructive command or an expensive search **blocks** until the operator approves *in Mission Control*. Wire approval events into the trace stream as a `trace` event of type `approval_request`; render an inline Approve/Deny in the [trace inspector](09-ui-agent-trace-inspector.md). This makes the showcase visibly interactive ("watch me approve the agent's web search").

### 5.2 Stateful turns via the Responses API

A multi-round blackboard task makes the *same* agent act several times. Re-sending the whole board each turn is token-expensive. The Responses API (`previous_response_id`) lets an agent retain its own working memory across its turns while still reading the *fresh* board index each time. Use `session_id = "{task_id}:{role}"` so Hermes correlates runs and bMAS can map them back ([HERMES_API.md](../HERMES_API.md) confirms `session_id` surfaces in run status). This directly implements the [indexed-memory hygiene](02-peer-review.md#27210-hermes-native-features--verify-first) recommendation — pass the index, not the whole board.

## 6. Pull-mode crons for the stigmergic future

For V2 ([doc 11](11-extensibility-and-variants.md)), each node runs a Hermes cron that polls `bmas:board:{task}:pressure` and self-activates when a region exceeds the node's activation threshold. Because `features.jobs_admin=false`, the daemon should **not** assume it can create these via `POST /api/jobs`/`POST /api/cron/jobs`. Provision them through SSH + Hermes CLI/`config.yaml`, or first live-test and document a supported job-admin API. **Not built in V1** — but the pressure field and profile-aware dispatch groundwork here are exactly what make it a config flip later.

## 7. Action items for the cluster (concrete)

- [x] Bump [HERMES_API.md](../HERMES_API.md) to v0.15.1 and keep it synced to the live version.
- [x] **Enable the Runs API on all 3 nodes** — `API_SERVER_*` in `.env` + `hermes gateway install --system --run-as-user root`. Done & boot-persistent 2026-06-07 ([§4](#4-enabling-the-runs-api-the-phase-1-unblocker--done-on-all-3-nodes-2026-06-07)).
- [x] **Restart the `:9119` dashboard on all 3 nodes** (it had been left stopped after the v0.15.1 update; the CLI `--insecure` flag still works).
- [ ] Author the profile set (`planner`, `expert`, `critic`, `conflict_resolver`, `cleaner`, `decider`, `universal`) — `SOUL.md` + `config.yaml` toolset scoping; commit to `agent/profiles/` and **replicate to all 3 nodes**. (The CU scheduler is *not* a profile — [§2.1](#21-should-the-control-unit-be-a-profile-mostly-no).)
- [ ] Choose and verify the profile-aware dispatch mechanism: per-profile gateways/ports, a local `hermes --profile` bridge, or a future verified per-request selector. Record the exact command/API shape in this doc before Phase 3b.
- [ ] Add a `role → (preferred_host, profile, dispatch_endpoint)` registry to `bmas.yaml` / `config.py` with home assignments + any-host fallback; teach dispatch to target the verified endpoint and load-balance experts one-per-host.
- [ ] Keep the `hermes -z` bridge as the documented fallback ([06 §8](06-agent-traces.md#8-graceful-degradation)).

➡️ Continue to [13 — UI Showcase Density](13-ui-showcase-density.md).
