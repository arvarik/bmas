# Execution Agent API

This FastAPI service executes one classic role activation for the daemon.

The default Docker stack runs the tool-free LiteLLM backend. Advanced nodes can use a Hermes Runs API or Hermes CLI.

## Execution backends

| Backend | Configuration | Use |
|:---|:---|:---|
| Direct LiteLLM | `BMAS_EXECUTION_BACKEND=litellm` | Single-host starter without tools |
| Hermes Runs API | `BMAS_EXECUTION_BACKEND=hermes` plus `HERMES_GATEWAY_URL` | Advanced tools, sessions, and streamed run state |
| Hermes CLI | `BMAS_EXECUTION_BACKEND=hermes` plus `HERMES_BIN` | Advanced one-process fallback |

The direct backend sends the role prompt, task objective, and blackboard context to LiteLLM. It returns the model result and token usage.

The Hermes backends add profiles, persistent session state, tool traces, and cancellation of remote runs.

The reviewed API contract targets Hermes Agent v0.20.4. Read [Hermes Integration Contract](../docs/HERMES_API.md) before you update a node.

## Endpoints

| Method | Path | Purpose |
|:---|:---|:---|
| `GET` | `/health` | Reports bounded capability-aware readiness. |
| `GET` | `/health/detailed` | Reports the upstream Hermes readiness and capability contract. |
| `POST` | `/execute` | Executes one idempotent role activation. |
| `POST` | `/tasks/{task_id}/cancel` | Cancels local activations and known Hermes runs. |
| `GET` | `/v1/capabilities` | Proxies Hermes capability discovery. |
| `GET` | `/v1/skills` | Proxies the read-only active skill inventory. |
| `GET` | `/v1/toolsets` | Proxies the read-only toolset inventory. |
| `GET` | `/api/sessions*` | Proxies session browsing. |
| `POST` | `/api/sessions/{session_id}/fork` | Forks one Hermes session. |
| `POST` | `/v1/runs/{run_id}/approval` | Sends an approval choice. |
| `POST` | `/v1/runs/{run_id}/steer` | Sends live run guidance. |

Set `BMAS_EXECUTE_KEY` to protect detailed health, execution, cancellation, and Hermes proxy routes. The daemon and Mission Control send the same bearer credential.

## Idempotent execution

The request can include `activation_id`, `session_id`, and `turn_id`.

1. The daemon keeps `activation_id` stable during retries.
2. The agent saves a running record before execution.
3. A retry reads the existing record or reconnects to a known Hermes run.
4. A terminal response remains in the durable activation cache.
5. A cancelled activation stays terminal.

The agent returns HTTP 409 when a running record has no known Hermes run identifier. This result prevents an automatic duplicate.

The direct backend also uses the same activation cache. A daemon retry does not create a second provider request after a saved terminal result.

## Logs and traces

Set both `DAEMON_INGEST_URL` and `BMAS_NODE_KEY` to send records to the daemon.

- The log emitter sends bounded structured log records.
- The trace emitter batches translated execution events.
- The trace emitter saves failed batches to a disk spool.
- A later request retries saved batches.

Use persistent directories outside `/tmp` for a deployed node.

## Profiles

The classic Hermes profiles are `planner`, `expert`, `critic`, `conflict_resolver`, `cleaner`, and `decider`.

Read [profiles/README.md](profiles/README.md) for identity and tool-scope details.

The direct starter uses the profile name as a role selector. It does not load Hermes tools.

## Main environment variables

| Variable | Default | Purpose |
|:---|:---|:---|
| `BMAS_EXECUTION_BACKEND` | `auto` | Selects `litellm`, `hermes`, or automatic Hermes detection. |
| `NODE_ID` | `agent-node1` | Sets the stable node identifier. |
| `LITELLM_URL` | `http://localhost:4000/v1` | Sets the model gateway URL. |
| `LITELLM_MODEL` | `medium` | Sets the fallback model alias. |
| `LITELLM_API_KEY` | Empty | Authenticates model gateway calls. |
| `BMAS_EXECUTE_KEY` | Empty | Authenticates daemon execution requests. |
| `DAEMON_INGEST_URL` | Empty | Sets the daemon base URL for logs and traces. |
| `BMAS_NODE_KEY` | Empty | Authenticates log and trace ingest. |
| `TASK_TIMEOUT_SECONDS` | `120` | Limits one complete execution request. |

