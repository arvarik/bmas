# bMAS Agent API Server

FastAPI server that runs on each **edge node**, bridging the bMAS Daemon to the local Hermes Agent installation.

> **This is the canonical source.** Deploy updates by copying `api_server.py` to the target node at `/opt/bmas/api_server.py` and restarting the `hermes-agent` service.

## Endpoints

| Method | Path | Description |
|:---|:---|:---|
| `GET` | `/health` | Health check — verifies Hermes binary and LiteLLM gateway |
| `POST` | `/execute` | Execute a task via `hermes -z` with optional persona injection |

> **Skills** are managed by the Hermes Dashboard (`:9119/api/skills/*`), not this server. Mission Control proxies skills requests directly to the dashboard.

## Requirements

```
fastapi
uvicorn[standard]
httpx
pydantic
```

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|:---|:---|:---|
| `LITELLM_URL` | `http://localhost:4000/v1` | LiteLLM gateway URL (set to your control plane IP) |
| `LITELLM_MODEL` | `medium` | Default LiteLLM model name |
| `HERMES_BIN` | `/usr/local/bin/hermes` | Path to Hermes CLI binary |
| `TASK_TIMEOUT_SECONDS` | `120` | Default task execution timeout |
| `NODE_ID` | `agent-node1` | Identifier for this node (used in logs and responses) |

## Running

```bash
# Development
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

# Production (via systemd — see docs/NODE_SETUP.md)
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

## Deploying to Nodes

```bash
# Copy to your agent nodes (replace with your actual IPs from bmas.yaml)
for ip in <AGENT_NODE_1_IP> <AGENT_NODE_2_IP> <AGENT_NODE_3_IP>; do
  scp api_server.py root@$ip:/opt/bmas/api_server.py
  ssh root@$ip 'systemctl restart hermes-agent'
done
```
