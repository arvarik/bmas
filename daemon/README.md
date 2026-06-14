# Daemon — bMAS Orchestrator

The central orchestration service for bMAS. A Python FastAPI application that manages the complete task lifecycle through a cyclic blackboard protocol: triage → CU agent selection → board read/write → convergence check → repeat.

> Runs as Docker container `bmas-daemon` on the control plane at port 9000.

## Architecture

```
User / Mission Control
        │
        ▼
   ┌─────────┐     ┌───────────────┐
   │  app.py  │────▶│  orchestrator │── coordinates ──▶ Agent Nodes
   │  FastAPI │     │               │                    (:8000 each)
   └─────────┘     └───────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
         ┌────▼────┐  ┌───▼────┐  ┌───▼─────┐
         │blackboard│  │ board  │  │ triage  │
         │ + gateway│  │ store  │  │         │
         └────┬─────┘  └───┬────┘  └───┬─────┘
              │            │           │
         Redis :6379  SQLite /data  vLLM :8001
```

## Project Structure

```
daemon/
├── src/
│   ├── app.py                     # FastAPI entry point + lifespan
│   ├── config.py                  # YAML config loader + validation
│   ├── database.py                # SQLite persistence (v2 schema, 12 tables)
│   ├── auth.py                    # Bearer token auth for node ingest
│   ├── file_utils.py              # File upload handling + PDF extraction
│   ├── core/
│   │   ├── orchestrator.py        # Task lifecycle + multi-round dispatch
│   │   ├── blackboard.py          # Redis state + durable board snapshots
│   │   ├── board_store.py         # Event-sourced board (entries + events)
│   │   ├── gateway.py             # Capability-gated agent dispatch + locking
│   │   ├── entry.py               # Board entry schema + validation
│   │   ├── protocol.py            # Agent ↔ Daemon message protocol
│   │   ├── salience.py            # Entry salience scoring
│   │   ├── event_emitter.py       # SSE event abstraction (Redis + in-memory)
│   │   ├── capabilities.py        # Agent capability registry
│   │   ├── log_levels.py          # Level normalization (INF→info, etc.)
│   │   ├── triage.py              # Complexity classifier client (vLLM)
│   │   └── variants/
│   │       └── traditional.py     # Cyclic CU → agents → board loop
│   ├── models/
│   │   └── personas.py            # Agent role definitions + dynamic experts
│   ├── monitoring/
│   │   └── health_loop.py         # Background agent health polling
│   └── routes/
│       ├── submit.py              # POST /submit
│       ├── tasks.py               # GET /tasks, /tasks/{id}/*
│       ├── events.py              # SSE endpoints (task + system)
│       ├── ingest.py              # POST /ingest/traces, /ingest/logs
│       ├── artifacts.py           # Artifact ingest + retrieval
│       ├── files.py               # File upload + retrieval
│       ├── hitl.py                # HITL: pause/resume/directive/steer/approval
│       └── health.py              # GET /health, /state
└── tests/                         # 25+ test files (429 tests)
    ├── test_board_store.py        # Event-sourced board
    ├── test_gateway.py            # Agent dispatch + capability gating
    ├── test_traditional_cost.py   # Cost tracking in traditional variant
    ├── test_structured_logs.py    # Structured logging pipeline
    ├── test_coordinator_narration.py  # CU narration events
    ├── test_salience.py           # Entry salience scoring
    ├── test_protocol.py           # Message protocol validation
    └── ...                        # + 18 more test files
```

## API Endpoints

### Task Management

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/submit` | Submit a task (HTTP 202). Triggers triage → CU → agent cycle. |
| `GET` | `/tasks` | List task history with pagination and optional status filter. |
| `GET` | `/tasks/{id}` | Full task detail including variant, turns, cost, status. |
| `GET` | `/tasks/{id}/cost` | Per-task cost breakdown by model and phase. |
| `GET` | `/tasks/{id}/logs` | Archived structured log entries with pagination. |
| `GET` | `/tasks/{id}/debate` | Debate entries for a task. |
| `GET` | `/tasks/{id}/board` | Durable board snapshot (entries + events). |
| `GET` | `/tasks/{id}/trace` | Agent trace data (per-turn structured traces). |
| `GET` | `/tasks/{id}/turns` | Turn-level breakdown (round, agent, selection rationale). |
| `GET` | `/config/active` | Active coordination variant + deployment config. |

### Files & Artifacts

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/tasks/{id}/files` | Upload file attachments to a task. |
| `GET` | `/tasks/{id}/files` | List uploaded files for a task. |
| `GET` | `/tasks/{id}/files/{fid}` | Download a specific file. |
| `GET` | `/tasks/{id}/files/{fid}/text` | Extracted text content (PDF/doc). |
| `GET` | `/tasks/{id}/artifacts` | List artifacts produced by a task. |
| `GET` | `/tasks/{id}/artifacts/{aid}` | Download a specific artifact. |

