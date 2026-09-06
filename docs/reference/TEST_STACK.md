# The complete test stack and release gates

This document describes the complete local test stack, the unmocked
browser journey, the performance contract, the pinned toolchain, and
the release gates that make evaluation the default generation.

## The test-stack controller

`scripts/test-stack.py` starts the complete local stack in this order:

1. A temporary Redis with a test-only password.
2. The deterministic fake nested provider (`scripts/fake-provider.py`).
   It speaks the OpenAI chat-completions protocol and the Hermes runs
   contract: capabilities, detailed health, run submission, run
   status, one server-sent event stream, and run stop. Every answer
   derives from the prompt digest.
3. The daemon with a temporary SQLite database, temporary upload and
   artifact directories, and the benchmark scheduler under its
   lifecycle.
4. The real agent service pointed at the fake provider and gateway.
5. Mission Control with test authentication.

The controller allocates every port from the reserved range 43000 to
43999, generates distinct test-only API, node, execution, Redis, and
provider credentials, and writes the selected ports, paths, and
credentials location to one generated environment file. It waits for
one real readiness endpoint per service: Redis answers `PING` with
the test password, the provider and the agent answer `/health`, the
daemon answers `/health` and then `/readiness` with every check
ready, and Mission Control proxies daemon readiness through
`/api/readiness`. No readiness check reads a mocked route.

Every component writes one log under the temporary root. On stop the
controller sends cancellation to the daemon for every active run,
stops the processes in reverse order, copies every log and the
database beside the environment file as artifacts, deletes the
temporary root, and fails when a process, a bound port, or a
temporary secret survives.

```text
python3 scripts/test-stack.py start --env-file test-results/full-stack/test-env.json
python3 scripts/test-stack.py status --env-file test-results/full-stack/test-env.json
python3 scripts/test-stack.py stop --env-file test-results/full-stack/test-env.json
```

## The unmocked browser journey

`mission-control/e2e/full-stack/evaluation-journey.spec.ts` runs the
documented journey with one Playwright worker against the running
stack: import a public-style fixture through the local upload
adapter, create and edit a draft with undo and redo, publish the
governed dataset version, create a test from the classic runtime
preset with two arms after preflight, start the deterministic run,
wait for every attempt to complete through the real scheduler and
agent service, read the report with its estimand and denominators,
create a compatible baseline gate and preview it, freeze the
analysis, export the analysis-replay bundle, and reimport it with an
authenticated approval that replays the analysis to equal digests.
The browser reads the datasets, tests, runs, run detail, and
baselines pages that proxy the real daemon.

The mocked component project stays separate and parallel. The
full-stack project runs through `npm run test:e2e:full-stack`,
preserves every attempt's output, and reports a retried-then-passed
test as flaky through `flake-reporter.ts`, which fails the zero flake
budget.

The browser steps now cover the frozen report on the run page (engine,
replay verification, resolved metric definitions, denominators, and
the decision bars), the analysis history with its current and
superseded snapshots, the frozen gate rule on the baseline detail page
with its unsaved preview, and the metric definition lifecycle screen.
The mocked `e2e/evaluation-screens.spec.ts` covers the same screens
against recorded daemon responses, including the blocked frozen report
before a metric definition publishes.

The journey then exercises the operations screens in the browser:
it scores one attempt through the evaluation path and opens its score
record with the boundary, the runtime digest, and the terminal class,
opens the evidence viewer and reads the redacted path with its data
class and policy digest, freezes a second analysis snapshot, records
one reconciliation and one late charge in the resource ledger, exports
and reimports the replay bundle with an approval, revises a draft
metric definition, reads the dataset version record, registers a
judge anchor set and calibrates it now, and authors, previews, and
publishes a study before it reads the admission verdict of a run on
the study's revision. The fake provider answers the judge with prose,
so every anchor item abstains and the calibration records a failed
state with full abstention. The stack passes `BMAS_LITELLM_URL` to
the daemon so the model-backed judge reaches the fake provider. The
mocked `e2e/evaluation-operations.spec.ts` covers the same screens
against recorded daemon responses.

## The live-provider smoke

Every required group answers model calls with the deterministic fake
provider, which hides provider-specific behaviour: reasoning tokens
inside the completion budget, empty structured replies, deprecated
sampling parameters, and the material a judge needs to label an
anchor item. The optional `daemon.live-provider-smoke` group runs
`daemon/tests/test_live_provider.py` against a daemon that already
uses a real gateway, for example the compose starter after
`./scripts/bmas up`:

```bash
set -a; source .env; set +a
BMAS_LIVE_DAEMON_URL=http://127.0.0.1:9000 \
  PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/run-test-manifest.py \
  --group daemon.live-provider-smoke
```

It submits one classic task and asserts the decider produced the
answer, and it registers a four-item anchor set with inline content
and asserts the judge labels every item without abstaining. It spends
real provider budget, so it never enters a required profile.

## The performance contract

`daemon/tests/test_performance_contract.py` implements the published
table with one harness: five warmup trials and thirty measured trials
per operation, with the median, p95, maximum, peak memory, and fixture
digest recorded to `test-results/performance-contract.json`. The
`daemon.performance-contract-smoke` group runs the same harness at
smoke scale on every worker so the report structure proves on every
run. The `daemon.performance-contract` group sets
`BMAS_PERFORMANCE_WORKER=1` and enforces every limit on the pinned
Linux worker with eight logical CPUs, 16 GiB memory, and local SSD.

## The pinned toolchain

`toolchain-pins.yaml` pins Python, Node.js, npm, Playwright, the
Chromium build, Redis, SQLite, Wasmtime, NumPy, and the statistical
arithmetic contract. Wasmtime and NumPy resolve as installed Python
distributions, because the daemon executes scorer components through
the `wasmtime` wheel and the vectorized analysis engine through NumPy.
The statistics component pins `binary64-sequential-summation`: the
reference engine and the vectorized engine both honour it, so the
engine choice never changes a number.
`scripts/check-toolchain.py` resolves every component, records the
exact versions, and fails before tests when a component this
consumer requires is absent or when any resolved component violates
its pin. The daemon partition requires the Python-side components and
the Mission Control partition requires the browser-side components
after the browser install.
The Playwright pin resolves from the installed package in `mission-control/node_modules`, never through `npx`, because `npx` downloads the newest release when the package is absent and reports that version instead of the locked one. A component a partition does not require reports `unresolved` or `violates_pin` without failing that partition.

## The second implementation of the portable profiles

`mission-control/src/lib/transform-profile.ts` and
`mission-control/src/lib/analysis-rng.ts` implement `bmas-transform`
and `bmas-analysis-rng` in TypeScript. The
`mission-control.conformance-fixtures` group runs them against the
published daemon fixtures, so every digest, rank, sample, split,
candidate, sign-flip bit, and oracle aggregate has one real second
consumer that reproduces it byte for byte.

## Release gates and the default generation

`scripts/release-gates.py` maps every documented release gate to the
manifest groups that prove it and reads one complete manifest result
as read-only data. A gate is passed only when every named group
passed in that run, and the script refuses promotion until all gates
pass. `--promote` writes `conformance/release/evaluation-default.json`
with the commit, the manifest run identifier, the result digest, and
the gate report under one digest. The daemon reads that file through
`benchmarks/release.py` and reports the default generation on
`GET /api/evaluation/authority`: evaluation is the default only while
the evidence verifies and every gate passed.
