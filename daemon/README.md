# Daemon — bMAS Orchestrator

The central orchestration service for bMAS. A Python FastAPI application that manages the complete task lifecycle: submission → triage → planning → execution → auditing → consensus.

> Runs as Docker container `bmas-daemon` on the control plane at port 9000.

## Architecture

```
User / Mission Control
        │
        ▼
   ┌─────────┐     ┌──────────────┐
   │  app.py  │────▶│ orchestrator │──── dispatches to ────▶ Agent LXCs
   │  FastAPI │     │              │                         (:8000 each)
   └─────────┘     └──────┬───────┘
                          │
                   ┌──────┴──────┐
                   │             │
              ┌────▼────┐  ┌────▼─────┐
              │blackboard│  │  triage  │
              └────┬─────┘  └────┬─────┘
                   │             │
              Redis :6379   vLLM :8001
```

## Project Structure

```
daemon/
├── src/
│   ├── app.py                  # FastAPI entry point + lifespan
│   ├── config.py               # YAML config loader + validation
│   ├── database.py             # SQLite persistence (aiosqlite)
│   ├── routes/
│   │   ├── submit.py           # POST /submit
│   │   ├── tasks.py            # GET /tasks, /tasks/{id}/*
│   │   ├── events.py           # SSE endpoints (task + system)
│   │   └── health.py           # GET /health, /state
│   ├── core/
│   │   ├── orchestrator.py     # Task lifecycle + dispatch
│   │   ├── blackboard.py       # Redis client abstraction
│   │   └── triage.py           # Semantic complexity classifier
│   ├── models/
│   │   └── personas.py         # Agent role definitions
│   └── monitoring/
│       └── health_loop.py      # Background health publisher
└── tests/
    ├── test_triage_demo.py     # Triage classification smoke test
    └── test_sse_smoke.py       # SSE streaming smoke test
```

## API Endpoints

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/submit` | Submit a task (HTTP 202). Triggers triage → plan → execute → audit. |
| `GET` | `/tasks` | List task history with pagination and optional status filter. |
| `GET` | `/tasks/{id}` | Full task detail including sub-tasks. |
| `GET` | `/tasks/{id}/cost` | Per-task cost breakdown by model and phase. |
| `GET` | `/tasks/{id}/logs` | Archived log entries with pagination. |
| `GET` | `/tasks/{id}/debate` | Debate entries for a task. |
| `GET` | `/events/{id}` | Task-scoped SSE stream (real-time updates). |
| `GET` | `/events/system` | System health + task lifecycle SSE stream. |
| `GET` | `/state` | Public blackboard state with live agent health. |
| `GET` | `/health` | Dependency health check (Redis + SQLite). |

## Task Lifecycle

1. **Triage** — Qwen3-1.7B classifies complexity: `SIMPLE` / `LIGHT` / `MEDIUM` / `COMPLEX`
2. **Route** — Maps complexity to a LiteLLM model alias via the `routing` table in `bmas.yaml`
3. **Plan** — Planner agent decomposes the task into a DAG of sub-tasks
4. **Execute** — Executor agent implements each sub-task
5. **Audit** — Auditor agent reviews the debate, resolves conflicts, produces consensus
6. **Publish** — Consensus result written to public blackboard, private debate space wiped

For `COMPLEX` tasks, the orchestrator activates a dynamic expert persona flow: Gemini Pro generates 3 domain-specific expert personas, all 3 agents run in parallel, and the Auditor synthesizes the debate.

## Configuration

All values are loaded from `bmas.yaml` and environment variables:

| Source | Variable | Purpose |
|:---|:---|:---|
| `bmas.yaml` | `control_plane.host/ports` | Redis, LiteLLM, Triage URLs |
| `bmas.yaml` | `nodes[*].host/port` | Agent endpoints |
| `bmas.yaml` | `routing.*` | Complexity-to-model mapping |
| `.env` | `REDIS_PASSWORD` | Redis authentication |
| `.env` | `LITELLM_MASTER_KEY` | LiteLLM API key |
| env | `LOCK_TTL_MS` | Redlock TTL (default: 300000) |

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

## Dependencies

| Package | Purpose |
|:---|:---|
| `fastapi` | ASGI web framework with automatic OpenAPI docs |
| `uvicorn[standard]` | ASGI server with uvloop + httptools |
| `httpx` | Async HTTP client for agent dispatch and LiteLLM calls |
| `redis[hiredis]` | Async Redis client with C-accelerated parser |
| `pydantic` | Request/response validation |
| `aiosqlite` | Async SQLite for task history persistence |
| `pyyaml` | Configuration file parsing |
