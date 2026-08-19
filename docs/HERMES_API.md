# Hermes Integration Contract

This document defines the Hermes calls that the Stigmergic agent adapter uses.

Hermes remains optional. The default starter calls LiteLLM directly and does not require this contract.

## Reviewed Hermes release

This contract was reviewed against [Hermes Agent v0.20.4](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.18), released on August 18, 2026.

The five required Runs API routes remain compatible with the adapter. Hermes v0.20.4 also adds discovery, approval, steering, session, skill, and toolset routes that bMAS now uses.

Pin the tag `v2026.8.18` for deployment testing. Run `hermes --version` on every node and record the result with the deployment.

Hermes can change these routes in a later release. Repeat the contract tests before each version change.

## Upstream API

| API | Common port | bMAS use |
|:---|:---|:---|
| Hermes API server | `8642` | Runs, readiness, approvals, steering, skills, toolsets, and sessions |

Hermes starts the API server as a gateway platform. bMAS does not scrape or call the Hermes Dashboard.

## API server authentication

Configure the upstream Hermes process with these variables:

```env
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=replace-with-gateway-key
```

Configure the Stigmergic adapter with matching values:

```env
HERMES_GATEWAY_URL=http://127.0.0.1:8642
HERMES_GATEWAY_KEY=replace-with-gateway-key
```

The adapter sends `HERMES_GATEWAY_KEY` as an HTTP bearer token. Hermes validates that token against `API_SERVER_KEY`.

Do not expose the API server to an untrusted network. The API server can provide access to the complete configured toolset.

## Required API server routes

The adapter uses these routes.

| Method | Path | Purpose |
|:---|:---|:---|
| `GET` | `/health` | Checks API server reachability. |
| `POST` | `/v1/runs` | Creates one run. |
| `GET` | `/v1/runs/{run_id}` | Reads run state and recovers after a disconnect. |
| `GET` | `/v1/runs/{run_id}/events` | Streams run events through Server-Sent Events. |
| `POST` | `/v1/runs/{run_id}/stop` | Stops a timed-out or cancelled run. |

The node is not ready for Hermes execution until each required route works with the configured key.

## Capability discovery

Hermes v0.20.4 provides `GET /v1/capabilities`. The response lists supported features, route paths, authentication, and session headers.

The agent queries this route for each health snapshot. It checks each required feature and its exact method and path.

The daemon marks a node ready only when the agent reports a complete Runs API contract. A legacy health response proves liveness but not readiness.

Hermes also provides `GET /health/detailed`. This route reports bounded readiness data for the active profile, state database, model, disk, and active runs.

The upstream `GET /health` route only proves liveness. A degraded detailed health response still uses HTTP 200, so the agent inspects its top-level status.

The agent exposes its result through `GET /health` and `GET /health/detailed`. The detailed route includes the bounded upstream capabilities and readiness object.

## Run request

The adapter sends this shape to `POST /v1/runs`:

```json
{
  "input": "Task objective plus the bounded blackboard context",
  "instructions": "Role prompt",
  "model": "daemon-selected-model",
  "session_id": "stable-actor-session",
  "previous_response_id": "optional-prior-response"
}
```

| Field | Source | Purpose |
|:---|:---|:---|
| `input` | Task and context | Supplies the current role work. |
| `instructions` | Role prompt | Adds role behavior to the Hermes system prompt. |
| `model` | Daemon routing | Selects the model for this Hermes run. |
| `session_id` | Daemon activation context | Preserves actor session state. |
| `previous_response_id` | Saved turn context | Continues state when Hermes retains the response. |

Hermes v0.20.4 also accepts `provider`, `model_options`, and `conversation_history`. The current adapter does not send these fields.

Hermes gives `conversation_history` precedence over `previous_response_id`. The current adapter sends only `previous_response_id` when the blackboard context supplies it.

Hermes returns HTTP 202 with `run_id` and `status: started`. The adapter also accepts an `id` field for older compatible servers.

## Profile selection

The Runs API request does not carry a `profile` field. Hermes selects a profile from the API server process or the request URL.

Hermes v0.20.4 supports shared multi-profile routing when `gateway.multiplex_profiles` is true. A named profile uses a `/p/<profile>/` prefix.

For example, a planner node can use this adapter value:

```env
HERMES_GATEWAY_URL=http://127.0.0.1:8642/p/planner
```

Each named profile must define its own `API_SERVER_KEY`. Set `HERMES_GATEWAY_KEY` to the key for the selected profile.