## Hermes environment variables

| Variable | Default | Purpose |
|:---|:---|:---|
| `HERMES_GATEWAY_URL` | Empty | Enables the Hermes Runs API. |
| `HERMES_GATEWAY_KEY` | Empty | Authenticates Hermes gateway calls. |
| `HERMES_BIN` | `/usr/local/bin/hermes` | Selects the Hermes CLI fallback. |
| `SSE_READ_TIMEOUT` | `600` | Limits one idle Hermes event read. |
| `CANCELLATION_TIMEOUT_SECONDS` | `5` | Limits one Hermes cancellation call. |
| `HERMES_429_MAX_ATTEMPTS` | `3` | Limits pre-admission capacity retries. |
| `HERMES_429_RETRY_BASE_SECONDS` | `0.5` | Sets the first fallback retry delay. |
| `HERMES_429_RETRY_MAX_SECONDS` | `5` | Limits one agent retry delay. |
| `HERMES_PROXY_MAX_BODY_BYTES` | `1048576` | Limits one proxied request body. |

Hermes calls the upstream authentication value `API_SERVER_KEY`. Set `HERMES_GATEWAY_KEY` to the same value for the selected profile.

A multi-profile Hermes gateway uses a profile URL prefix. For example, set `HERMES_GATEWAY_URL=http://127.0.0.1:8642/p/planner`.

Hermes v0.20.4 exposes capabilities, detailed health, approvals, steering, sessions, skills, and toolsets. The adapter proxies these routes through its fixed Hermes URL.

Hermes exposes skills and toolsets as read-only inventory. It does not provide an API server route that changes them.

The adapter sends `X-Hermes-Session-Key` as `bmas:<task-actor-session>`. This value stays stable across rounds and node rescheduling without sharing memory between tasks.

## Reliability limits

| Variable | Default | Purpose |
|:---|:---|:---|
| `TRACE_SPOOL_DIR` | `/tmp/bmas-trace-spool` | Stores failed trace batches. |
| `TRACE_FLUSH_RETRIES` | `3` | Limits immediate trace delivery attempts. |
| `TRACE_SPOOL_MAX_FILES` | `10000` | Limits saved trace files. |
| `TRACE_SPOOL_MAX_BYTES` | `268435456` | Limits saved trace bytes. |
| `TRACE_EVENT_MAX_BYTES` | `65536` | Limits one trace event. |
| `TRACE_MEMORY_MAX_EVENTS` | `1000` | Limits traces held in memory. |
| `LOG_RECORD_MAX_BYTES` | `65536` | Limits one structured log record. |
| `LOG_BUFFER_MAX_RECORDS` | `1000` | Limits buffered log records. |
| `ACTIVATION_CACHE_DIR` | Under the trace directory | Stores durable activation state. |
| `ACTIVATION_CACHE_TTL_SECONDS` | `3600` | Retains terminal responses. |
| `ACTIVATION_CACHE_MAX_ENTRIES` | `1000` | Limits activation records. |
| `ACTIVATION_CACHE_MAX_BYTES` | `67108864` | Limits activation record bytes. |

The Docker starter mounts persistent agent state at `/var/lib/bmas-agent`.

## Run the Docker starter

Use the repository command from the root directory.

```bash
./scripts/bmas up
```

The Compose file sets the direct LiteLLM backend and each required key.

## Run an advanced node

Read [Hermes Node Setup](../docs/NODE_SETUP.md). That guide uses a non-root system service and a protected environment file.

## Test

```bash
../.venv/bin/python -m pytest tests -q
```

The tests cover event parsing, idempotency, cancellation, direct LiteLLM dispatch, Hermes dispatch, and trace delivery.