### Agent Ingest (bearer-authenticated)

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/ingest/traces/{id}/{turn}` | Ingest structured agent traces. |
| `POST` | `/ingest/logs/{id}` | Ingest distributed structured logs. |
| `POST` | `/ingest/artifacts/{id}/{turn}` | Ingest agent-produced artifacts. |

### Streaming & HITL

| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/events/{id}` | Task-scoped SSE stream (18 event types). |
| `GET` | `/events/system` | System health + task lifecycle SSE stream. |
| `POST` | `/{id}/pause` | Pause a running task at the next round boundary. |
| `POST` | `/{id}/resume` | Resume a paused task. |
| `POST` | `/{id}/directive` | Inject an operator directive into the board. |
| `POST` | `/{id}/steer` | Steer agent selection for the next round. |
| `POST` | `/{id}/approval` | Approve or reject a pending agent action. |
| `GET` | `/health` | Dependency health check (Redis + SQLite). |
| `GET` | `/state` | Public blackboard state with live agent health. |

## Task Lifecycle (Traditional Variant)

The traditional variant implements the bMAS paper's cyclic execution model:

```
User submits task
       │
       ▼
┌─────────────┐
│ 1. TRIAGE   │  Qwen3-1.7B classifies complexity → selects model tier
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────────────┐
│ 2. CYCLIC EXECUTION (repeats until convergence) │
│                                                  │
│   ┌──────────────┐                               │
│   │ Control Unit │  LLM reads the full board,    │
│   │ (CU)         │  selects agents for this       │
│   └──────┬───────┘  round with rationale          │
│          │                                        │
│   ┌──────▼──────┐                                 │
│   │ Agent       │  Selected agents execute in     │
│   │ Execution   │  parallel, read board, write    │
│   │             │  new entries to the board        │
│   └──────┬──────┘                                 │
│          │                                        │
│   ┌──────▼──────┐                                 │
│   │ Convergence │  CU checks: is the task done?  │
│   │ Check       │  Budget exceeded? Stalled?      │
│   └──────┬──────┘                                 │
│          │                                        │
│    NO ───┘──── YES                                │
│    (loop)      │                                  │
└────────────────┼──────────────────────────────────┘
                 │
                 ▼
          ┌─────────────┐
          │ 3. FINALIZE │  Decider agent produces consensus result.
          └─────────────┘  Board persisted to SQLite.
```

### Agent Roles

| Role | Description |
|:---|:---|
| **Planner** | Decomposes tasks into structured sub-problems |
| **Expert.*** | Domain-specific experts dynamically generated per task |
| **Critic** | Challenges assumptions, identifies gaps |
| **Conflict Resolver** | Synthesizes conflicting expert perspectives |
| **Cleaner** | Prunes low-value or redundant board entries |
| **Decider** | Produces the final consensus result |

The Control Unit (CU) selects which roles to activate each round based on the current board state — roles are not fixed per agent node.

## Configuration

All values are loaded from `bmas.yaml` and environment variables:

| Source | Variable | Purpose |
|:---|:---|:---|
| `bmas.yaml` | `control_plane.host/ports` | Redis, LiteLLM, Triage URLs |
| `bmas.yaml` | `nodes[*].host/port` | Agent endpoints |
| `bmas.yaml` | `routing.*` | Complexity-to-model mapping |
| `bmas.yaml` | `coordination.variant` | Active variant (traditional/stigmergic) |
| `bmas.yaml` | `coordination.traditional.*` | Variant-specific tuning (rounds, budget, etc.) |
| `bmas.yaml` | `storage.*` | File upload + artifact settings |
| `.env` | `REDIS_PASSWORD` | Redis authentication |
| `.env` | `LITELLM_MASTER_KEY` | LiteLLM API key |
| `.env` | `BMAS_NODE_KEY` | Bearer token for agent ingest auth |

## Development

```bash
# Development (with Docker Compose dev override)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up daemon

# Or run locally
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
BMAS_CONFIG=../bmas.yaml PYTHONPATH=src uvicorn app:app --host 0.0.0.0 --port 9000 --reload
```

### Testing

```bash
# Run the full test suite (429 tests)
cd daemon
pytest tests/ -v --tb=short

# Lint
ruff check src/ tests/

# Type check
mypy src/ --ignore-missing-imports
```

## Dependencies

| Package | Purpose |
|:---|:---|
| `fastapi` ≥ 0.136 | ASGI web framework with automatic OpenAPI docs |
| `uvicorn[standard]` ≥ 0.46 | ASGI server with uvloop + httptools |
| `httpx` ≥ 0.28 | Async HTTP client for agent dispatch and LiteLLM calls |
| `redis[hiredis]` ≥ 7.4 | Async Redis client with C-accelerated parser |
| `pydantic` ≥ 2.13 | Request/response validation |
| `aiosqlite` ≥ 0.21 | Async SQLite for task history persistence |
| `pyyaml` ≥ 6.0 | Configuration file parsing |
| `pymupdf` ≥ 1.25 | PDF text extraction |
| `python-multipart` ≥ 0.0.20 | Multipart form data for file uploads |
