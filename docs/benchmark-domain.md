# Benchmark domain foundation

This document defines the stable records for the benchmark system. The foundation starts at database version 8. Analysis records use version 10.

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
    ├── Scorer version selection
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

The authoring preflight resolves each arm before publication. It stores a secret-free runtime envelope and a checksum. A published revision cannot change.

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

## Run execution contract

A run materializes all trials and initial attempts in one transaction. An idempotency key prevents duplicate runs after a client retry.

The scheduler applies a global active-attempt limit. Each test revision also defines its own concurrency limit.

The scheduler admits attempts through the normal task queue. Each task receives its saved runtime configuration and benchmark identifiers.

A pause stops new attempt admission. It does not stop active tasks. A resume starts admission again.

A cancellation cancels queued attempts first. It then asks active tasks to stop at their safe boundary.

Each repetition has a stable repeat index and random seed. A retry creates a new attempt with a higher retry index. The prior attempt remains available.

The scheduler recovers a claimed attempt without a task after a daemon restart. The scheduler returns that attempt to the queue.

## Scoring contract

Each scorer has an immutable identifier, kind, and version. A test revision selects exact scorer versions.

The daemon calculates numeric, multiple-choice letter, and exact text scores. It saves the extracted output, explanation, and evidence.

A failed, cancelled, or timed-out attempt receives an excluded score. The score does not convert the execution result into a zero.

Reports must use only the latest retry for each repetition. Reports must keep prior retries available for provenance.

## Comparison report contract

A run report uses the latest retry for each trial and repetition. The report keeps the count of prior retries for provenance.

Each arm reports the failure rate, score, cost, duration, and token use. Continuous metrics include the mean, median, p95, and a 95 percent estimate.

The report returns no interval bounds for one sample. This rule prevents a single result from appearing certain.

An arm comparison pairs results by the dataset item and repetition. The delta uses the right arm minus the left arm.

The report accepts subject, split, tag, and scorer filters. Mission Control stores these filters in the URL and exports the same selection as CSV.

Every report includes the dataset, test revision, execution plan, and report checksums.

## Baseline and regression gate contract

A baseline pins one completed run and one regression rule set. The database prevents updates and deletes for that baseline.

A gate compares a candidate run with the pinned run. Each rule uses one exact metric path and one operator.

The supported operators are minimum value, maximum value, maximum score drop, and maximum relative increase.

The database saves one immutable evaluation for each baseline and candidate pair. A repeated request returns the saved evaluation.

A gate returns `indeterminate` when a run is incomplete or a required metric is unavailable. An incomplete run cannot pass a gate.

Use this command in a continuous integration job:

```sh
python scripts/benchmark-gate.py BASELINE_ID CANDIDATE_RUN_ID \
  --daemon-url http://localhost:9000 \
  --api-key "$BMAS_API_KEY" \
  --output benchmark-gate.json
```

The command returns exit code `0` for passed, `1` for failed, `2` for indeterminate, and `3` for a request error.

## Runtime qualification contract

Each registered runtime publishes a versioned benchmark contract. The contract defines its configuration schema, seed behavior, repetitions, and required snapshot fields.

A static qualification checks the contract and a stable preflight checksum. It returns `provisional` because it contains no execution evidence.

A run qualification also checks a completed run and each latest attempt snapshot. It returns `passed` only when every required check passes.

Mission Control lists Patchboard and Stigmergic as planned runtimes. The daemon does not advertise them as available until their runtime adapters register.

## Current phase boundary

Mission Control authors tests and revisions. It controls durable runs and provides paired reports, immutable baselines, regression gates, and runtime qualifications.

The current scheduler uses one daemon owner. A later scale phase adds distributed ownership and durable leases for multiple daemon replicas.

Patchboard and Stigmergic still need concrete runtime adapters. Their adapters must pass a run qualification before users can compare them.
