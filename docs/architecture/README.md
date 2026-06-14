[🏠 Index](../README.md) | [📋 Roadmap](../roadmap/README.md) | [🎨 Design System](../design/DESIGN.md)

# bMAS — System Architecture

> [!NOTE]
> This document describes the **current** architecture as implemented in the traditional coordination variant. For future variants (stigmergic, patchboard) and the bMAS paper's full vision, see the [Roadmap](../roadmap/README.md).

## 1. Overview

bMAS (Blackboard Multi-Agent System) is a distributed AI swarm that coordinates multiple LLM-powered agents through a shared blackboard. It implements the architecture proposed in [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701), where agents coordinate through shared environmental signals rather than direct communication — a pattern called [stigmergy](https://en.wikipedia.org/wiki/Stigmergy).

The system uses a **cyclic execution model**: a Control Unit (CU) reads the board state, selects which agents to activate, those agents read the board and write new entries, and the cycle repeats until convergence. This contrasts with fixed pipelines — the CU dynamically determines the best agents per round based on what's on the board.

### 1.1 Design Philosophy

| Principle | Implementation |
|:---|:---|
| **Cyclic blackboard protocol** | CU → agent selection → board read/write → convergence check → repeat |
| **Dynamic agent roles** | 7 role profiles (planner, expert, critic, conflict_resolver, cleaner, decider, universal) activated per-round |
| **Single config file** | All deployment topology, model routing, variant settings, and node configuration in `bmas.yaml` |
| **Docker-first** | Control plane runs as 5 Docker containers via `docker-compose.yml` |
| **Cost-optimized** | Local triage classifier routes tasks to the cheapest capable model; budget ceilings halt runaway spend |
| **Dual-write persistence** | Every event writes to Redis (real-time) AND SQLite (permanent history) |
| **Fail-open** | Triage, cost tracking, and logging are best-effort — failures never block task execution |

---

## 2. System Topology

```
┌─────────────────────── Control Plane (Docker Compose) ───────────────────────┐
│                                                                               │
│  ┌───────────┐  ┌────────────┐  ┌───────────┐  ┌──────────┐  ┌───────────┐  │
│  │   Redis   │  │  LiteLLM   │  │  Triage   │  │  Daemon  │  │ Mission   │  │
│  │ Blackboard│  │  Gateway   │  │ Classifier│  │  Orch.   │  │ Control   │  │
│  │   :6379   │  │   :4000    │  │   :8001   │  │  :9000   │  │  :9321    │  │
│  │           │  │            │  │  (GPU)    │  │          │  │           │  │
│  └─────┬─────┘  └──────┬─────┘  └─────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│        │               │              │             │              │         │
└────────┼───────────────┼──────────────┼─────────────┼──────────────┼─────────┘
         │               │              │             │              │
         │        ┌──────┴──────┐       │      ┌──────┴──────┐       │
         │        │ Cloud APIs  │       │      │ Agent Nodes │       │
         │        │ Gemini/etc  │       │      │  (×3 LXCs)  │       │
         │        └─────────────┘       │      └──────┬──────┘       │
         │                              │             │              │
         │                              │      ┌──────┴──────┐       │
         │                              │      │ Edge Infra  │       │
         │                              │      │ llama.cpp   │       │
         │                              │      └─────────────┘       │
         │                              │                            │
         │                        ┌─────┴──────┐              ┌──────┴──────┐
         │                        │ Beszel Hub │              │  Browser    │
         │                        │   :8090    │              │  (React 19) │
         │                        └────────────┘              └─────────────┘
```

| Zone | What runs here | Network |
|:---|:---|:---|
| **Control Plane** | 5 Docker containers (Redis, LiteLLM, Triage, Daemon, Dashboard) | Single host, bridged Docker network `bmas` |
| **Agent Nodes** | Proxmox LXCs running Hermes Agent API + optional local inference | LAN — each node exposes `:8000` to the control plane |
| **Cloud APIs** | Gemini, Claude, OpenAI (via LiteLLM proxy) | Internet — authenticated through LiteLLM |

---

## 3. Component Architecture

### 3.1 Daemon — Orchestrator (`daemon/`)

**The brain.** A Python FastAPI service that manages the complete task lifecycle through cyclic blackboard execution.

| Aspect | Detail |
|:---|:---|
| **Language** | Python 3.13+ |
| **Framework** | FastAPI + Uvicorn |
| **Port** | 9000 |
| **Persistence** | Dual-write: Redis (real-time) + SQLite v2 schema via aiosqlite (permanent) |
| **Config** | Reads `bmas.yaml` at startup via `config.py` |

#### Internal Structure

```
daemon/src/
├── app.py                     # FastAPI entry point + lifespan
├── config.py                  # YAML config loader → module-level constants
├── database.py                # SQLite v2 schema (12 tables, additive migrations)
├── auth.py                    # Bearer token auth for agent ingest
├── file_utils.py              # File upload handling + PDF extraction
├── core/
│   ├── orchestrator.py        # Task lifecycle: triage → CU → agents → converge
│   ├── blackboard.py          # Redis state + durable board snapshots (no TTL)
│   ├── board_store.py         # Event-sourced board (entries + events, deterministic replay)
│   ├── gateway.py             # Capability-gated agent dispatch with per-task locking
│   ├── entry.py               # Board entry schema + validation
│   ├── protocol.py            # Agent ↔ Daemon message protocol
│   ├── salience.py            # Entry salience scoring
│   ├── event_emitter.py       # SSE event abstraction (Redis Pub/Sub + in-memory)
│   ├── capabilities.py        # Agent capability registry
│   ├── log_levels.py          # Level normalization (INF→info, WRN→warning, etc.)
│   ├── triage.py              # Complexity classifier client (calls vLLM)
│   └── variants/
│       └── traditional.py     # Cyclic CU → agent selection → board → convergence loop
├── models/
│   └── personas.py            # Agent role definitions + dynamic expert persona generator
├── monitoring/
│   └── health_loop.py         # Background agent health polling → Redis
└── routes/
    ├── submit.py              # POST /submit — async task submission (HTTP 202)
    ├── tasks.py               # GET /tasks, /tasks/{id}/* — task/board/cost/log/trace/turns
    ├── events.py              # GET /events/{id}, /events/system — SSE via Redis Pub/Sub
    ├── ingest.py              # POST /ingest/traces, /ingest/logs — bearer-auth'd agent ingest
    ├── artifacts.py           # Artifact ingest + retrieval
    ├── files.py               # File upload + download + text extraction
    ├── hitl.py                # HITL: pause/resume/directive/steer/approval
    └── health.py              # GET /health, /state — dependency health + blackboard snapshot
```

#### Key Design Decisions

- **Cyclic execution**: The traditional variant implements the paper's CU-driven loop. Each round, the CU reads the full board and selects agents with rationale. Agents execute concurrently, write entries to the board, and the cycle repeats until convergence (Decider says done, budget exceeded, or max rounds reached).
- **Event-sourced board**: `board_store.py` maintains an append-only event log. Board state is derived by replaying events, enabling deterministic snapshots and fork support.
- **Dual-write pattern**: Every lifecycle event writes to both Redis (for live SSE streaming) and SQLite (for permanent task history). SQLite writes are best-effort — they log warnings but never interrupt a running task.
- **Lock-per-task**: Each task acquires a Redlock on `orchestrator:{task_id}`, allowing concurrent task execution while preventing duplicate processing.
- **Capability-gated dispatch**: `gateway.py` checks agent capabilities before dispatch, ensuring only capable agents receive tasks.
- **Budget ceiling**: The traditional variant tracks cumulative LLM spend and halts execution when `budget_ceiling_usd` is exceeded.

---

### 3.2 Agent API Server (`agent/`)

**The hands.** A FastAPI server deployed to each edge node (Proxmox LXC), bridging the Daemon to [Hermes](https://github.com/hypermodeinc/hermes) agents.

| Aspect | Detail |
|:---|:---|
| **Language** | Python 3.13+ |
| **Framework** | FastAPI |
| **Port** | 8000 (per node) |
| **Deployment** | Copied to `/opt/bmas/api_server.py` on each LXC, runs as systemd service |

#### Execution Paths

1. **Runs API (primary)** — When `HERMES_GATEWAY_URL` is set, the agent uses the Hermes Gateway's `POST /v1/runs` endpoint with SSE streaming. Real-time trace/log data flows back to the daemon via `TraceEmitter` and `LogEmitter`.
2. **CLI fallback** — When no gateway is configured, falls back to `hermes -z` subprocess execution.

#### Key Features

- **TraceEmitter** — Batches and POSTs structured agent traces (tool calls, content blocks) to `/ingest/traces/{task_id}/{turn_id}` on the daemon
- **LogEmitter** — Ships structured per-agent log entries (with fields, node ID, turn ID) to `/ingest/logs/{task_id}` on the daemon
- **SSE parser** — Handles both standard and Hermes Gateway SSE formats
- **Profile support** — 7 Hermes profiles (planner, expert, critic, conflict_resolver, cleaner, decider, universal) with per-role toolset scoping

---

### 3.3 Redis — Blackboard (`redis/`)

**The shared memory.** Redis serves as the blackboard — the central knowledge store through which all agents coordinate.

| Aspect | Detail |
|:---|:---|
| **Image** | `redis:7-alpine` |
| **Port** | 6379 |
| **Memory** | 1 GB `maxmemory` (2 GB container limit) |
| **Persistence** | RDB snapshots every 60s if ≥100 keys changed |
| **Eviction** | `volatile-lru` — evicts least-recently-used keys with TTL first |

#### Namespace Schema

| Namespace | Key Pattern | Data Type | Purpose |
|:---|:---|:---|:---|
| **Public State** | `bmas:public:state` | Hash | Orchestrator phase, iteration count, pause flag |
| **Public Tasks** | `bmas:public:tasks` | Hash | Task registry — maps task IDs to JSON objects |
| **Public Results** | `bmas:public:results` | Hash | Consensus results — written by Decider |
| **Board Entries** | `bmas:board:{task}:entries` | Hash | Per-task board entries (entry_id → JSON) |
| **Board Meta** | `bmas:board:{task}:meta` | Hash | Board metadata (phase, round, variant) |
| **Private Debate** | `bmas:private:{session}:debate` | List | Per-session debate entries (wiped after consensus) |
| **Locks** | `bmas:locks:{resource}` | String | Redlock distributed locks (`SET NX PX`) |
| **Global Logs** | `bmas:logs:{node_id}` | Stream | Durable agent log streams (capped at 1000 entries) |
| **Task Logs** | `bmas:logs:task:{task_id}` | Stream | Per-task log stream (TTL: 24h) |
| **Metrics (Cost)** | `bmas:metrics:cost` | Hash | Per-model USD cost counters |
| **Metrics (Tokens)** | `bmas:metrics:tokens` | Hash | Per-model token counters |
| **HITL Hints** | `bmas:public:hints:{task_id}` | List | Operator hints injected during pause |
| **Abort Flags** | `bmas:public:abort:{task_id}` | String | Operator abort signals |
| **Pub/Sub (Task)** | `bmas:events:{task_id}` | Channel | Real-time task events (18 types) |
| **Pub/Sub (System)** | `bmas:events:system` | Channel | System-wide events (task-started, task-completed) |

---

### 3.4 LiteLLM — Model Gateway (`litellm/`)

**The router.** A centralized OpenAI-compatible API endpoint that abstracts all model backends behind unified routing, cost tracking, and retry logic.

| Aspect | Detail |
|:---|:---|
| **Image** | Custom (extends official LiteLLM image) |
| **Port** | 4000 |
| **Config** | Auto-generated from `bmas.yaml` at container startup by `generate_config.py` |

| Setting | Value | Rationale |
|:---|:---|:---|
| Strategy | `simple-shuffle` | Round-robin across backends in a model group |
| Retries | 2 with 5s backoff | Handles transient cloud API failures |
| Timeout | 120s | Accounts for edge node cold starts |
| `drop_params` | `true` | Silently drops unsupported params |

---

### 3.5 Triage — Complexity Classifier (`triage/`)

**The gatekeeper.** Classifies task complexity before any paid API call. Routes to the cheapest capable model. Runs only with the `gpu` Docker Compose profile.

| Aspect | Detail |
|:---|:---|
| **Image** | `vllm/vllm-openai:latest` |
| **Port** | 8001 |
| **Model** | Qwen3-1.7B (bfloat16) |
| **GPU** | 35% VRAM utilization |

| Tier | Model Target | Cost |
|:---|:---|:---|
| SIMPLE | Edge node (Gemma 4B) | $0 |
| LIGHT | Gemini Flash Lite | $ |
| MEDIUM | Gemini Flash | $$ |
| COMPLEX | Gemini Pro | $$$ |

The classification is validated by a 117-case evaluation suite (`triage/eval/`).

---

### 3.6 Mission Control — Dashboard (`mission-control/`)

**The eyes.** A real-time operations dashboard for monitoring, debugging, and controlling the swarm.

| Aspect | Detail |
|:---|:---|
| **Framework** | Next.js 16 (App Router) |
| **React** | 19.2.x |
| **Language** | TypeScript 5.x |
| **Styling** | Vanilla CSS with design tokens |
| **Port** | 9321 |

#### Feature Matrix

| Feature | Technology | Data Source |
|:---|:---|:---|
| Task Overview | React + Markdown | REST + SSE |
| Execution Graph (TurnGraph) | React Flow (`@xyflow/react`) | SSE `turn`, `narration` events |
| Distributed Log Stream | TanStack Virtual + detail drawer | SSE `log` events + REST fallback |
| Agent Trace Inspector | TanStack Virtual | REST `/tasks/{id}/trace` |
| Blackboard Board | Timeline / Threads / Graph views | SSE `board_*` events + REST `/tasks/{id}/board` |
| Mission Cockpit | 4-panel live layout | Composite SSE + board + convergence |
| Operator Controls (HITL) | ActionButton + directives | POST `/hitl/{action}` |
| Artifact Browser | File tree + downloads | REST `/tasks/{id}/artifacts` |
| Cost & Budget | BudgetGauge + breakdown | SSE `cost` events |
| Skills Explorer | Tabbed panel | REST `/skills` |
| Hardware Telemetry | Multi-node gauges | REST `/telemetry` (Beszel Hub) |

#### Data Flow

The dashboard uses **Server-Sent Events (SSE)** for all real-time data. The `useTaskStream.ts` hook manages 18 event types with `requestAnimationFrame` batching and REST hydration for initial state:

```
Daemon (Redis Pub/Sub) ──▶ /api/stream/task/{id} (SSE) ──▶ useTaskStream.ts
                                                                │
                     ├──▶ turns[]         → TurnGraph
                     ├──▶ boardEntries[]  → BlackboardBoard
                     ├──▶ logs[]          → DistributedLogStream
                     ├──▶ costEvents[]    → BudgetGauge
                     ├──▶ traces[]        → AgentTrace
                     ├──▶ narrations[]    → TurnGraph (coordinator spine)
                     └──▶ status          → TopBar, StatusBadge
```

All API routes are **server-side proxies** in `src/app/api/` that forward to backend services, avoiding CORS issues.

---

## 4. Task Lifecycle (Traditional Variant)

The traditional variant implements the bMAS paper's cyclic execution model:

### 4.1 Full Flow

```
 User submits task
       │
       ▼
 ┌─────────────┐
 │ 1. TRIAGE   │  Qwen3-1.7B classifies complexity
 │             │  → Selects model tier (SIMPLE/LIGHT/MEDIUM/COMPLEX)
 └──────┬──────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────┐
 │ 2. CYCLIC EXECUTION (repeats until convergence)          │
 │                                                          │
 │   ┌────────────────────┐                                 │
 │   │ Control Unit (CU)  │  LLM reads the full board      │
 │   │                    │  Selects agents for this round  │
 │   │                    │  Emits routing rationale        │
 │   └────────┬───────────┘  (narration event)              │
 │            │                                             │
 │   ┌────────▼──────────┐                                  │
 │   │ Agent Execution   │  Selected agents execute in      │
 │   │                   │  parallel (gateway dispatches    │
 │   │  Planner          │  to nodes via Runs API / CLI)    │
 │   │  Expert.*         │                                  │
 │   │  Critic           │  Each agent reads the board,     │
 │   │  Conflict Resolver│  writes new entries/events       │
 │   │  Cleaner          │                                  │
 │   └────────┬──────────┘                                  │
 │            │                                             │
 │   ┌────────▼──────────┐                                  │
 │   │ Round Accounting  │  Cost rollup, trace ingest,      │
 │   │                   │  board snapshot persistence       │
 │   └────────┬──────────┘                                  │
 │            │                                             │
 │   ┌────────▼──────────┐                                  │
 │   │ Convergence Check │  Is the task done?               │
 │   │                   │  Budget exceeded?                │
 │   │                   │  Stalled (no progress)?          │
 │   │                   │  Max rounds reached?             │
 │   └────────┬──────────┘                                  │
 │            │                                             │
 │      NO ───┘──── YES                                     │
 │     (loop)       │                                       │
 └──────────────────┼───────────────────────────────────────┘
                    │
                    ▼
 ┌─────────────────────┐
 │ 3. FINALIZE         │  Decider agent produces consensus result
 │                     │  Board persisted to SQLite
 │                     │  Task marked complete via SSE
 └─────────────────────┘
```

### 4.2 Agent Roles

| Role | Purpose | When Activated |
|:---|:---|:---|
| **Planner** | Decomposes tasks into structured sub-problems | Early rounds, complex tasks |
| **Expert.*** | Domain-specific expertise (dynamically generated) | Mid rounds, when domain knowledge needed |
| **Critic** | Challenges assumptions, identifies gaps | After initial solutions on the board |
| **Conflict Resolver** | Synthesizes conflicting perspectives | When board has contradictions |
| **Cleaner** | Prunes low-value or redundant entries | When board exceeds `cleaner_entry_threshold` |
| **Decider** | Produces final consensus judgment | Last round (convergence reached) |

The Control Unit (CU) selects which roles to activate each round based on the current board state. Roles are not fixed per agent node — the CU assigns roles dynamically.

### 4.3 Convergence Criteria

A round converges when any of these conditions is met:
1. **CU judges task complete** — The CU's board assessment signals convergence
2. **Budget exceeded** — Cumulative LLM spend exceeds `budget_ceiling_usd`
3. **Stall detection** — No meaningful new entries for `stall_rounds` consecutive rounds
4. **Max rounds** — `max_rounds` limit reached

---

## 5. Data Flow & Persistence

### 5.1 Dual-Write Strategy

Every significant event writes to **two** persistence layers:

```
Event (turn, board entry, log, cost, trace, result)
    │
    ├──→ Redis       Real-time blackboard for live dashboard (ephemeral)
    │                - Pub/Sub for SSE streaming (18 event types)
    │                - Hashes for board entries and state snapshots
    │                - Streams for log tailing
    │
    └──→ SQLite      Permanent task history (durable)
                     - 12 tables: tasks, sub_tasks, debate, cost_entries,
                       logs, turns, board_entries, board_events,
                       agent_traces, task_files, artifacts, ...
                     - Survives Redis eviction and container restarts
```

### 5.2 Real-time Event Delivery (SSE)

| Channel | Pattern | Event Types | Consumers |
|:---|:---|:---|:---|
| **Task** | `bmas:events:{task_id}` | status, phase, log, cost, turn, board_entry, board_event, narration, trace, debate, error, hitl, file, artifact, convergence, steer, approval, complete | `/events/{task_id}` |
| **System** | `bmas:events:system` | task-started, task-completed | `/events/system` |

### 5.3 Agent Ingest Pipeline

Agent nodes ship trace and log data back to the daemon in real-time:

```
Agent Node (TraceEmitter / LogEmitter)
    │
    ├── POST /ingest/traces/{task_id}/{turn_id}  (bearer auth)
    │   → daemon writes to agent_traces table + publishes SSE trace event
    │
    ├── POST /ingest/logs/{task_id}  (bearer auth)
    │   → daemon writes to logs table + publishes SSE log event
    │
    └── POST /ingest/artifacts/{task_id}/{turn_id}  (bearer auth)
        → daemon writes to artifacts table + publishes SSE artifact event
```

---

## 6. Configuration Architecture

Everything is driven by `bmas.yaml`, supplemented by `.env` for secrets:

```
bmas.yaml                        .env
    │                              │
    ├── project (name/desc)        ├── REDIS_PASSWORD
    ├── control_plane              ├── LITELLM_MASTER_KEY
    │   ├── host                   ├── GEMINI_API_KEY
    │   └── ports (5)              ├── ANTHROPIC_API_KEY (optional)
    ├── coordination               ├── OPENAI_API_KEY (optional)
    │   ├── variant                ├── HF_TOKEN
    │   ├── view_budget_tokens     ├── BMAS_NODE_KEY
    │   ├── traditional.*          ├── BESZEL_EMAIL
    │   ├── role_registry          └── BESZEL_PASSWORD
    │   └── board settings
    ├── nodes[] (×3)
    │   ├── host/port
    │   ├── role
    │   └── inference
    ├── triage
    │   ├── enabled/model
    │   └── gpu settings
    ├── models{}
    │   └── provider/model/key
    ├── routing{}
    │   └── complex/medium/light/simple → model
    ├── storage
    │   ├── enabled
    │   ├── user_media_dir
    │   └── artifacts_dir
    └── monitoring
        └── beszel_hub URL
```

---

## 7. Networking & Security

### 7.1 Security Boundaries

| Boundary | Mechanism |
|:---|:---|
| **Redis** | Password-authenticated (`requirepass`). Bound to all interfaces but LAN-only. |
| **LiteLLM** | Master key authentication. All model API calls go through it. |
| **Agent Ingest** | Bearer token authentication (`BMAS_NODE_KEY`) for all `/ingest/*` endpoints |
| **Cloud APIs** | API keys stored in `.env`, never committed to git. Injected as container environment variables. |
| **Agent nodes** | No authentication on `/execute` currently. Relies on LAN isolation. |
| **Dashboard** | No authentication. Intended for internal/homelab use. |
| **Triage** | No authentication. GPU profile must be explicitly enabled. |

> [!WARNING]
> The current security model assumes a trusted LAN. Agent execution endpoints and the dashboard have no authentication. This is appropriate for homelab deployments but not for multi-tenant or internet-facing use.

---

## 8. Agent Role System

The system uses 7 role profiles, dynamically activated by the Control Unit per round:

| Role | Profile | Purpose | Toolset |
|:---|:---|:---|:---|
| **Planner** | `planner` | Decomposes complex tasks into structured execution plans | web, browser, terminal, file |
| **Expert** | `expert` | Domain-specific expertise, dynamically generated per task | full (web, browser, terminal, code_exec, file) |
| **Critic** | `critic` | Challenges assumptions, identifies gaps in the board | web, browser, file (read-only analysis) |
| **Conflict Resolver** | `conflict_resolver` | Synthesizes conflicting perspectives on the board | web, browser, file |
| **Cleaner** | `cleaner` | Prunes low-value or redundant board entries | file only (board content) |
| **Decider** | `decider` | Produces final consensus judgments | web, file |
| **Universal** | `universal` | V2 roleless agent for stigmergic variant | full |

Profiles are deployed to agent nodes via `scripts/deploy_profiles.sh` and stored in `agent/profiles/`. Each profile is a fully isolated Hermes instance with its own `SOUL.md` (identity), `config.yaml` (toolset), memory, and sessions.

The CU determines which roles to activate each round by analyzing the current board state. For example:
- **Round 1**: Planner (decompose the task)
- **Round 2**: Expert.valuation, Expert.engineering (domain work)
- **Round 3**: Critic (challenge the experts' work)
- **Round 4**: Conflict Resolver (synthesize disagreements)
- **Round 5**: Decider (produce consensus)

---

## 9. Concurrency & Locking

### 9.1 Distributed Locking

The blackboard uses single-instance Redlock for coordination:

```python
# Acquire: SET NX PX (atomic test-and-set with TTL)
lock_id = uuid4()
acquired = redis.set(f"bmas:locks:{resource}", lock_id, nx=True, px=ttl_ms)

# Release: Lua script (atomic check-and-delete)
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
```

Lock TTL defaults to 300,000ms (5 minutes), configurable via `LOCK_TTL_MS`.

### 9.2 Per-Task Locking

The gateway (`gateway.py`) acquires per-task locks before dispatching agents, preventing duplicate agent activation within the same round.

---

## 10. Monitoring & Observability

### 10.1 Health Monitoring

A background `health_loop` polls each agent node's `/health` endpoint and publishes status to Redis. The Dashboard consumes this via the system SSE stream.

### 10.2 Cost Tracking

Every LLM call's token usage and cost are tracked in Redis (`bmas:metrics:cost`, `bmas:metrics:tokens`) and SQLite (`cost_entries` table). The traditional variant accumulates per-task spend and checks against `budget_ceiling_usd` each round.

### 10.3 Structured Logging

Agent nodes ship structured logs back to the daemon via `/ingest/logs/{task_id}`. Logs include agent role, level, structured fields (JSON), node ID, and turn ID. The `log_levels.py` module normalizes abbreviations (INF→info, WRN→warning, ERR→error, DBG→debug).

### 10.4 Hardware Telemetry

The Dashboard proxies hardware metrics from [Beszel Hub](https://github.com/henrygd/beszel) (`:8090`) — CPU, RAM, disk, temperature, uptime, and load for all nodes. Configured via `monitoring.beszel_hub` in `bmas.yaml`.

---

## 11. Technology Stack

| Layer | Technology | Version | Purpose |
|:---|:---|:---|:---|
| **Orchestrator** | Python + FastAPI + Uvicorn | 3.13+ | Task lifecycle, API, dispatch |
| **Persistence** | SQLite (aiosqlite) | v2 schema | Permanent task history (12 tables) |
| **Blackboard** | Redis | 7-alpine | Real-time state, Pub/Sub, Streams, locks |
| **Model Gateway** | LiteLLM | latest | Unified model routing + cost tracking |
| **Triage** | vLLM + Qwen3-1.7B | latest | Complexity classification |
| **Agent Runtime** | Hermes CLI / Runs API | — | LLM agent execution on edge nodes |
| **Local Inference** | llama.cpp (llama-server) | — | Free local model inference |
| **Dashboard** | Next.js 16 + React 19 + TypeScript 5 | 16.2.x | Real-time operations UI |
| **Execution Graph** | React Flow (`@xyflow/react`) | 12.x | Swimlane turn visualization |
| **Virtualization** | TanStack Virtual (`@tanstack/react-virtual`) | 3.x | Log/trace list performance |
| **Markdown** | react-markdown + remark-gfm | 10.x | Result rendering |
| **Charts** | Recharts | 3.x | Cost and token visualizations |
| **State Management** | Zustand | 5.x | Client-side state |
| **Styling** | Vanilla CSS (design tokens) | — | Dark-mode-first design system |
| **Containerization** | Docker Compose | — | Control plane orchestration |
| **Edge Infrastructure** | Proxmox VE (LXC) | — | Agent node infrastructure |
| **Monitoring** | Beszel Hub | — | Hardware telemetry |

---

## References

- **Han, B. & Zhang, S. (2025).** *Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture.* [arXiv:2507.01701](https://arxiv.org/abs/2507.01701) — The foundational paper for the bMAS architecture.
- [Stigmergy (Wikipedia)](https://en.wikipedia.org/wiki/Stigmergy) — The coordination mechanism this system is named after.
