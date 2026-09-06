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

## The native agent protocol and the real-process journey

The agent implements the current agent protocol in
`agent/bmas_protocol/native.py`. The daemon signs activation and
effect grants with one Ed25519 key that lives beside the database
(`daemon/src/protocol_keys.py`), so a restart keeps every issued grant
verifiable. The agent keeps its own Ed25519 seed under the activation
cache directory and registers the public key over the node-authenticated
route `POST /agent-protocol/agent-keys`. The daemon pins the key: one
key identifier never changes its bytes, and a new identifier records a
rotation. The agent publishes its capability document at
`GET /bmas/capabilities`. The daemon probes that document and, for a
task under a run-control row and a qualified endpoint, delivers one
signed grant to `POST /bmas/activations` instead of the bearer
`/execute` request. A legacy endpoint keeps the bearer path.

The agent verifies the grant under the daemon key, signs one exact
acknowledgement, stores the grant and the acknowledgement durably,
posts the acknowledgement to `POST /agent-protocol/acknowledgements`,
and only then executes. A repeated delivery of the same grant returns
the stored acknowledgement and result without a second execution,
across a restart, and `GET /bmas/acknowledgements/{grant_id}` serves
the stored acknowledgement. Every model call requests one effect
grant from `POST /agent-protocol/effect-grants`, verifies it, and posts
signed receipts to `POST /agent-protocol/receipts` for the transport
start and the observed response with the usage. The agent carries a
byte-identical copy of the daemon digest profile and signing modules,
and `scripts/tests/test_vendored_protocol.py` keeps the copies equal.

`daemon/tests/test_agent_protocol_routes.py` runs the real agent
protocol code in process against the daemon routes.
`daemon/tests/test_foundation_process_journey.py` runs the journey
across real processes. `scripts/test-stack.py start
--without-mission-control` starts Redis, the fake provider, the
daemon, and the agent. The daemon admits one run over
`POST /agent-protocol/runs`, dispatches one grant over
`POST /agent-protocol/dispatch`, and the agent executes one model call
against the fake provider under an effect grant with two receipts.
`scripts/test-stack.py restart` then stops and respawns the daemon and
the agent over the same database, signing key, and activation cache.
After the restart the run keeps its fence and projection digest, the
agent serves the same acknowledgement, the daemon treats the same
bytes as a duplicate, and a second activation dispatches under the
same fence. The `daemon.foundation-process-journey` group runs it and
feeds the `foundation_complete_stack_journey` release gate.

## Interactive admission and the Classic column with the real runtime

`daemon/src/interactive_admission.py` admits every interactive task
into one Foundation run before the runtime executes. The orchestrator
calls it after it resolves the runtime pair. The admission goes through
`run_admission.admit_run`: the exact pair, the version set of the pair
with the live database schema version, the policy set derived from the
daemon configuration in force, the asset manifest of the task's
uploaded files, the storage readiness report (cached per process), the
live qualification records when `BMAS_REQUIRE_PROVIDER_QUALIFICATION`
is set, the run budget from the task's `budget_ceiling_usd`, one
reserved reservation the activation grants bind, the journal genesis,
and the run-control row with the task fence. A legacy contract keeps
its budget permissive: the reservation records intent and the classic
ledger stays the spend authority. The writer runs only when the
`run_context`, `runtime_unit_of_work`, and `budget_reservations` gates
are on in `bmas.yaml`; with them off, the task keeps the legacy bearer
path. A prerequisite the writer rejects fails the task closed.
`BMAS_STORAGE_CONFIRMED=1` confirms an unrecognised filesystem type
for the storage readiness check. The journey route
`POST /agent-protocol/runs` uses the same writer.

The recorded seed travels as the `seed` submission override. A legacy
runtime records it in the run admission and never applies it, which
is the declared `recorded_only` value.

The legacy capability records now declare the host's compatibility
adapter for the activation ledger, the dispatch outbox, the agent
protocol, the signed acknowledgement, and the nested receipts: the
host performs the current protocol on the legacy runtime's behalf, and
the runtime itself authors no native authority record. The behavioral
suite derives those values from real behaviour: it counts the journal
records a runtime authored (proposal, evidence, goal, budget,
terminal, and invalidation operations, or any record outside host
authority) and the activation grants the host dispatched.

A native dispatch retries with one new attempt number that names the
attempt it retries, so every attempt owns its own lease, grant, and
acknowledgement. A run without a reserved reservation stays on the
legacy path. A daemon-side ledger error returns one failed turn and
never opens the endpoint circuit, because it is not an endpoint
failure.

`daemon/tests/test_behavioral_conformance_stack.py` runs the Classic
column with the real classic runtime. The test stack starts Redis, the
fake provider, the daemon, and the agent with every writer gate on.
The suite submits real tasks over `POST /submit`, aborts one through
`POST /api/tasks/{task_id}/abort`, resumes one through a real daemon
restart, and reads the durable footprint from the daemon's own
database. The `daemon.behavioral-conformance-stack` group runs it and
feeds the `foundation_shared_conformance` gate. The daemon job in
continuous integration installs `redis-server` for it.

