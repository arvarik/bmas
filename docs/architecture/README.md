[🏠 Index](../README.md) | [📋 Roadmap](../roadmap/README.md) | [🎨 Design System](../design/DESIGN.md)

# bMAS — System Architecture

> [!NOTE]
> This document describes the **current** architecture as implemented. For planned enhancements and the bMAS paper's full vision (cyclic execution, dynamic roles, etc.), see the [Roadmap](../roadmap/README.md).

## 1. Overview

bMAS (Blackboard Multi-Agent System) is a distributed AI swarm that coordinates multiple LLM-powered agents through a shared blackboard. It implements a subset of the architecture proposed in [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701), where agents coordinate through shared environmental signals rather than direct communication — a pattern called [stigmergy](https://en.wikipedia.org/wiki/Stigmergy).

The system decomposes user tasks into sub-tasks, routes them to appropriate models based on complexity, and produces consensus results through a structured Planner → Executor → Auditor pipeline.

### 1.1 Design Philosophy

| Principle | Implementation |
|:---|:---|
| **Single config file** | All deployment topology, model routing, and node configuration lives in `bmas.yaml` |
| **Docker-first** | The control plane runs as 5 Docker containers via `docker-compose.yml` |
| **Cost-optimized** | A local triage classifier routes tasks to the cheapest capable model |
| **Dual-write persistence** | Every event writes to Redis (real-time) AND SQLite (permanent history) |
| **Fail-open** | Triage, cost tracking, and logging are best-effort — failures never block task execution |

---

## 2. System Topology

```
┌─────────────────────── Control Plane (Docker Compose) ───────────────────────┐
│                                                                               │
│  ┌───────────┐  ┌────────────┐  ┌───────────┐  ┌──────────┐  ┌───────────┐  │
│  │   Redis   │  │  LiteLLM   │  │  Triage   │  │  Daemon  │  │ Dashboard │  │
│  │ Blackboard│  │  Gateway   │  │ Classifier│  │Orchestr. │  │ Mission   │  │
│  │   :6379   │  │   :4000    │  │   :8001   │  │  :9000   │  │ Control   │  │
│  │           │  │            │  │  (GPU)    │  │          │  │  :9321    │  │
│  └─────┬─────┘  └──────┬─────┘  └─────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│        │               │              │             │              │         │
└────────┼───────────────┼──────────────┼─────────────┼──────────────┼─────────┘
         │               │              │             │              │
         │        ┌──────┴──────┐       │      ┌──────┴──────┐       │
         │        │ Cloud APIs  │       │      │ Agent LXCs  │       │
         │        │ Gemini/etc  │       │      │  (×3 nodes) │       │
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

The system separates into three deployment zones:

| Zone | What runs here | Network |
|:---|:---|:---|
| **Control Plane** | 5 Docker containers (Redis, LiteLLM, Triage, Daemon, Dashboard) | Single host, bridged Docker network `bmas` |
| **Edge Nodes** | Proxmox LXCs running Hermes Agent API + optional local inference | LAN — each node exposes `:8000` to the control plane |
| **Cloud APIs** | Gemini, Claude, OpenAI (via LiteLLM proxy) | Internet — authenticated through LiteLLM |

---

## 3. Component Architecture

### 3.1 Daemon — Orchestrator (`daemon/`)

**The brain.** A Python FastAPI service that manages the complete task lifecycle.

| Aspect | Detail |
|:---|:---|
| **Language** | Python 3.12+ |
| **Framework** | FastAPI + Uvicorn |
| **Port** | 9000 |
| **Persistence** | Dual-write: Redis (real-time) + SQLite via aiosqlite (permanent) |
| **Config** | Reads `bmas.yaml` at startup via `config.py` |

#### Internal Structure

```
daemon/src/
├── app.py                  # FastAPI entry point + lifespan (startup/shutdown)
├── config.py               # YAML config loader — parses bmas.yaml into module-level constants
├── database.py             # SQLite persistence layer (aiosqlite) — tasks, sub-tasks, debate, cost, logs
├── core/
│   ├── orchestrator.py     # Task lifecycle: triage → plan → execute → audit → publish
│   ├── blackboard.py       # Redis client abstraction (locks, state, debate, events, logging, HITL)
│   └── triage.py           # Semantic complexity classifier client (calls vLLM)
├── models/
│   └── personas.py         # Agent role definitions + dynamic expert persona generator
├── monitoring/
│   └── health_loop.py      # Background task — polls agent health, publishes to Redis
└── routes/
    ├── submit.py           # POST /submit — async task submission (HTTP 202)
    ├── tasks.py            # GET /tasks, /tasks/{id}/* — task history from SQLite
    ├── events.py           # GET /events/{id}, /events/system — SSE endpoints via Redis Pub/Sub
    └── health.py           # GET /health, /state — dependency health + blackboard snapshot
```

#### Key Design Decisions

- **Dual-write pattern**: Every lifecycle event writes to both Redis (for live SSE streaming to the dashboard) and SQLite (for permanent task history). SQLite writes are best-effort — they log warnings but never interrupt a running task.
- **Lock-per-task**: Each task acquires a Redlock on `orchestrator:{task_id}`. This allows concurrent task execution (future) while preventing duplicate processing.
- **Fail-fast triage**: If the triage service is unreachable, the daemon defaults to `MEDIUM` complexity — it never under-routes to cheaper models.
- **Exponential retry on dispatch**: Agent dispatch retries 3 times with exponential backoff (1s, 2s) to handle Hermes cold-start delays.

---

### 3.2 Agent API Server (`agent/`)

**The hands.** A lightweight FastAPI server deployed to each edge node (Proxmox LXC), bridging the Daemon to the local [Hermes](https://github.com/hypermodeinc/hermes) CLI agent.

| Aspect | Detail |
|:---|:---|
| **Language** | Python 3.12+ |
| **Framework** | FastAPI |
| **Port** | 8000 (per node) |
| **Deployment** | Copied to `/opt/bmas/api_server.py` on each LXC, runs as a systemd service |

#### Endpoints

| Method | Path | Purpose |
|:---|:---|:---|
| `GET` | `/health` | Verifies Hermes binary and LiteLLM gateway connectivity |
| `POST` | `/execute` | Executes a task via `hermes -z` with persona injection |

The agent server receives a `TaskRequest` from the Daemon containing `task_id`, `description`, `role_prompt`, and optional `context`. It invokes the Hermes CLI with the persona as a system prompt and returns the result.

---

### 3.3 Redis — Blackboard (`redis/`)

**The shared memory.** Redis serves as the blackboard — the central knowledge store through which all agents coordinate without direct communication.

| Aspect | Detail |
|:---|:---|
| **Image** | `redis:7-alpine` |
| **Port** | 6379 |
| **Memory** | 1 GB `maxmemory` (2 GB container limit) |
| **Persistence** | RDB snapshots every 60s if ≥100 keys changed |
| **Eviction** | `volatile-lru` — evicts least-recently-used keys with TTL first |

#### Namespace Schema

All keys use the `bmas:` prefix with hierarchical namespacing:

| Namespace | Key Pattern | Data Type | Purpose |
|:---|:---|:---|:---|
| **Public State** | `bmas:public:state` | Hash | Orchestrator phase, iteration count, pause flag |
| **Public Tasks** | `bmas:public:tasks` | Hash | Task registry — maps task IDs to JSON task objects |
| **Public Results** | `bmas:public:results` | Hash | Consensus results — written only by the Auditor |
| **Private Debate** | `bmas:private:{session}:debate` | List | Per-session agent debate entries (wiped after consensus) |
| **Locks** | `bmas:locks:{resource}` | String | Redlock distributed locks (`SET NX PX`) |
| **Global Logs** | `bmas:logs:{node_id}` | Stream | Durable agent log streams (capped at 1000 entries) |
| **Task Logs** | `bmas:logs:task:{task_id}` | Stream | Per-task log stream (TTL: 24h) |
| **Metrics (Cost)** | `bmas:metrics:cost` | Hash | Per-model USD cost counters |
| **Metrics (Tokens)** | `bmas:metrics:tokens` | Hash | Per-model token counters |
| **HITL Hints** | `bmas:public:hints:{task_id}` | List | Operator hints injected during pause |
| **Abort Flags** | `bmas:public:abort:{task_id}` | String | Operator abort signals |
| **Pub/Sub (Task)** | `bmas:events:{task_id}` | Channel | Real-time task events (phase, debate, log, cost, complete) |
| **Pub/Sub (System)** | `bmas:events:system` | Channel | System-wide events (task-started, task-completed) |

#### Why Redis?

| Alternative | Why Not |
|:---|:---|
| Filesystem | No atomic operations, no pub/sub, no distributed locks |
| PostgreSQL | Overkill for ephemeral swarm state; adds latency |
| etcd | Designed for config, not streaming data |
| **Redis** | ✅ Atomic ops (Redlock), Streams for durable logs, sub-ms latency, Pub/Sub for SSE |

---

### 3.4 LiteLLM — Model Gateway (`litellm/`)

**The router.** A centralized OpenAI-compatible API endpoint that abstracts all model backends behind unified routing, cost tracking, and retry logic.

| Aspect | Detail |
|:---|:---|
| **Image** | Custom (extends official LiteLLM image) |
| **Port** | 4000 |
| **Config** | Auto-generated from `bmas.yaml` at container startup by `generate_config.py` |

#### Model Routing

```
Daemon (model="gemini-pro")
    │
    ▼
┌───────────┐
│  LiteLLM  │──→ gemini-pro       ──→ Gemini 3.1 Pro (cloud)      $$$
│   :4000   │──→ gemini-flash     ──→ Gemini 3 Flash (cloud)       $$
│           │──→ gemini-flash-lite ──→ Gemini 3.1 Flash Lite (cloud) $
│           │──→ edge-node-*      ──→ Local inference (llama.cpp)    $0
└───────────┘
```

The Daemon sends model names from `bmas.yaml`'s `routing` section; LiteLLM resolves them to actual backends. Model names and routing are fully configurable — the table above shows an example deployment.

#### Key Settings

| Setting | Value | Rationale |
|:---|:---|:---|
| Strategy | `simple-shuffle` | Round-robin across backends in a model group |
| Retries | 2 with 5s backoff | Handles transient cloud API failures |
| Timeout | 120s | Accounts for edge node cold starts |
| `drop_params` | `true` | Silently drops unsupported params (e.g., `guided_choice` sent to Gemini) |

---

### 3.5 Triage — Complexity Classifier (`triage/`)

**The gatekeeper.** Before any paid API call, triage classifies task complexity and routes to the cheapest capable model. Runs only with the `gpu` Docker Compose profile.

| Aspect | Detail |
|:---|:---|
| **Image** | `vllm/vllm-openai:latest` |
| **Port** | 8001 |
| **Model** | Qwen3-1.7B (bfloat16) |
| **GPU** | 35% VRAM utilization (~5.6 GB on RTX 5060 Ti) |

#### Classification Flow

```
User Task → Qwen3-1.7B + guided_choice + /no_think → TIER
    │
    ├── SIMPLE  → Edge node (Gemma 4B, $0)
    ├── LIGHT   → Gemini Flash Lite ($)
    ├── MEDIUM  → Gemini Flash ($$)
    └── COMPLEX → Gemini Pro ($$$)
```

| Decision | Rationale |
|:---|:---|
| **Qwen3-1.7B** | Best instruction-following for sub-2B models. Small enough to share GPU. |
| **`guided_choice`** | vLLM constrained decoding guarantees valid tier labels — no parsing failures |
| **`/no_think`** | Disables Qwen3's thinking mode — adds latency without improving classification |
| **MEDIUM fallback** | If triage is unreachable, defaults to MEDIUM — never under-routes |

The classification is validated by a 117-case evaluation suite (`triage/eval/`) covering 4 tiers across 9 complexity dimensions.

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
| Task DAG Visualizer | React Flow (`@xyflow/react`) | Daemon `/state` (2s polling) |
| Live Log Terminals | xterm.js (`@xterm/xterm`) | Redis Streams (SSE) |
| Operator Controls (HITL) | ActionButton + Toast | Daemon `/hitl/*` |
| Blackboard Inspector | SplitView | Redis public + private state |
| Cost Tracker | Recharts + MetricCard | Redis metrics hashes |
| Skills Explorer | Tabbed Panel | Agent LXCs `/skills` |
| Hardware Telemetry | Multi-node gauges | Beszel Hub `:8090` |

#### Data Flow

The dashboard never talks to Redis or agents directly from the browser. All API routes are **server-side proxies** in `src/app/api/` that forward to backend services, avoiding CORS issues:

```
Browser (React 19)
    │
    ▼
Next.js Server (API Routes)
    │
    ├──→ Daemon :9000   (state, submit, hitl, tasks, events)
    ├──→ Redis :6379    (logs SSE, cost metrics, private state)
    ├──→ Agent LXCs     (skills proxy)
    └──→ Beszel Hub     (telemetry)
```

**Log streaming** uses Server-Sent Events (SSE), not WebSockets. The `/api/logs` endpoint uses a module-level singleton Redis subscriber to prevent connection pool exhaustion — all connected browser tabs share the same subscriber.

The design system is documented in [DESIGN.md](../design/DESIGN.md).

---

## 4. Task Lifecycle

Every task flows through a structured pipeline. The specific flow depends on the triage classification.

### 4.1 Standard Flow (SIMPLE / LIGHT / MEDIUM)

```
 User submits task
       │
       ▼
 ┌─────────────┐
 │ 1. TRIAGE   │  Qwen3-1.7B classifies complexity
 │             │  → Routes to cheapest capable model
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │ 2. PLAN     │  Planner agent decomposes into sub-task DAG
 │             │  Writes plan to private debate space
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │ 3. EXECUTE  │  Executor agent implements each sub-task
 │             │  Writes results to private debate space
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │ 4. AUDIT    │  Auditor reads entire debate
 │             │  Resolves conflicts, produces consensus
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │ 5. PUBLISH  │  Consensus written to public blackboard
 │             │  Private debate space wiped
 └─────────────┘
```

Each phase transition:
1. Updates Redis state (`bmas:public:state`)
2. Publishes a Pub/Sub event (consumed by SSE endpoints)
3. Writes sub-task status updates to Redis + SQLite
4. Checks for operator abort signals

### 4.2 Complex Research Flow (COMPLEX)

For tasks classified as `COMPLEX`, the orchestrator activates **dynamic expert personas**:

1. **Expert generation** — Gemini Pro generates 3 domain-specific expert identities from the task description
2. **Parallel debate** — All 3 agents (assigned to planner/executor/auditor nodes) run in parallel with their expert personas, writing to the private debate space
3. **Synthesis** — The Auditor persona reads all expert perspectives and produces a unified consensus
4. **Publish** — Same as standard flow

If expert persona generation fails (Gemini unreachable or malformed response), the system falls back to 3 hardcoded defaults: Systems Architecture, Domain Analysis, and Quality Assurance.

---

## 5. Data Flow & Persistence

### 5.1 Dual-Write Strategy

Every significant event writes to **two** persistence layers:

```
Event (phase change, debate entry, log, cost, result)
    │
    ├──→ Redis       Real-time blackboard for live dashboard (ephemeral)
    │                - Pub/Sub for SSE streaming
    │                - Hashes for state snapshots
    │                - Streams for log tailing
    │
    └──→ SQLite      Permanent task history (durable)
                     - Tasks, sub-tasks, debate entries, cost entries, logs
                     - Survives Redis eviction and container restarts
```

Redis writes are **primary** (must succeed for real-time UI). SQLite writes are **best-effort** (log warnings on failure but never interrupt execution). This means:

- The live dashboard always works even if SQLite has issues
- Task history survives Redis restarts and memory evictions
- No single point of failure blocks task execution

### 5.2 Real-time Event Delivery

The system uses two separate Pub/Sub channel patterns for SSE:

| Channel | Pattern | Consumers |
|:---|:---|:---|
| **Task events** | `bmas:events:{task_id}` | `/events/{task_id}` SSE endpoint |
| **System events** | `bmas:events:system` | `/events/system` SSE endpoint |

Task events include: `phase`, `subtask`, `debate`, `log`, `cost`, `error`, `complete`.
System events include: `task-started`, `task-completed`.

---

## 6. Configuration Architecture

Everything is driven by a single `bmas.yaml` file, supplemented by `.env` for secrets:

```
bmas.yaml                    .env
    │                          │
    ├── project (name/desc)    ├── REDIS_PASSWORD
    ├── control_plane          ├── LITELLM_MASTER_KEY
    │   ├── host               ├── GEMINI_API_KEY
    │   └── ports (5)          ├── ANTHROPIC_API_KEY (optional)
    ├── nodes[] (×3)           ├── OPENAI_API_KEY (optional)
    │   ├── host/port          ├── HF_TOKEN
    │   ├── role               ├── BESZEL_EMAIL
    │   └── inference          └── BESZEL_PASSWORD
    ├── triage
    │   ├── enabled
    │   ├── model
    │   └── gpu settings
    ├── models{}
    │   └── provider/model/key
    ├── routing{}
    │   └── complex/medium/light/simple → model
    └── monitoring
        └── beszel_hub URL
```

At container startup:
- **Daemon** reads `bmas.yaml` via `config.py`, exports module-level constants (`AGENT_ENDPOINTS`, `LITELLM_URL`, etc.)
- **LiteLLM** runs `generate_config.py` which translates `bmas.yaml` into LiteLLM's native `config.yaml` format
- **Dashboard** reads `bmas.yaml` for control plane addresses and node topology
- **Redis** uses `entrypoint.sh` to inject `REDIS_PASSWORD` into `redis.conf` from `.env`
- **Triage** receives model/GPU settings via Docker Compose environment variable interpolation

---

## 7. Networking & Security

### 7.1 Network Topology

```
┌──────────────────────────────────────────────────────────┐
│                    Docker Bridge: bmas                    │
│                                                          │
│  redis:6379  litellm:4000  triage:8001  daemon:9000     │
│  dashboard:9321                                          │
└─────────────────────────┬────────────────────────────────┘
                          │  Published ports
                          │  (mapped to host)
                          ▼
┌──────────────────────────────────────────────────────────┐
│                   LAN (192.168.4.0/24)                   │
│                                                          │
│  Control Plane: 192.168.4.240                            │
│  Agent Node 1:  192.168.4.103  (Inference: .102:8080)   │
│  Agent Node 2:  192.168.4.112  (Inference: .111:8080)   │
│  Agent Node 3:  192.168.4.122  (Inference: .121:8080)   │
│  Beszel Hub:    192.168.4.229:8090                       │
└──────────────────────────────────────────────────────────┘
```

### 7.2 Security Boundaries

| Boundary | Mechanism |
|:---|:---|
| **Redis** | Password-authenticated (`requirepass`). Bound to all interfaces but LAN-only. |
| **LiteLLM** | Master key authentication. All model API calls go through it. |
| **Cloud APIs** | API keys stored in `.env`, never committed to git. Injected as container environment variables. |
| **Agent nodes** | No authentication currently. Relies on LAN isolation. |
| **Dashboard** | No authentication. Intended for internal/homelab use. |
| **Triage** | No authentication. GPU profile must be explicitly enabled. |

> [!WARNING]
> The current security model assumes a trusted LAN. Agent endpoints and the dashboard have no authentication. This is appropriate for homelab deployments but not for multi-tenant or internet-facing use.

---

## 8. Agent Role System

The current implementation uses 3 fixed roles with static assignment per node:

| Role | Purpose | Persona |
|:---|:---|:---|
| **Planner** | Decomposes complex tasks into sub-task DAGs | Strategic analyst — breaks problems into structured execution plans |
| **Executor** | Implements sub-tasks (code, research, etc.) | Technical implementer — produces concrete deliverables |
| **Auditor** | Reviews the debate, resolves conflicts, produces consensus | Quality reviewer — synthesizes agent contributions into a coherent result |

Roles are assigned statically in `bmas.yaml` via `nodes[*].role`. Each node runs exactly one role. The Daemon's `config.py` maps roles to HTTP endpoints at startup:

```python
AGENT_ENDPOINTS = {
    "planner":  "http://192.168.4.103:8000",
    "executor": "http://192.168.4.112:8000",
    "auditor":  "http://192.168.4.122:8000",
}
```

For `COMPLEX` tasks, the orchestrator overrides these static personas with dynamically generated expert identities (see §4.2), but the physical node assignment remains the same.

> [!NOTE]
> The bMAS paper (§3.2) proposes dynamic role assignment where any node can assume any role per-task, plus additional roles (Decider, Critic, Conflict-Resolver, Cleaner). See the [roadmap](../roadmap/control-unit.md) for the implementation plan.

---

## 9. Concurrency & Locking

### 9.1 Distributed Locking

The blackboard uses single-instance Redlock for coordination:

```python
# Acquire: SET NX PX (atomic test-and-set with TTL)
lock_id = uuid4()
acquired = redis.set(f"bmas:locks:{resource}", lock_id, nx=True, px=ttl_ms)

# Release: Lua script (atomic check-and-delete, prevents releasing someone else's lock)
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
```

Lock TTL defaults to 300,000ms (5 minutes), configurable via `LOCK_TTL_MS` environment variable.

### 9.2 Current Limitations

- **Single task at a time**: The orchestrator acquires a lock on `orchestrator:{task_id}`, but the current pipeline is sequential — it doesn't process multiple tasks concurrently.
- **No read-write separation**: All blackboard operations go through a single Redis connection.
- **Single-instance Redlock**: Sufficient for homelab, but not HA-safe. Would need multi-instance Redlock (via `aioredlock`) for production.

---

## 10. Monitoring & Observability

### 10.1 Health Monitoring

A background `health_loop` in the Daemon periodically polls each agent node's `/health` endpoint and publishes status to Redis. The Dashboard consumes this via the system SSE stream.

### 10.2 Cost Tracking

Every LLM call's token usage and cost are tracked in Redis (`bmas:metrics:cost`, `bmas:metrics:tokens`) and SQLite (`cost_entries` table). The Dashboard's Cost Tracker renders this as real-time charts.

### 10.3 Hardware Telemetry

The Dashboard proxies hardware metrics from [Beszel Hub](https://github.com/henrygd/beszel) (`:8090`) — CPU, RAM, disk, temperature, uptime, and load for all nodes. This is optional and configured via `monitoring.beszel_hub` in `bmas.yaml`.

---

## 11. Repository Structure

```
bmas/
├── bmas.yaml              # ← Single config file (entire deployment)
├── .env                   # ← Secrets (API keys, passwords)
├── docker-compose.yml     # ← Unified control plane (5 services)
├── docker-compose.dev.yml # ← Dev overrides (hot-reload, volume mounts)
│
├── daemon/                # Python FastAPI orchestrator
│   ├── src/
│   │   ├── app.py         #   Entry point + lifespan
│   │   ├── config.py      #   YAML config → module constants
│   │   ├── database.py    #   SQLite persistence (aiosqlite)
│   │   ├── core/
│   │   │   ├── orchestrator.py   # Task lifecycle engine
│   │   │   ├── blackboard.py     # Redis client (locks, state, events, HITL)
│   │   │   └── triage.py         # Complexity classifier client
│   │   ├── models/
│   │   │   └── personas.py       # Agent role definitions
│   │   ├── monitoring/
│   │   │   └── health_loop.py    # Background agent health polling
│   │   └── routes/
│   │       ├── submit.py         # POST /submit
│   │       ├── tasks.py          # GET /tasks, /tasks/{id}/*
│   │       ├── events.py         # SSE endpoints (task + system)
│   │       └── health.py         # GET /health, /state
│   └── tests/
│
├── agent/                 # Edge node agent API (deployed to LXCs)
│   └── api_server.py      #   Hermes ↔ Daemon bridge (:8000)
│
├── mission-control/       # Next.js 16 dashboard (:9321)
│   └── src/
│       ├── app/            #   App Router pages + API route proxies
│       ├── components/     #   UI primitives + feature components
│       ├── hooks/          #   Zustand store + toast state
│       └── lib/            #   Redis client singleton + design tokens
│
├── litellm/               # LiteLLM model gateway (:4000)
│   ├── generate_config.py  #   bmas.yaml → LiteLLM config.yaml
│   └── entrypoint.sh      #   Startup: generate config + start proxy
│
├── redis/                 # Redis blackboard (:6379)
│   ├── redis.conf.template #   Config template (password injected at runtime)
│   └── entrypoint.sh      #   Startup: render config + start server
│
├── triage/                # vLLM complexity classifier (:8001)
│   └── eval/              #   117-case evaluation suite
│
├── docs/
│   ├── architecture/      #   ← You are here
│   ├── design/            #   Mission Control design system (DESIGN.md)
│   ├── roadmap/           #   Future enhancements (by category)
│   ├── QUICKSTART.md      #   Get running in 5 minutes
│   ├── CONFIGURATION.md   #   Full bmas.yaml reference
│   ├── NODE_SETUP.md      #   Edge node provisioning guide
│   └── HERMES_API.md      #   Hermes tool-calling API reference
│
├── examples/              # Example configurations + diagrams
└── scripts/               # Operational utilities (healthcheck.sh)
```

---

## 12. Technology Stack

| Layer | Technology | Version | Purpose |
|:---|:---|:---|:---|
| **Orchestrator** | Python + FastAPI + Uvicorn | 3.12+ | Task lifecycle, API, dispatch |
| **Persistence** | SQLite (aiosqlite) | — | Permanent task history |
| **Blackboard** | Redis | 7-alpine | Real-time state, Pub/Sub, Streams, locks |
| **Model Gateway** | LiteLLM | latest | Unified model routing + cost tracking |
| **Triage** | vLLM + Qwen3-1.7B | latest | Complexity classification |
| **Agent Runtime** | Hermes CLI | — | LLM agent execution on edge nodes |
| **Local Inference** | llama.cpp (llama-server) | — | Free local model inference |
| **Dashboard** | Next.js 16 + React 19 + TypeScript 5 | 16.2.x | Real-time operations UI |
| **DAG Rendering** | React Flow (`@xyflow/react`) | 12.x | Task graph visualization |
| **Terminals** | xterm.js (`@xterm/xterm`) | 6.x | Live agent log streams |
| **Charts** | Recharts | 3.x | Cost and token visualizations |
| **State Management** | Zustand | 5.x | Client-side state |
| **Styling** | Vanilla CSS (design tokens) | — | Dark-mode-first design system |
| **Containerization** | Docker Compose | — | Control plane orchestration |
| **Virtualization** | Proxmox VE (LXC) | — | Edge node infrastructure |
| **Monitoring** | Beszel Hub | — | Hardware telemetry |

---

## References

- **Han, B. & Zhang, S. (2025).** *Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture.* [arXiv:2507.01701](https://arxiv.org/abs/2507.01701) — The foundational paper for the bMAS architecture.
- [Stigmergy (Wikipedia)](https://en.wikipedia.org/wiki/Stigmergy) — The coordination mechanism this system is named after.
