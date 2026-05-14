# Daemon — Stigmergic Orchestrator

The central orchestration service for Stigmergic. A Python FastAPI application that manages the complete task lifecycle: submission → triage → planning → execution → auditing → consensus.

> Runs on the HP OMEN control plane at `192.168.4.240:9000`, managed by systemd (`bmas-daemon`).

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
| `config.py` | Environment-driven configuration for Redis, LiteLLM, Triage, agent endpoints, and Redlock TTL. |
| `test_daemon.py` | Quick smoke test for the triage router classification. |

## API Endpoints

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/submit` | Submit a task to the swarm. Triggers the full triage → plan → execute → audit pipeline. |
| `GET` | `/state` | Returns the public blackboard state with live agent health (parallel health probes to all 3 agent LXCs). |
| `GET` | `/health` | Health check with Redis connectivity verification. Returns `healthy` or `degraded`. |

## Task Lifecycle

1. **Triage** — Qwen3-1.7B classifies complexity: `SIMPLE` / `LIGHT` / `MEDIUM` / `COMPLEX`
2. **Route** — Maps complexity to a LiteLLM model alias (`edge-node-*` / `light` / `medium` / `heavy`)
3. **Plan** — Planner agent decomposes the task into a DAG of sub-tasks
4. **Execute** — Executor agent implements each sub-task
5. **Audit** — Auditor agent reviews the debate, resolves conflicts, produces consensus
6. **Publish** — Consensus result written to public blackboard, private debate space wiped

For `COMPLEX` tasks, the orchestrator activates a dynamic expert persona flow: Gemini Pro generates 3 domain-specific expert personas, all 3 agents run in parallel, and the Auditor synthesizes the debate.

## Configuration

All values are environment-driven with sensible defaults:

| Variable | Default | Purpose |
|:---|:---|:---|
| `REDIS_URL` | `redis://:bmas-redis-secret-2026@192.168.4.240:6379/0` | Redis connection string |
| `LITELLM_URL` | `http://192.168.4.240:4000/v1` | LiteLLM gateway endpoint |
| `LITELLM_KEY` | `sk-bmas-master-2026` | LiteLLM master API key |
| `TRIAGE_URL` | `http://192.168.4.240:8001/v1` | vLLM triage endpoint |
| `AGENT_1_URL` | `http://192.168.4.103:8000` | Planner agent endpoint |
| `AGENT_2_URL` | `http://192.168.4.112:8000` | Executor agent endpoint |
| `AGENT_3_URL` | `http://192.168.4.122:8000` | Auditor agent endpoint |
| `LOCK_TTL_MS` | `300000` (5 min) | Redlock TTL — must exceed 3× agent dispatch timeout |

## Development

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the daemon (development)
uvicorn main:app --host 0.0.0.0 --port 9000 --reload

# Run via systemd (production)
sudo systemctl start bmas-daemon
sudo systemctl status bmas-daemon
```

## Dependencies

| Package | Purpose |
|:---|:---|
| `fastapi` | ASGI web framework with automatic OpenAPI docs |
| `uvicorn[standard]` | ASGI server with uvloop + httptools |
| `httpx` | Async HTTP client for agent dispatch and LiteLLM calls |
| `redis[hiredis]` | Async Redis client with C-accelerated parser |
| `pydantic` | Request/response validation |
