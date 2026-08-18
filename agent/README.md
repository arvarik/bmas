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
| `POST` | `/tasks/{task_id}/cancel` | Cancel local activations and stop recorded Hermes runs for one task |

The `/execute` route accepts `session_id` and `activation_id`. The daemon must
keep each value stable when it retries the same actor activation. The server
uses `session_id` for Hermes memory. The server uses `activation_id` to prevent
duplicate runs. A stable `turn_id` provides the idempotency key when the
request omits `activation_id`.

The server writes a persistent `running` record before execution. It adds the
Hermes run ID after submission. A retry reconnects to that run and saves its
terminal response. The server returns HTTP 409 when the record has no run ID.
This rule prevents an automatic duplicate after a server restart.

The cancellation state is terminal. A late run-ID update or result cannot
replace it. Fresh terminal records remain protected for
`ACTIVATION_CACHE_TTL_SECONDS`. The server returns HTTP 503 when protected
records fill the cache.

An uncertain record without a run ID enters quarantine after
`ACTIVATION_UNCERTAIN_TTL_SECONDS`. Cache pressure can remove an expired
uncertain record. This action can permit a duplicate if the original run still
exists. Use the cancellation route before a manual retry when the run state is
unknown.

Set `BMAS_EXECUTE_KEY` to protect `/execute` and the cancellation route. Send the key as a bearer token or
as `X-BMAS-Execute-Key`. The endpoint stays open when the variable is empty.
This default preserves existing deployments. The `/health` route stays open.

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

Both use bearer token authentication through `BMAS_NODE_KEY`. Trace delivery
uses retries and a disk spool. A later request sends each saved batch again.
The daemon can receive a trace more than once after an uncertain network
failure. It must use the trace identity and sequence to remove duplicates.
The spool evicts old reasoning batches before final and error batches. It
rejects new low-priority data when only terminal batches remain.

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
| `BMAS_EXECUTE_KEY` | *(empty)* | Optional bearer or `X-BMAS-Execute-Key` credential for `/execute` |
| `SSE_READ_TIMEOUT` | `600` | SSE stream read timeout in seconds |
| `CANCELLATION_TIMEOUT_SECONDS` | `5` | Maximum time for one Hermes stop request |
| `TRACE_FLUSH_RETRIES` | `3` | Number of delivery attempts for each trace batch |
| `TRACE_RETRY_BASE_SECONDS` | `0.25` | Base delay for trace delivery retries |
| `TRACE_SPOOL_DIR` | `/tmp/bmas-trace-spool` | Development trace queue directory. Use persistent storage in production. |
| `TRACE_DRAIN_TIMEOUT_SECONDS` | `5` | Maximum final trace delivery wait |
| `TRACE_SPOOL_MAX_FILES` | `10000` | Maximum queued trace files |
| `TRACE_SPOOL_MAX_BYTES` | `268435456` | Maximum queued trace bytes |
| `TRACE_EVENT_MAX_BYTES` | `65536` | Maximum bytes in one trace event |
| `TRACE_MEMORY_MAX_EVENTS` | `1000` | Maximum trace events retained in memory |
| `LOG_RECORD_MAX_BYTES` | `65536` | Maximum bytes in one structured log record |
| `LOG_BUFFER_MAX_RECORDS` | `1000` | Maximum structured log records retained in memory |
| `ACTIVATION_CACHE_TTL_SECONDS` | `3600` | Completed activation response lifetime |
| `ACTIVATION_CACHE_MAX_ENTRIES` | `1000` | Maximum completed activation responses in memory and on disk |
| `ACTIVATION_CACHE_MAX_BYTES` | `67108864` | Maximum bytes in the durable activation cache |
| `ACTIVATION_CACHE_DIR` | `$TRACE_SPOOL_DIR/activations` | Activation state directory. Use persistent storage in production. |
| `ACTIVATION_RUNNING_TTL_SECONDS` | `7200` | Age that changes a stale running record to uncertain |
| `ACTIVATION_UNCERTAIN_TTL_SECONDS` | `21600` | Age that quarantines an uncertain record without a run ID |

> **Feature gating:** Set `HERMES_GATEWAY_URL` to enable the Runs API path. Without it, all execution falls back to `hermes -z`. Set `DAEMON_INGEST_URL` + `BMAS_NODE_KEY` to enable trace/log shipping.

`TASK_TIMEOUT_SECONDS` and the request `timeout` field limit the complete
execution operation. This limit includes staging, run submission, streaming,
polling, and artifact synchronization. `SSE_READ_TIMEOUT` limits one idle SSE
read. A bounded cancellation request can run after the execution deadline. The
CLI path uses the daemon-selected model.

Trace and log limits never truncate the final result returned to the daemon.
An oversized cached result returns in full for its first request. The durable
record then blocks an uncertain duplicate without storing the oversized body.

The `/tmp` defaults support development only. A reboot can erase those files.
Production nodes must use the persistent paths in `docs/NODE_SETUP.md`.

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
# 69 tests covering parsing, translation, execution, and reliability
```
