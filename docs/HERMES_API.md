# Hermes Integration Contract

This document defines the Hermes calls that the Stigmergic agent adapter uses.

Hermes remains optional. The default starter calls LiteLLM directly and does not require this contract.

## Two separate APIs

| API | Common port | Stigmergic use |
|:---|:---|:---|
| Hermes Gateway | `8642` | Run submission, event streaming, polling, and cancellation |
| Hermes Dashboard | `9119` | Optional profile and skill views in Mission Control |

Do not send execution requests to the Dashboard API.

Pin and test one Hermes release before deployment. Upstream API details can change independently from this repository.

## Gateway authentication

Set `HERMES_GATEWAY_KEY` on the execution node. The adapter sends this value as an HTTP bearer token.

Do not expose the gateway to an untrusted network. A gateway can provide access to the complete configured toolset.

## Required Gateway routes

The adapter uses these routes.

| Method | Path | Purpose |
|:---|:---|:---|
| `GET` | `/health` | Checks gateway reachability. |
| `POST` | `/v1/runs` | Creates one run. |
| `GET` | `/v1/runs/{run_id}` | Reads run state and recovers after a disconnect. |
| `GET` | `/v1/runs/{run_id}/events` | Streams run events through Server-Sent Events. |
| `POST` | `/v1/runs/{run_id}/stop` | Stops a timed-out or cancelled run. |

The node is not ready for Hermes Runs API execution until each required route works with the configured key.

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
| `instructions` | Role prompt | Defines role behavior for this activation. |
| `model` | Daemon routing | Preserves the selected LiteLLM alias. |
| `session_id` | Daemon activation context | Preserves actor session state. |
| `previous_response_id` | Saved turn context | Continues state when the gateway supports it. |

The adapter accepts `run_id` or `id` in the creation response.

## Profile selection

The Runs API request does not carry a `profile` field in this adapter.

Use a gateway process that already runs the required Hermes profile. A deployment that needs several gateway profiles must provide separate profile-scoped gateway processes or another reviewed selection mechanism.

The CLI fallback passes `--profile PROFILE` for each activation.

Do not assume one gateway process changes profiles for each request.

## Event stream

The adapter parses normal Server-Sent Event records. Each record can provide an `event:` line and a JSON `data:` line.

It translates these event names:

| Hermes event | Stigmergic trace type |
|:---|:---|
| `message.delta` | `reasoning` |
| `reasoning.available` | `reasoning` |
| `tool.started` | `tool_call` |
| `tool.completed` | `tool_result` |
| `approval.request` | `approval_request` |
| `approval.responded` | `approval_request` |
| `run.completed` | `final` |
| `run.failed` | `error` |
| `run.cancelled` | `error` |

An unknown event becomes a bounded generic reasoning trace. It does not stop the run.

The event stream must end with a terminal event. If it ends early, the adapter polls the run route until it finds a terminal state.

## Terminal data

A successful terminal response should provide `output` and optional `usage`.

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
3. Continue polling a non-terminal state until the request deadline.
4. Report a failed activation when Hermes returns HTTP 404.
5. Stop the run when the request reaches its deadline.

This sequence requires the gateway to retain run state for the expected retry period.

## Cancellation

The daemon cancellation route cancels the local activation task. The adapter then sends `POST /v1/runs/{run_id}/stop`.

`CANCELLATION_TIMEOUT_SECONDS` limits the stop call. A failed stop call does not change the local cancelled activation record.

## Dashboard session authentication

Mission Control supports two optional Dashboard features:

- list and toggle skills
- read profile information

The server route reads the Dashboard HTML and extracts `window.__HERMES_SESSION_TOKEN__`. It sends that token through `X-Hermes-Session-Token`.

The integration uses these Dashboard routes:

| Method | Path | Purpose |
|:---|:---|:---|
| `GET` | `/` | Reads the current process session token. |
| `GET` | `/api/skills` | Lists skills. |
| `PUT` | `/api/skills/toggle` | Enables or disables one skill. |
| `GET` | `/api/profiles` | Reads profiles when the Dashboard provides the route. |

The token changes after a Dashboard restart. Mission Control fetches a fresh token for each proxy operation.

Treat the Dashboard feature as optional. A missing route must not block classic task execution.

## Node readiness checklist

Complete these checks on each Hermes node:

1. `GET /health` succeeds with the gateway key.
2. One test run returns a stable run identifier.
3. The event route sends valid Server-Sent Events.
4. The polling route returns the same terminal output.
5. The stop route stops a long test run.
6. The model field reaches the expected model gateway.
7. Tool traces stay within the configured trace size limits.
8. The agent activation cache uses persistent storage.

## Failure meanings

### Run submission fails

1. The agent returns a failed activation before it receives a run identifier.
2. The gateway URL, key, model, or request contract is invalid.
3. Read the agent and gateway logs, then test `POST /v1/runs` with a minimal reviewed request.

### The event stream ends early

1. The agent switches from the stream to run polling.
2. The network closed the stream or Hermes stopped emitting events.
3. Check proxy idle timeouts and confirm the polling route keeps terminal state.

### A retry returns HTTP 409 from the agent

1. The agent has a running activation record without a saved Hermes run identifier.
2. The original submission result is uncertain, so an automatic retry can duplicate work.
3. Inspect Hermes run state and cancel the activation before a manual retry.

## Related guides

- [Hermes Node Setup](NODE_SETUP.md)
- [Execution Agent API](../agent/README.md)
- [Operations](OPERATIONS.md)