Unprefixed routes use the default profile. A separate profile-scoped gateway process also remains valid.

The CLI fallback passes `-p PROFILE` for each activation.

## Event stream

Hermes v0.20.4 puts the event name in the JSON `event` field inside each Server-Sent Events data record. The adapter also accepts a standard `event:` line.

The adapter translates these event names:

| Hermes event | Stigmergic trace type | Notes |
|:---|:---|:---|
| `message.delta` | `reasoning` | Carries streamed output text. |
| `reasoning.available` | `reasoning` | Carries a reasoning preview. |
| `tool.started` | `tool_call` | Carries the tool name and a preview. |
| `tool.completed` | `tool_result` | Carries the tool name, duration, and error flag. |
| `approval.request` | `approval_request` | Reports a blocked tool call. |
| `approval.responded` | `approval_response` | Reports an approval response. |
| `subagent.start` | `subagent_start` | Starts one delegation tree node. |
| `subagent.complete` | `subagent_complete` | Completes one tree node with usage, cost, and duration. |
| `run.completed` | `final` | Carries output and usage. |
| `run.failed` | `error` | Carries an error message. |
| `run.cancelled` | `error` | Reports cancellation. |

The adapter preserves each Hermes `run_id` on related traces. It also preserves bounded subagent identity, lineage, status, usage, cost, duration, and file counts.

Mission Control uses these fields to render the delegation tree. A `run.steered` event remains a bounded reasoning trace.

The upstream `tool.completed` event does not include the complete tool result. The adapter can report completion, but it cannot reconstruct the missing result body.

An unknown event becomes a bounded generic reasoning trace. It does not stop the run.

Hermes sends a keepalive comment every 30 seconds. The event stream ends after a terminal event and a final stream comment.

If the stream ends early, the adapter polls the run route until it finds a terminal state.

## Run states and retention

Hermes v0.20.4 can return these active states:

- `queued`
- `running`
- `waiting_for_approval`
- `stopping`

It can return these terminal states:

- `completed`
- `failed`
- `cancelled`

Hermes v0.20.4 retains an unused event buffer for five minutes. It retains terminal run status for one hour.

A connected subscriber continues to receive events. Event-buffer expiry does not stop an active run.

The adapter activation cache and Hermes run retention must cover the expected retry period.

## Terminal data

A successful terminal response provides `output` and `usage`. A steered run can also provide `pending_steer`.

The adapter normalizes these usage fields:

- `input_tokens` or `prompt_tokens`
- `output_tokens` or `completion_tokens`
- `total_tokens`

The adapter attaches the daemon-selected model alias. The daemon calculates static cost from its pricing table when needed.

## Reconciliation

The agent saves the Hermes `run_id` in its activation record.

When the daemon retries the same activation, the adapter reads the saved run state before it creates another run.

The adapter follows this sequence after an event-stream failure:

1. Poll `GET /v1/runs/{run_id}`.
2. Accept `completed`, `failed`, or `cancelled` as terminal.
3. Continue polling an active state until the request deadline.
4. Report a failed activation when Hermes returns HTTP 404.
5. Stop the run when the request reaches its deadline.

## Cancellation

The daemon cancellation route cancels the local activation task. The adapter then sends `POST /v1/runs/{run_id}/stop`.

Hermes v0.20.4 returns `status: stopping` before the agent exits. Polling later returns `cancelled` after the agent exits.

`CANCELLATION_TIMEOUT_SECONDS` limits the stop call. A failed stop call does not change the local cancelled activation record.

## Concurrency limit

Hermes v0.20.4 limits concurrent API server runs to 10 by default. It returns HTTP 429 when the node reaches this limit.

Set `gateway.api_server.max_concurrent_runs` in the Hermes profile configuration. Set zero only when another control limits concurrency.

The agent retries explicit pre-admission 429 responses with bounded backoff. It preserves a safe upstream `Retry-After` value after its final attempt.

The agent releases the local activation claim when Hermes does not create a run. The daemon then reschedules the same activation identifier on another eligible node.

The daemon clears the node-local `previous_response_id` when it selects another node. It uses bounded attempts and returns `endpoint_rate_limited` after all candidates stay full.

## Additional APIs that bMAS uses

Hermes v0.20.4 provides these control and inventory routes.