Three stack details make that run real. The stack points every role
in the registry at its own agent process, so the classic activations
reach the stack agent instead of the deployed port. The stack sets
`LOCK_TTL_MS` to fifteen seconds, so a task lease held by a killed
daemon expires and the restarted daemon resumes the task within
seconds. The fake provider answers arithmetic from the task line
outside code fences, with the operands next to the arithmetic word,
so the board context that the agent appends as fenced JSON never
changes the answer.

## Receipts for the Hermes backends

The Hermes gateway backend runs one provider effect per run under one
daemon-issued grant, with a receipt at the transport start and one at
the observed terminal state with the usage. Hermes executes its own
tools before the agent sees them, so each observed tool event requests
one tool grant and posts its receipt with the marker
`observed_after_execution_by_hermes_gateway`. The ledger shows those
tool effects as observed after execution, never as pre-authorized. The
command-line backend runs one provider effect around the process and
reports the exit code on failure. It emits no tool events, so it
produces no tool receipts.

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
The harness measures every published limit. The scheduler decision
measurement claims 100 leases across 20 runs and enforces 100
milliseconds at p95. The cancellation measurement cancels one run
with queued work and enforces two seconds until the scheduler stops
offering that run. The report's `host` block records the machine
image digest from `BMAS_HOST_IMAGE_DIGEST`, the kernel release, and
the Python, SQLite, Redis client, Redis server
(`BMAS_REDIS_SERVER_VERSION`), NumPy, Wasmtime, Node.js, and
Playwright versions, so a number never travels without its host.
The pinned worker run itself happens in continuous integration on
that worker. A laptop run records the smoke scale only and enforces
nothing.

## The Foundation verification tests

`daemon/tests/test_foundation_verification.py` proves four
behaviours the handoff gate listed as missing. Twelve concurrent
writers on one run commit through one ordered digest chain with
distinct cursors and sequence numbers. A simulated full disk
(`sqlite3.OperationalError: database or disk is full`) at the journal
insert, the projection write, the outbox write, or the commit leaves
no partial record, and the retry commits exactly once. The scheduled
restore test in `daemon/src/restore_test.py` creates one backup,
restores it into an isolated directory, and compares the measured
recovery time and recovery point lag against the objectives
(60 seconds, zero lag). It records every outcome as a `restore_test`
backup record, so a failed restore test shows in the Recovery Center
`backup_health` queue. `BMAS_RESTORE_TEST_INTERVAL_SECONDS` above
zero starts the loop in the daemon against `BMAS_BACKUP_ROOT`. A
repeatability pair runs one case twice under one seed plan, captures
both evidence bundles with their own seed evidence, and claims no
repeatability.

## The behavioral conformance suite

`daemon/src/conformance_behavior.py` executes the Foundation services
for one runtime pair and derives every observed matrix value from what
happened. The matrix suite in `conformance_kit.py` still checks that a
record declares one value per capability. The behavioral suite proves
the value. The journal genesis carries the exact pair and commits once
under one idempotency token. The artifact store rejects a wrong digest
and an early reference and keeps promoted bytes immutable. An applied
seed changes the output, and an equal seed repeats it. A cancel stops
the next step and the run-control cancellation states advance. A stale
fence and a stale lease are rejected, and a replay from cursor zero
rebuilds the same projection digest. A native execution writes durable
activation and effect rows, and a legacy execution writes none. The
endpoint directory selects the pair's protocol partition and fails
closed. A budget refuses an over-limit reservation. Evidence and goals
reach the projection, every record carries the common envelope, and a
terminal outcome closes the run to further updates while the post
terminal invalidation still commits. The reference scorer replays the
runtime output deterministically, and the interface falls back to the
generic panels.

`daemon/src/core/variants/reference.py` is the executable reference
runtime. It registers as the pair `reference/1`, runs in process with
no agent and no provider, derives one digest per step from the seed,
checks the abort signal before every step, saves a checkpoint after
every step, and resumes from the checkpoint after a restart. It stays
out of the public capability document. The `daemon.behavioral-conformance`
group runs the suite for the reference pair and the three legacy pairs
and feeds the `foundation_shared_conformance` release gate. The suite
also proves its own teeth: a reference runtime that ignores the seed
fails the native column, and a legacy path that writes one native row
fails the ledger case.

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
`mission-control/src/lib/keyed-digest.ts` implements the keyed
digest, the semantic text transform, and the exact content digest.
`scripts/generate-keyed-digest-fixtures.py` freezes the reference
vectors in `daemon/tests/fixtures/keyed_digest.json`, and both the
daemon (`test_keyed_digest_fixtures.py`) and the same Mission Control
group reproduce every HMAC, text transform, and exact digest.

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
