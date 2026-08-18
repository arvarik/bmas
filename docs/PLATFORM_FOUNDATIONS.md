# Platform Foundations

This document defines the shared platform around each coordination runtime.
The current build registers only the `classic` blackboard runtime.
The `traditional` name remains an input alias for saved tasks and old clients.

PatchBoard and stigmergic runtimes are not available in this build.

## Runtime contract

Each runtime registers one canonical identifier and one public descriptor.
The daemon publishes these descriptors through `GET /capabilities`.

Each descriptor declares these values:

- A runtime contract version.
- A configuration schema version.
- A recovery support flag.
- Required agent features.
- Event, panel, graph, control, progress, and result features.

The daemon rejects an unknown runtime during submission.
The daemon never changes an unknown runtime to `classic`.
Mission Control also shows an explicit unsupported state.

## Task lifecycle

The daemon owns the shared lifecycle around every runtime.
The runtime owns its coordination loop and its termination decision.

1. The submission route validates the objective and runtime identifier.
2. The route captures the complete effective runtime configuration.
3. SQLite saves the task and the immutable configuration.
4. The queue admits the task under global limits.
5. The worker claims a fenced task lease.
6. The registered runtime starts or resumes the task.
7. The runtime returns a `VariantOutcome` value.
8. The daemon saves the terminal result, cost, phase, and delivery event.

The daemon blocks recovery when it cannot read a saved configuration version.
A later software version can validate and retry that blocked task.

## Durable events

SQLite is the source of truth for lifecycle events.
Redis supplies a low-latency notification after SQLite accepts an event.
The outbox retries a failed Redis publication in cursor order.

Each durable event has a monotonic cursor and an idempotency key.
SSE responses include the cursor in the standard `id` field.
A client reconnects with the standard `Last-Event-ID` header.

The server returns HTTP 409 when the cursor falls outside the retained range.
The response tells the client to hydrate current state and reconnect.

Mission Control ignores a repeated or older cursor.
This rule makes duplicate delivery safe for the browser projection.

## Mission Control contract

Mission Control loads daemon capabilities before it selects a runtime adapter.
An adapter decodes events and projects hydration data into the task view.
The adapter also declares navigation, graph, progress, control, and result rules.

The browser uses one hydration request for each task refresh.
The browser batches frequent event updates into one animation frame.
These rules reduce request count and React render work during long tasks.

## Load limits

The daemon applies explicit limits at each load boundary.

- `BMAS_MAX_ACTIVE_TASKS` limits concurrent task workers.
- `BMAS_MAX_QUEUED_TASKS` limits waiting tasks.
- `BMAS_MAX_TASK_CHARS` limits objective size.
- `BMAS_AGENT_ENDPOINT_MAX_CONCURRENCY` limits calls to one agent endpoint.
- `BMAS_AGENT_ENDPOINT_WAIT_TIMEOUT_S` limits endpoint queue time.
- `BMAS_EVENT_PAYLOAD_MAX_BYTES` limits one durable event.
- `BMAS_EVENT_OUTBOX_MAX` limits pending event notifications.
- `BMAS_EVENT_OUTBOX_BATCH` limits one delivery retry batch.
- `BMAS_EVENT_REPLAY_PAGE` limits one journal replay query.
- `BMAS_BOARD_PROJECTION_CACHE_TASKS` limits cached Redis projections.

`GET /health` reports task queue, runtime, lifecycle, and event delivery state.
The service reports a degraded state when a required dependency fails.

## Extension checklist

A future runtime must complete each item before registration.

1. Define a canonical runtime identifier.
2. Define a versioned configuration schema.
3. Capture every setting that can change a task result.
4. Define migration rules for each supported saved schema.
5. Return a typed `VariantOutcome` from the runtime.
6. Use the host dispatch service for agent calls.
7. Use opaque actor identifiers in storage and events.
8. Define versioned event payloads and idempotency keys.
9. Define a Mission Control adapter for each advertised feature.
10. Add submission, execution, restart, replay, and terminal tests.
11. Add long-task load tests for the new runtime.
12. Keep the runtime unavailable until all required contracts pass.