| Method and path | bMAS feature | Current state |
|:---|:---|:---|
| `GET /v1/capabilities` | Automatic node compatibility checks | Active |
| `POST /v1/runs/{run_id}/approval` | Human approval with `once`, `session`, `always`, or `deny` | Active |
| `POST /v1/runs/{run_id}/steer` | Live guidance for an active run | Active |
| `GET /v1/skills` | Read the active profile's skills | Active and read-only |
| `GET /v1/toolsets` | Read enabled and configured toolsets | Active and read-only |
| `GET /api/sessions*` | Browse session metadata and messages | Active |
| `POST /api/sessions/{session_id}/fork` | Fork a saved session | Active |

The adapter protects each proxy route with `BMAS_EXECUTE_KEY`. It uses the configured fixed Hermes host and the selected profile prefix.

Hermes v0.20.4 advertises `admin_config_rw: false`. It provides no API server route that changes a skill or toolset.

Mission Control shows profile-aware inventory. It does not present a remote toggle that Hermes cannot apply.

The existing bMAS board steering route still changes blackboard entries. The new run steering action calls the Hermes run route.

## Long-term memory scope

Hermes v0.20.4 accepts `X-Hermes-Session-Key` on `POST /v1/runs`. This header gives a stable scope to an external memory provider.

The header is separate from the transcript `session_id`. It supports at most 256 characters and rejects control characters.

The adapter sends `bmas:<task-id>:<actor>` as this header. This value remains stable across rounds and node rescheduling without sharing memory between tasks.

The transcript `session_id` and `previous_response_id` remain separate. They continue the visible run conversation.

## Mission Control access

Mission Control calls the bMAS agent proxy. It never receives `HERMES_GATEWAY_KEY` and never calls Hermes directly.

Set `BMAS_EXECUTE_KEY` on Mission Control, the daemon, and each agent. Mission Control uses this value only in server routes.

Each configured agent URL represents one active Hermes profile. The capability response supplies its model and active profile context.

Hermes supports `/p/<profile>/` URL prefixes, but each profile can require a different `API_SERVER_KEY`. bMAS therefore does not guess profile prefixes or keys.

Configure a separate bMAS agent endpoint for each profile that operators must inspect or select.

## Node readiness checklist

Complete these checks on each Hermes node:

1. Confirm that `hermes --version` reports the reviewed release.
2. Confirm that `GET /v1/capabilities` lists all five required routes.
3. Confirm that `GET /health/detailed` reports a ready profile, model, state database, and disk.
4. Confirm that `GET /health` succeeds with the API server key.
5. Confirm that one test run returns a stable run identifier.
6. Confirm that the event route sends valid Server-Sent Events.
7. Confirm that the polling route returns the same terminal output.
8. Confirm that the stop route moves a long test run through `stopping` to `cancelled`.
9. Confirm that the model field reaches the expected model provider.
10. Confirm that tool traces stay within the configured trace size limits.
11. Confirm that the agent activation cache uses persistent storage.
12. Confirm that the Hermes concurrency limit matches the node capacity.

## Failure meanings

### Run submission fails

1. The agent returns a failed activation before it receives a run identifier.
2. The API server URL, key, model, profile prefix, or request contract is invalid.
3. Read both service logs, then test `POST /v1/runs` with a minimal reviewed request.

### Run submission returns HTTP 429

1. Hermes reached `gateway.api_server.max_concurrent_runs`.
2. The agent retries the pre-admission response with bounded backoff.
3. The daemon selects another eligible node after the agent exhausts its attempts.
4. Reduce dispatch pressure or increase the reviewed Hermes limit when all nodes remain full.

### The event stream ends early

1. The adapter switches from the stream to run polling.
2. The network closed the stream or Hermes stopped emitting events.
3. Check proxy idle timeouts and confirm that the polling route retains terminal state.

### A retry returns HTTP 409 from the agent

1. The agent has a running activation record without a saved Hermes run identifier.
2. The original submission result is uncertain, so an automatic retry can duplicate work.
3. Inspect Hermes run state and cancel the activation before a manual retry.

## Related guides

- [Hermes Node Setup](NODE_SETUP.md)
- [Execution Agent API](../agent/README.md)
- [Operations](OPERATIONS.md)
- [Hermes API Server reference](https://github.com/NousResearch/hermes-agent/blob/v2026.8.18/website/docs/user-guide/features/api-server.md)
- [Hermes profile reference](https://github.com/NousResearch/hermes-agent/blob/v2026.8.18/website/docs/user-guide/profiles.md)
