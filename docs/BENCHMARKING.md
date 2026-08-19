# Benchmarking

[Return to the documentation index](README.md).

Mission Control supports immutable datasets, versioned tests, durable runs, paired reports, human reviews, baselines, and regression gates.

## Record flow

```text
Dataset source
└── Immutable dataset version
    └── Published test revision
        ├── Runtime arms
        ├── Scorer versions
        └── Durable run
            └── Trial by arm and item
                └── Attempt and retry history
```

The system saves a checksum for each dataset version, test configuration, runtime arm, execution plan, attempt snapshot, report, baseline, and gate evaluation.

## Import a dataset

1. Open **Evaluate → Datasets**.
2. Select a UTF-8 CSV or JSONL source file.
3. Map the input and expected-output fields.
4. Map optional identifiers, subjects, splits, and tags.
5. Review validation errors before import.
6. Publish the immutable dataset version.

The importer rejects empty required values and duplicate item identifiers. A later source change creates a new version.

## Author a test

1. Open **Evaluate → Tests**.
2. Select one published dataset version.
3. Add one or more runtime arms.
4. Enter runtime configuration as JSON.
5. Select immutable scorer versions.
6. Set repetitions, concurrency, timeout, and a practical score difference.
7. Run preflight validation.
8. Publish the revision.

Preflight resolves every runtime configuration before publication. It stores a secret-free effective configuration and checksum.

Published test revisions do not change. Create a new revision when a runtime, scorer, dataset, or execution setting changes.

## Start and control a run

Select Low, Normal, High, or Urgent priority before you start a run. The scheduler uses priority only when it selects the next queued attempt.

Pause stops new admission. Resume restarts admission. Cancel removes queued work and requests safe-boundary cancellation for active tasks.

Retry creates a later attempt for each eligible failed or cancelled trial. The run keeps the earlier attempt and its evidence.

## Scheduler safety

The scheduler uses one atomic SQLite transaction for each claim. `BEGIN IMMEDIATE` lets one writer reserve the claim decision before it changes an attempt.

Each active claim has a renewable lease token and an increasing fence number. A former owner cannot attach, finish, fail, or release the attempt after ownership changes.

The daemon retains recent scheduler heartbeats for diagnosis. A new scheduler registration removes inactive records older than seven days when they own no active attempt.

The design follows SQLite's documented transaction model. Read [SQLite transaction control](https://www.sqlite.org/lang_transaction.html) for the write-lock behavior.

The current design supports multiple scheduler processes on one host. It does not support daemon workers on different hosts.

## Capacity configuration

The global limit always applies. Optional JSON maps add runtime, model, and provider limits.

```env
BMAS_BENCHMARK_MAX_ACTIVE=8
BMAS_BENCHMARK_RUNTIME_LIMITS={"classic":4,"patchboard":2,"stigmergic":2}
BMAS_BENCHMARK_MODEL_LIMITS={"starter-model":6}
BMAS_BENCHMARK_PROVIDER_LIMITS={"gemini":6}
BMAS_BENCHMARK_MODEL_PROVIDERS={"starter-model":"gemini"}
```

Open **Runtime qualifications** to inspect active claims, queued priorities, resource limits, and scheduler heartbeats.

## Human review

Open a completed attempt and expand **Human review**. Save a pass decision, a score from zero through one, and an optional note.

The review becomes immutable. The database allows one review for each attempt and reviewer identifier.

The report uses human reviews only for calibration diagnostics. A human review does not replace the saved automatic score.

## Baselines and gates

Create a baseline from one completed run. Add exact metric paths, an analysis method, an operator, and a threshold.

A gate evaluates one candidate run against the pinned baseline. It returns passed, failed, or indeterminate.

An incomplete run or unavailable metric produces an indeterminate result. It never produces a pass.

Use the command-line gate in continuous integration:

```bash
python scripts/benchmark-gate.py BASELINE_ID CANDIDATE_RUN_ID \
  --daemon-url http://localhost:9000 \
  --api-key "$BMAS_API_KEY" \
  --output benchmark-gate.json
```

## Reproducibility checklist

- Pin the dataset version and test revision.
- Keep arm configuration checksums unchanged.
- Use the same scorer versions.
- Keep the source revision and image identifiers.
- Use enough paired items for the selected practical difference.
- Read failures separately from score differences.
- Add human reviews when an automatic scorer needs calibration.
- Export the filtered CSV and preserve the report checksum.
