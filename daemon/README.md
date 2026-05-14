# Daemon — Stigmergic Orchestrator

The central orchestration service for Stigmergic. A Python FastAPI application that manages the complete task lifecycle: submission → triage → planning → execution → auditing → consensus.

> Runs as Docker container `bmas-daemon` on the control plane at port 9000.

## Architecture

```
User / Mission Control
        │
        ▼
   ┌─────────┐     ┌─────────────┐
   │ main.py │────▶│ orchestrator │──── dispatches to ────▶ Agent LXCs
   │ FastAPI │     │   .py       │                         (:8000 each)
   └─────────┘     └──────┬──────┘
                          │
                   ┌──────┴──────┐
                   │             │
              ┌────▼────┐  ┌────▼────────┐
              │blackboard│  │triage_router│
              │   .py    │  │    .py      │
              └────┬─────┘  └─────┬───────┘
                   │              │
              Redis :6379    vLLM :8001
```

## Modules

| File | Purpose |
|:---|:---|
| `main.py` | FastAPI entry point. Defines `/submit`, `/state`, `/health` endpoints. Manages lifespan (Redis pre-flight, HTTP client pool). |
| `orchestrator.py` | Core task lifecycle. Implements standard flow (Plan → Execute → Audit) and complex research flow (dynamic expert personas + parallel debate). Handles Redlock, HITL pause gates, and phase tracking. |
| `blackboard.py` | Redis client abstraction. Manages all 6 namespaces: public state, private debate, locks, log streams, metrics, and HITL hints. Uses atomic Lua scripts for lock release. |
| `triage_router.py` | Semantic complexity classifier. Routes tasks to the local Qwen3-1.7B model via vLLM with `guided_choice` constrained decoding. Maps results to LiteLLM model aliases. |
| `personas.py` | Agent role definitions (Planner, Executor, Auditor) sent as `role_prompt` payloads. Includes dynamic expert persona generation for complex research tasks. |
| `config.py` | Loads `bmas.yaml` at import time. Builds all derived URLs, agent endpoints, routing table, and triage settings. |
| `test_daemon.py` | Quick smoke test for the triage router classification. |

## API Endpoints

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/submit` | Submit a task to the swarm. Triggers the full triage → plan → execute → audit pipeline. |
| `GET` | `/state` | Returns the public blackboard state with live agent health (parallel health probes to all 3 agent LXCs). |
| `GET` | `/health` | Health check with Redis connectivity verification. Returns `healthy` or `degraded`. |

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
BMAS_CONFIG=../bmas.yaml uvicorn main:app --host 0.0.0.0 --port 9000 --reload
```

## Dependencies

| Package | Purpose |
|:---|:---|
| `fastapi` | ASGI web framework with automatic OpenAPI docs |
| `uvicorn[standard]` | ASGI server with uvloop + httptools |
| `httpx` | Async HTTP client for agent dispatch and LiteLLM calls |
| `redis[hiredis]` | Async Redis client with C-accelerated parser |
| `pydantic` | Request/response validation |
