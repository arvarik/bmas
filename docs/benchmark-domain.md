# Benchmark domain foundation

This document defines the stable records for the benchmark system. The schema starts at database version 8.

## Design goals

- Preserve each input, configuration, attempt, score, and artifact.
- Compare runtimes without adding runtime-specific columns.
- Separate cancellation from execution failure.
- Keep operator actions durable and safe to retry.
- Let the current CLI evaluator migrate into the product without losing its scorer identity.

## Record hierarchy

```text
Dataset
└── Dataset version
    └── Dataset item

Benchmark test
└── Test revision
    ├── Test arm
    └── Benchmark run
        └── Trial, one arm and one dataset item
            └── Attempt
                ├── Task
                └── Score
```

A dataset version stores a canonical checksum. A published version and its items are immutable.

A test revision points to one dataset version. Its arms define the runtimes and their opaque configuration values.

A trial binds one arm to one dataset item. An attempt records each execution or retry for that trial.

## Runtime compatibility

Each test arm uses two fields for runtime data:

- `runtime_id` identifies the execution implementation.
- `configuration` stores the runtime configuration as JSON.

The benchmark core does not interpret the configuration. The selected runtime validates the configuration against its own versioned contract.

This design supports `classic`, Patchboard, and future Stigmergic variants. A new runtime does not require a benchmark schema change.

## Execution identity

Each task stores a secret-free execution snapshot in Blackboard metadata. The snapshot includes these values:

- The runtime identifier.
- The effective runtime configuration.
- The submission overrides.
- The benchmark run, trial, and attempt identifiers when present.
- The build revision and image identifier.

The daemon calculates a SHA-256 checksum from canonical JSON. The checksum provides a stable comparison key.

The snapshot redacts keys that contain credential, password, secret, token, authorization, or API key terms.

## Dataset import contract

The registry accepts UTF-8 CSV and JSONL files. An import maps source columns into these canonical fields:

- `id`
- `input`
- `expected_output`
- `subject`
- `split`
- `tags`

The `input` and `expected_output` mappings are required. The importer rejects duplicate item identifiers and empty required values.

The registry keeps the source file and its checksum. It also stores the canonical checksum for normalized items.

## Terminal task states

The legacy task `status` field still accepts `pending`, `running`, `completed`, and `failed`. Existing consumers can continue to read that field.

The `terminal_kind` field defines the accurate terminal result:

- `completed`
- `failed`
- `cancelled`

The daemon sets `run_state` to `cancelling` before it stops execution. It sets `terminal_kind` to `cancelled` at the terminal boundary.

## Operator action contract

Mission Control sends an `X-Idempotency-Key` header for each action. A retry reuses the same key.

The daemon saves the request before it sends the action. The daemon then saves the accepted, rejected, or failed outcome.

The event journal exposes both records in the task timeline. The audit record includes the operator identity when the caller supplies `X-Operator-Id`.

## Current phase boundary

This foundation does not schedule benchmark runs. It does not calculate aggregate reports. Later phases add test authoring, durable run scheduling, scoring, comparison, and regression gates.
