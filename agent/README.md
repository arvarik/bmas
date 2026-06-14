# bMAS Agent API Server

FastAPI server that runs on each **edge node**, bridging the bMAS Daemon to the local [Hermes](https://github.com/hypermodeinc/hermes) agent installation.

Supports two execution paths:
1. **Runs API** (primary) — `POST /v1/runs` + SSE streaming via the Hermes Gateway, with real-time trace and log ingest back to the daemon
2. **CLI fallback** — `hermes -z` subprocess execution when the Runs API is unavailable

> **This is the canonical source.** Deploy updates by copying `api_server.py` and `profiles/` to each target node and restarting the `hermes-agent` service.

## Endpoints

| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/health` | Health check — verifies Hermes binary, optional Runs API gateway, and LiteLLM connectivity |
| `POST` | `/execute` | Execute a task via Runs API (SSE) or `hermes -z` fallback, with persona/profile injection |

## Execution Flow

```
Daemon ─── POST /execute ──▶ Agent API Server
                                    │
                         ┌──────────┴──────────┐
                         │                      │
              HERMES_GATEWAY_URL           No gateway
              is set?                      configured?
                         │                      │
                  ┌──────▼──────┐       ┌───────▼───────┐
                  │ Runs API    │       │ CLI fallback  │
                  │ POST /v1/runs│      │ hermes -z     │
                  │ + SSE stream│       │ subprocess    │
                  └──────┬──────┘       └───────┬───────┘
                         │                      │
                  TraceEmitter ──▶ Daemon /ingest/traces
                  LogEmitter  ──▶ Daemon /ingest/logs
```

### TraceEmitter & LogEmitter

When `DAEMON_INGEST_URL` and `BMAS_NODE_KEY` are configured, the agent server ships structured data back to the daemon in real-time:

- **TraceEmitter** — Batches and POSTs agent traces (tool calls, content blocks, function results) to `/ingest/traces/{task_id}/{turn_id}`
- **LogEmitter** — Ships structured per-agent log entries (with fields, node ID, turn ID) to `/ingest/logs/{task_id}`

Both use bearer token authentication (`BMAS_NODE_KEY`) and are fire-and-forget (failures logged but never block execution).

## Profiles

Hermes profiles implement the persona library from the bMAS paper. Each profile is a fully isolated Hermes instance with its own `SOUL.md`, `config.yaml`, toolset, memory, and sessions.

| Profile | Paper Role | Description |
|:---|:---|:---|
| `planner` | Planner | Decomposes tasks into structured sub-problems |
| `expert` | Dynamic Expert | Domain-specific expert, dynamically generated per task |
| `critic` | Critic | Challenges assumptions, identifies gaps |
| `conflict_resolver` | Conflict Resolver | Synthesizes conflicting perspectives |
| `cleaner` | Cleaner | Prunes low-value or redundant board entries |
| `decider` | Decider | Produces final consensus judgments |
| `universal` | Roleless (V2) | Full toolset, used for stigmergic variant |

See [profiles/README.md](profiles/README.md) for the full profile specification.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|:---|:---|:---|
| `LITELLM_URL` | `http://localhost:4000/v1` | LiteLLM gateway URL |
| `LITELLM_MODEL` | `medium` | Default LiteLLM model name |
| `HERMES_BIN` | `/usr/local/bin/hermes` | Path to Hermes CLI binary |
| `TASK_TIMEOUT_SECONDS` | `120` | Default task execution timeout |
| `NODE_ID` | `agent-node1` | Node identifier (used in logs and trace attribution) |
| `HERMES_GATEWAY_URL` | *(unset)* | Hermes Gateway URL to enable Runs API path (e.g., `http://localhost:8642`) |
| `HERMES_GATEWAY_KEY` | *(empty)* | API key for the Hermes Gateway |
| `DAEMON_INGEST_URL` | *(unset)* | Daemon URL for trace/log ingest (e.g., `http://192.168.4.240:9000`) |
| `BMAS_NODE_KEY` | *(empty)* | Bearer token for authenticating ingest requests to the daemon |
| `SSE_READ_TIMEOUT` | `600` | SSE stream read timeout in seconds |

> **Feature gating:** Set `HERMES_GATEWAY_URL` to enable the Runs API path. Without it, all execution falls back to `hermes -z`. Set `DAEMON_INGEST_URL` + `BMAS_NODE_KEY` to enable trace/log shipping.

## Requirements

```
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
httpx>=0.28.0
pydantic>=2.10.0
```

## Running

```bash
# Development
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

# Production (via systemd — see docs/NODE_SETUP.md)
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

## Deploying to Nodes

```bash
# Deploy agent code + profiles to all nodes (replace with your IPs from bmas.yaml)
for ip in <AGENT_NODE_1_IP> <AGENT_NODE_2_IP> <AGENT_NODE_3_IP>; do
  scp api_server.py root@$ip:/opt/bmas/api_server.py
  scp requirements.txt root@$ip:/opt/bmas/requirements.txt
  ssh root@$ip 'cd /opt/bmas && pip install -r requirements.txt'
  ssh root@$ip 'systemctl restart hermes-agent'
done

# Deploy Hermes profiles (uses the helper script)
./scripts/deploy_profiles.sh
```

## Testing

```bash
cd agent
pytest tests/ -v --tb=short
# 39 tests covering SSE parsing, event translation, and Runs API integration
```
