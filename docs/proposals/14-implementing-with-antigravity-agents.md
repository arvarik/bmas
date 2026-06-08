[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ UI Showcase Density](13-ui-showcase-density.md) | [➡️ Novelty & Research Directions](15-novelty-and-research-directions.md) | [🗺️ Migration Plan](10-migration-and-rollout.md)

# 14 — Implementation Runbook (Exact Prompts, In Order)

> [!ABSTRACT]
> This is a **literal copy-paste log**: every prompt to run, in order, with an explicit marker for **when to start a new agent conversation**. It is intentionally repetitive — each step is self-contained so you never have to cross-reference. Work top to bottom. The only judgment you exercise is: **merge when green, fix when red, escalate when an agent stops and asks.** The rationale (why actor≠critic, guardrails) is at the [end](#appendix-a--why-the-actorcritic-split) — you don't need it to run the steps.

---

## How to read this runbook

Each step is tagged:

- **🆕 NEW AGENT** — open a brand-new Antigravity agent conversation (via the Agent Manager or `Ctrl/Cmd+Shift+I`) and paste the prompt. Do **not** reuse a previous conversation. For implementation steps (the heavy lifts), use Antigravity's **`/goal`** slash command instead of a plain prompt — this tells the agent to be extra thorough and not stop until the goal is fully achieved.
- **♻️ RESUME Step N** — go back to the existing conversation from Step N and paste the follow-up (only used to fix that conversation's own PR). **Always paste the reviewer's specific findings** into your resume message so the implementer has full context without re-fetching.
- **🧑 YOU** — a human action (merge a PR, decide go/no-go). No prompt.

**Why new conversations each time:** the rule file you create in Step 0 is placed in `.agents/rules/` and is loaded automatically into *every* new Antigravity agent conversation, so a fresh conversation always boots with the full spec + invariants. Fresh context avoids drift and self-review bias. **The reviewer is always a different conversation than the implementer** — that is the whole safety mechanism.

**Placeholders:** replace `<PR#>` with the PR number the implementer step created (it prints it). Replace nothing else.

**Pace:** run one phase at a time, top to bottom. (Optional parallelism is called out where safe.) You can use the Agent Manager to monitor multiple conversations side-by-side when parallelism is available.

**Splitting large phases:** if an implementation agent signals it is running into complexity or context-window limits (most likely on Phases 2, 3b, or 5), split the phase into sub-branches (e.g., `feat/bb-phase-2a`, `feat/bb-phase-2b`) and run each as a separate implement → review → merge cycle, merging into the phase branch. Then open a single PR from the phase branch into `feat/true-blackboard`.

---

## Step 0 — 🆕 NEW AGENT — One-time setup

> Sets up the rule file, CI, branch protection, and confirms the agent's GitHub + node access. Run this once, first.

```
Set up this repo for the autonomous blackboard-migration build. Do all of the following, then report status:

1. Create .agents/rules/blackboard-migration.md with EXACTLY this content:
---
description: Binding rules for the true-blackboard migration
alwaysApply: true
---
# Blackboard migration — binding constraints
You implement docs/proposals/. It is a spec, not a suggestion.
## Before writing code
- Read the phase doc you're working on AND docs/proposals/10-migration-and-rollout.md.
- Read docs/proposals/11-extensibility-and-variants.md §6 (seams checklist) — MUST be honored.
- UI work: read docs/design/DESIGN.md; compose only from existing ui/ primitives + tokens.
- Agent/Hermes work: read docs/HERMES_API.md (verified live v0.15.1; gateway is up on :8642).
## Non-negotiable invariants
- Determinism boundary: LLMs PROPOSE patches; the deterministic kernel DISPOSES. (doc 04)
- Every state change is an emitted event; the board is an event log first, snapshot second.
- Never break the legacy pipeline. New behavior ships behind coordination.strategy / blackboard_v2 flags.
- Keep the SQLite dual-write. Redis is real-time; SQLite is durable truth.
- Honor the CoordinationStrategy seam: nothing in kernel/board/traces/UI may hard-code "Control Unit"
  or role names. The seams checklist is a MERGE GATE, not advice. (doc 11 §6)
## Definition of done (do NOT mark done without ALL of these)
- New deterministic code has unit tests, and they pass.
- Tests + type-check + lint pass locally (match the repo's existing tooling).
- If the phase touches Hermes/cluster behavior, you ran a LIVE verification on a node and pasted the
  actual command + output into the PR (not a description of what you expect).
- You opened a PR with gh and linked the exact doc sections you implemented.
- You did NOT review/approve your own PR.
## Escalate (STOP and ask the human) when
- You want to deviate from the spec or a seam — surface the trade-off, do not act.
- The spec is ambiguous/contradictory for the change at hand.
- A change would be destructive/irreversible on a node or to shared state, or touch secrets.
- You'd need to start a different phase to finish this one.
## Working style
- Stay strictly within the current phase's scope. One phase = one branch = one PR.
- Update the phase checklist in doc 10 as items land.
- Update docs/proposals/MIGRATION_STATUS.md with the phase, PR number, and status after opening a PR.
- Live verification uses a dedicated test namespace (task_id prefix ci-verify-*) and read-only probes;
  never mutate production state or reconfigure a node without escalation.

2. Add a GitHub Actions CI workflow that, on every PR, runs the daemon test suite + type-check + lint,
   and builds Mission Control. If tooling is missing, infer it from the repo and wire it up.
3. Create the integration branch feat/true-blackboard off main and push it.
4. Create docs/proposals/MIGRATION_STATUS.md with a tracking table:
   | Phase | Branch | PR | Status | Merged |
   Headers only, no rows yet. Each implementer step will append its row after opening a PR.
5. Verify and report: `gh auth status`; that you can `ssh root@192.168.4.103 'hostname'`; and that
   `curl -s -H "Authorization: Bearer $(ssh root@192.168.4.103 'grep ^API_SERVER_KEY ~/.hermes/.env | cut -d= -f2')" http://192.168.4.103:8642/v1/capabilities`
   returns `features.run_submission=true` (the booleans are nested under `features`, not top-level).
6. Print: whether branch protection on main requires green CI + 1 approval. If you cannot set it via gh,
   tell me the exact clicks to do it in GitHub settings.

Do not start any phase. Stop after reporting.
```

**🧑 YOU:** confirm CI runs, branch protection is on (green CI + 1 approval required to merge `main`), and Bugbot is enabled on the repo. Then proceed.

---

## Phase 0 — Foundations (config + flags + seam scaffolding)

### Step 1 — 🆕 NEW AGENT (`/goal`) — Implement Phase 0
```
Implement Phase 0 of docs/proposals/10-migration-and-rollout.md autonomously on branch
feat/bb-phase-0 (off feat/true-blackboard). Plan first — create an implementation plan artifact and
wait for my approval before writing code. Then implement:
(a) SQLite migration v2 from doc 07 (`SCHEMA_VERSION=2`, additive tables/columns, including trace/turn/board
tables needed by Phase 1); (b) the coordination.* config block (strategy/control_unit.*/stigmergic.*) +
pressure.weights with fail-fast validation per doc 11 §7, strategy default legacy_pipeline; (c) the
blackboard_v2 build flag; (d) model pricing config so daemon-side `cost_usd` can be computed from Hermes token
counts; (e) scaffold the CoordinationStrategy seam + wire the seams checklist (doc 11 §6) as a guard.
No behavior change. Add/extend tests for the config validation; run tests + type-check + lint.
Open a PR with gh into feat/true-blackboard, linking doc 10 Phase 0, doc 07, and doc 11 §2/§6, and PRINT the PR number.
Update docs/proposals/MIGRATION_STATUS.md with this phase's row.
Escalate if the existing config style makes fail-fast validation ambiguous. Do not review your own PR.
```

### Step 2 — 🆕 NEW AGENT — Independent review of Phase 0
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/10-migration-and-rollout.md (Phase 0) and the seams checklist in
docs/proposals/11-extensibility-and-variants.md §6. Run `gh pr diff <PR#>` and read ONLY the diff + the
spec; ignore the PR's self-description. Also check out the branch and run the test suite yourself — confirm
the output matches what the implementer claims. Post a PR review as a blocking checklist covering: any spec
deviation, any unimplemented definition-of-done item, any hard-coding the seam forbids, any "done" claim
not backed by pasted test output, and missing/weak tests. Do NOT fix anything. If clean, say so explicitly.
```

### Step 3 — 🧑 YOU
Read the reviewer's checklist + Bugbot. **If findings:** **♻️ RESUME Step 1** and paste: `Address the review findings on PR <PR#> and CI. The reviewer flagged: [paste the reviewer's specific findings here]. Push fixes; do not open a new PR.` Repeat Step 2 if the changes are substantial. **If clean + CI green:** merge PR `<PR#>`.

---

## Phase 1 — Agent traces over the live Runs API ⭐

### Step 4 — 🆕 NEW AGENT (`/goal`) — Implement Phase 1
```
Implement Phase 1 (agent traces) per docs/proposals/06-agent-traces.md on branch feat/bb-phase-1.
The Runs API gateway is ALREADY live on all 3 nodes (:8642; key in ~/.hermes/.env). Plan first — create an
implementation plan artifact and wait for my approval before writing code. Then:
rewrite agent/api_server.py to drive a run via POST /v1/runs + consume GET /v1/runs/{id}/events (SSE)
instead of hermes -z, capturing doc 06's trace schema; keep hermes -z as the documented fallback
(doc 06 §8); ingest traces → Redis + SQLite (doc 07); capture cost per-task/per-model/per-node + the
joules_estimate hook (doc 10 Phase 1). Ships behind a flag; must NOT require the board rewrite.
VERIFY LIVE: ssh to 192.168.4.103, submit a ci-verify-* task through :8642 with the bearer key, and paste
the real SSE events + the populated usage payload (this also closes Q2) into the PR. Remember
`/v1/capabilities` booleans are under `features.*`, and `usage` contains tokens only, not cost. Add tests for the event
parsing; run them. Open a PR with gh into feat/true-blackboard linking doc 06/07, and PRINT the PR number.
Update docs/proposals/MIGRATION_STATUS.md with this phase's row.
Do not review your own PR. Escalate per the rule if the live payload shape differs from doc 06.
```

### Step 5 — 🆕 NEW AGENT — Independent review of Phase 1
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/06-agent-traces.md + 07-data-model.md and the seams checklist (doc 11 §6). Run
`gh pr diff <PR#>` and read ONLY the diff + spec; ignore the PR's self-description. Also check out the
branch and run the test suite yourself — confirm the output matches what the implementer claims. Blocking
checklist: spec deviations, missing definition-of-done items, broken legacy path / missing flag, any "done"
claim not backed by the pasted live SSE + usage output, weak tests. Do NOT fix anything. If clean, say so
explicitly.
```

### Step 6 — 🆕 NEW AGENT — Independent live verification
```
You are an INDEPENDENT live verifier. SSH to 192.168.4.103, submit a fresh ci-verify-* task through the
endpoint PR <PR#> implements, and capture the ACTUAL output. Confirm the trace events + usage payload match
doc 06's schema and that cost is captured per-task/per-model/per-node. Post pass/fail on PR <PR#> with the
raw command + output. Use only the ci-verify-* namespace; make no config changes. Do not edit the code.
```

### Step 7 — 🧑 YOU
**If findings:** **♻️ RESUME Step 4** → `Address the review + verifier findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run Steps 5–6 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase E — Evaluation / A-B harness (start now; may overlap later phases)

### Step 8 — 🆕 NEW AGENT (`/goal`) — Implement Phase E
```
Implement docs/proposals/10-migration-and-rollout.md "Phase E" on branch feat/bb-eval. Plan first — create
an implementation plan artifact and wait for my approval before writing code. Then build:
the benchmark runner (GSM8K + an MMLU subset) that submits through bMAS and scores accuracy; per-run metrics
capture (accuracy/$/tokens/latency/rounds/consensus%/joules_estimate); the A/B harness that swaps ONLY
coordination.strategy and emits a side-by-side report; and failure-injection tooling (hook) for the
kill-a-node experiment. Add tests for the scorer; run them. Open a PR with gh into feat/true-blackboard
linking doc 10 Phase E + doc 15, and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md.
Do not review your own PR.
```

### Step 9 — 🆕 NEW AGENT — Independent review of Phase E
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/10-migration-and-rollout.md "Phase E" and doc 15. `gh pr diff <PR#>`, read ONLY diff + spec.
Also check out the branch and run the test suite yourself — confirm the output matches what the implementer
claims. Blocking checklist: does the scorer actually score correctly (check the tests), are all metrics
captured, does the A/B harness change ONLY the strategy. Do NOT fix anything. If clean, say so explicitly.
```

### Step 10 — 🧑 YOU
**If findings:** **♻️ RESUME Step 8** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge `<PR#>`.

---

## Phase 2 — PatchBoard kernel + fork-capable event log

### Step 11 — 🆕 NEW AGENT (`/goal`) — Implement Phase 2
```
Implement Phase 2 behind blackboard_v2 per docs/proposals/04-blackboard-protocol.md + 07-data-model.md on
branch feat/bb-phase-2. Plan first — create an implementation plan artifact and wait for my approval before
writing code. This is a large phase; if you hit context-window limits, escalate and we will split into
sub-branches. Build the deterministic kernel (JSON-Patch RFC 6902 validation, entry schema, optimistic
concurrency rev+CAS, salience/decay, Redis v2 layout, event emission on every commit). The kernel is
pure/deterministic and MUST NOT hard-code roles or "control unit" (doc 11 §6). The event log MUST support
fork-from-event, not just linear replay. Heavy unit tests: patch validation, concurrent CAS (property-test),
event emission, and a replay+fork test that re-materializes board state to event N. Run the suite and paste
the output in the PR. Open a PR with gh into feat/true-blackboard linking doc 04/07/11 §6, and PRINT the PR
number. Update docs/proposals/MIGRATION_STATUS.md. Escalate before changing the Redis deployment topology.
Do not review your own PR.
```

### Step 12 — 🆕 NEW AGENT — Independent review of Phase 2
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/04-blackboard-protocol.md + 07-data-model.md and the seams checklist (doc 11 §6).
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite yourself —
confirm the output matches what the implementer claims. Blocking checklist: determinism boundary intact, NO
hard-coded roles/"control unit" in the kernel, fork-from-event implemented (not just linear replay), CAS +
concurrency tested, "done" claims backed by pasted test output. Do NOT fix anything. If clean, say so
explicitly.
```

### Step 13 — 🆕 NEW AGENT — Independent verification (replay/fork)
```
You are an INDEPENDENT verifier. Check PR <PR#>'s replay/fork claim objectively: run the kernel's
replay+fork test and a daemon kill/restart-mid-task scenario; confirm the board re-materializes from the
event log without corruption and that fork-from-event works. Post pass/fail with raw output on PR <PR#>.
Do not edit the code.
```

### Step 14 — 🧑 YOU
**If findings:** **♻️ RESUME Step 11** → `Address review + verifier findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 12–13 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 3a — Author + deploy the Hermes profiles

### Step 15 — 🆕 NEW AGENT (`/goal`) — Implement + deploy profiles
```
Per docs/proposals/12-hermes-and-node-topology.md §2.5–3, on branch feat/bb-phase-3a: plan first — create an
implementation plan artifact and wait for my approval before writing code. Then author the profile set
(planner, expert, critic, conflict_resolver, cleaner, decider, universal). Each = SOUL.md (role identity) +
config.yaml (toolset scoping per doc 12). Experts share ONE expert profile (domain via per-task AGENTS.md).
The CU scheduler is NOT a profile (doc 12 §2.1). Commit to agent/profiles/. Then choose and verify the
profile-aware dispatch mechanism from doc 12: per-profile gateways/ports, a local `hermes --profile` bridge,
or a newly verified upstream selector. Add the role→(preferred_host, profile, dispatch_endpoint) registry to
bmas.yaml/config.py (home + any-host fallback). DEPLOY + VERIFY LIVE: replicate profiles to all 3 nodes and
confirm each role can actually execute through the chosen dispatch path. Paste `hermes profile list` and the
profile-scoped run/model evidence from each node. Open a PR with gh into feat/true-blackboard linking doc 12, and
PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md. Escalate before overwriting any existing
node config that isn't profile-related. Do not review your own PR.
```

### Step 16 — 🆕 NEW AGENT — Independent review of Phase 3a
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 12 §2.5–3.
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run any tests yourself. Blocking
checklist: 7 profiles present with correct toolset scoping, experts share ONE profile, CU is NOT a profile,
registry has home + fallback + dispatch_endpoint, and profile-aware dispatch is verified rather than assumed.
Do NOT fix. If clean, say so explicitly.
```

### Step 17 — 🆕 NEW AGENT — Independent live verification of profiles
```
You are an INDEPENDENT verifier. SSH to each of 192.168.4.103/.112/.122 and confirm every profile from PR
<PR#> is installed and can execute through the chosen profile-aware dispatch mechanism. Paste `hermes profile
list` plus a profile-scoped run/model proof per node. Do not accept a default-profile `/v1/runs` call as
profile verification unless the PR explicitly implements per-profile gateways/ports for that call. Post pass/fail
on PR <PR#> with raw output. Make no config changes beyond what the PR intends. Do not edit code.
```

### Step 18 — 🧑 YOU
**If findings:** **♻️ RESUME Step 15** → `Address findings on PR <PR#>. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 16–17 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 3b — Control Unit / CoordinationStrategy

### Step 19 — 🆕 NEW AGENT (`/goal`) — Implement Phase 3b
```
Implement Phase 3 (V1 ControlUnitStrategy) per docs/proposals/05-control-unit.md + doc 11 §2 on branch
feat/bb-phase-3b. Plan first — create an implementation plan artifact and wait for my approval before
writing code. This is a large phase; if you hit context-window limits, escalate and we will split into
sub-branches. Create daemon/src/core/coordination.py (CoordinationStrategy interface +
ControlUnitStrategy) and control_unit.py (OODA loop that READS THE PRESSURE FIELD, two-tier DECIDE,
consensus scorer). The deterministic scheduler stays in the daemon; only the Decider + rare LLM-escalation
are model calls. Add role personas + capability profiles in personas.py. Dispatch via the Phase-3a registry,
load-balancing experts one-per-host. Cost rails in the SAME PR (budget ceiling, round/duration/concurrency
caps, doc 05 §5). Tests for the scheduler against an in-memory board (no LLM); run them. VERIFY LIVE
end-to-end: run a ci-verify-* task and paste evidence that one agent critiques ANOTHER's finding unprompted
and the CU halts on consensus (not a fixed pipeline). control_unit only behind the flag; legacy default.
Open a PR with gh into feat/true-blackboard linking doc 05/11 §2, and PRINT the PR number. Update
docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 20 — 🆕 NEW AGENT — Independent review of Phase 3b
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 05 + doc 11 §2 and the
seams checklist (doc 11 §6). `gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run
the test suite yourself — confirm the output matches what the implementer claims. Blocking checklist:
scheduler is deterministic + in the daemon, reads the pressure field (not ad-hoc ifs), NO hard-coded
"Control Unit" in kernel/board, cost rails present, legacy stays default. Do NOT fix. If clean, say so
explicitly.
```

### Step 21 — 🆕 NEW AGENT — Independent live verification of Phase 3b
```
You are an INDEPENDENT verifier. Run a fresh ci-verify-* task with coordination.strategy=control_unit and
confirm objectively: (a) one agent reads + critiques ANOTHER agent's finding without the daemon dictating it,
and (b) the CU terminates on a consensus threshold, not a fixed pipeline length. Post pass/fail on PR <PR#>
with raw evidence (the trace/board excerpt). Use only the ci-verify-* namespace. Do not edit code.
```

### Step 22 — 🧑 YOU
**If findings:** **♻️ RESUME Step 19** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 20–21 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 3c — (OPTIONAL) Coordinator narration lane

> Skip unless you want the OODA loop visible in the showcase.

### Step 23 — 🆕 NEW AGENT (`/goal`) — Implement Phase 3c
```
Optional, per docs/proposals/05-control-unit.md §1.1, branch feat/bb-phase-3c. Plan first — create an
implementation plan artifact and wait for my approval before writing code. Route the CU's ESCALATION path
through a thin coordinator profile (capture rationale + chosen roles as a normal trace), gated by
coordination.control_unit.coordinator_narration (default false). HARD CONSTRAINTS: off the critical path
(deterministic fallback on timeout/error), escalation-turns only, never owns control. Render a Coordinator
lane (doc 13). Test the fallback path explicitly (a coordinator stall must NOT block the loop); run tests.
Open a PR with gh into feat/true-blackboard linking doc 05 §1.1 + doc 13, and PRINT the PR number. Update
docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 24 — 🆕 NEW AGENT — Independent review of Phase 3c
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 05 §1.1. `gh pr diff
<PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite yourself. Blocking checklist:
default false, off the critical path with a tested deterministic fallback, escalation-turns only, does NOT
own control. Do NOT fix. If clean, say so explicitly.
```

### Step 25 — 🧑 YOU
**If findings:** **♻️ RESUME Step 23** → `Address findings on PR <PR#>. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge `<PR#>`.

---

## Phase 4 — Blackboard visualization + trace inspector

> Optional parallelism: Phase 4 touches `mission-control/` only, so Steps 26–29 may run concurrently with Phases 2–3 (different files). **However, Phase 4 should not be verified or merged until Phase 1 (traces) and Phase 2 (kernel) are merged** — the UI needs real trace data and board entries to test against. Stubbed/mock data is acceptable during coding; the VERIFY step must use a live task. If you'd rather stay strictly linear, just run it here. Use the Agent Manager to run parallel conversations side-by-side.

### Step 26 — 🆕 NEW AGENT (`/goal`) — Implement Phase 4
```
Implement Phase 4 per docs/proposals/08-ui-blackboard-visualization.md + 09-ui-agent-trace-inspector.md in
doc 13's showcase philosophy, branch feat/bb-phase-4. Plan first — create an implementation plan artifact
and wait for my approval before writing code. Build BlackboardGraph (React Flow), WorkerLane, ConsensusMeter,
the trace timeline + turn inspector, and the linear replay scrubber on the Phase-2 fork-capable log. Compose
ONLY from existing ui/ primitives + DESIGN.md tokens (zero hardcoded values); add agent-role tokens first.
Use existing SSE plumbing with batching. VERIFY: build passes, and run against a live task so the graph
animates as patches land — attach a screenshot/recording to the PR. Open a PR with gh into
feat/true-blackboard linking doc 08/09/13, and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md.
Do not review your own PR.
```

### Step 27 — 🆕 NEW AGENT — Independent review of Phase 4
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 08/09/13 and DESIGN.md.
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and build Mission Control yourself.
Blocking checklist: composes only from ui/ primitives + tokens (NO hardcoded colors/spacing), uses existing
SSE plumbing, agent-identity colors honored, build passes, animation evidence attached. Do NOT fix. If clean,
say so explicitly.
```

### Step 28 — 🆕 NEW AGENT — Independent verification of Phase 4
```
You are an INDEPENDENT verifier. Build Mission Control from PR <PR#> and run it against a live ci-verify-*
task; confirm the blackboard graph renders and animates as patches land and the trace timeline populates.
Post pass/fail on PR <PR#> with a screenshot/recording. Do not edit code.
```

### Step 29 — 🧑 YOU
**If findings:** **♻️ RESUME Step 26** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 27–28 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 5 — Advanced features + cutover

### Step 30 — 🆕 NEW AGENT (`/goal`) — Implement Phase 5
```
Implement Phase 5 per docs/proposals/10-migration-and-rollout.md on branch feat/bb-phase-5. Plan first —
create an implementation plan artifact and wait for my approval before writing code. This is a large phase;
if you hit context-window limits, escalate and we will split into sub-branches. Then implement: native HITL
via run approvals (doc 12 §5.1), stateful turns via the Responses API (doc 12 §5.2), private sub-boards +
Conflict-Resolver (doc 05 §4), and the Mission cockpit (doc 13). Do the CUTOVER (flip coordination.strategy
default to control_unit) ONLY after doc 10 §6 + README §4 all pass AND the Phase-E benchmark shows
control_unit ≥ legacy on the eval set — run that A/B and paste the numbers in the PR. Keep legacy as a
selectable fallback strategy. Open a PR with gh into feat/true-blackboard linking doc 10 §5–6 + doc 12 §5,
and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 31 — 🆕 NEW AGENT — Independent review of Phase 5
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 10 §5–6 + doc 12 §5.
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite yourself —
confirm the output matches what the implementer claims. Blocking checklist: legacy still selectable after
cutover, HITL approval + Responses API wired per doc 12, the A/B benchmark numbers are pasted AND show
control_unit ≥ legacy, backward-compat contract updated. Do NOT fix. If clean, say so explicitly.
```

### Step 32 — 🆕 NEW AGENT — Independent live verification of cutover
```
You are an INDEPENDENT verifier. Confirm objectively for PR <PR#>: run the Phase-E A/B (legacy vs
control_unit) on the eval set and paste the metrics; exercise a run-approval HITL gate end-to-end on a node;
confirm legacy_pipeline still works when selected. Post pass/fail on PR <PR#> with raw numbers/output.
Do not edit code.
```

### Step 33 — 🧑 YOU
**If findings:** **♻️ RESUME Step 30** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 31–32 if needed. **If clean + CI green + benchmark gate met:** merge `<PR#>`, then merge `feat/true-blackboard` → `main`. **V1 is shipped.**

---

## Phase 6 — (FUTURE, SEPARATE) Pure-stigmergic variant

> Only after V1 is stable. This is where the novelty lives ([doc 15](15-novelty-and-research-directions.md)).

### Step 34 — 🆕 NEW AGENT (`/goal`) — Implement V2
```
Implement the StigmergicStrategy against the UNCHANGED substrate per docs/proposals/11-extensibility-and-
variants.md §4 on branch feat/bb-v2-stigmergic. Plan first — create an implementation plan artifact and wait
for my approval before writing code. Then implement: roleless universal actors, exponential pheromone decay,
parallel patch competition, basin-based termination, and pull-mode self-activation via Hermes crons on the
pressure field (doc 12 §6; crons are CLI-managed per Q9, provision via SSH/CLI not HTTP). It MUST run on the
same kernel/board/traces/UI with coordination.strategy: stigmergic. If it does NOT, the seams checklist
(doc 11 §6) was violated earlier — find and fix the seam, do not special-case. Tests for decay + termination.
Open a PR with gh linking doc 11 §4 + doc 15, and PRINT the PR number. Update
docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 35 — 🆕 NEW AGENT — Independent review of V2
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 11 §4 + the seams
checklist §6. `gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite
yourself. Blocking checklist: NO changes to the kernel/board/traces/UI were needed (if they were, a seam was
violated — flag it), decay + basin termination implemented, runs purely on coordination.strategy=stigmergic.
Do NOT fix. If clean, say so explicitly.
```

### Step 36 — 🆕 NEW AGENT — Run the novelty experiments
```
You are an INDEPENDENT experimenter. Using the Phase-E harness, run on a node cluster: (1) the V1-vs-V2 A/B
on the eval set; (2) the kill-a-node resilience experiment (partition a node mid-task; measure degradation
for control_unit vs stigmergic). Capture all metrics (accuracy/$/latency/rounds + recovery behavior) and post
a results summary on PR <PR#>. Use only the ci-verify-* namespace. Do not edit code.
```

### Step 37 — 🧑 YOU
**If findings:** **♻️ RESUME Step 34** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge. You now have the novel artifact + measured results for the showcase.

---

## Quick reference: the run map

| Phase | Implement (🆕) | Review (🆕) | Verify (🆕) | You (🧑) |
|:--|:--|:--|:--|:--|
| 0 Foundations | 1 | 2 | — | 3 |
| 1 Traces ⭐ | 4 | 5 | 6 | 7 |
| E Eval/A-B | 8 | 9 | — | 10 |
| 2 Kernel | 11 | 12 | 13 | 14 |
| 3a Profiles | 15 | 16 | 17 | 18 |
| 3b Control Unit | 19 | 20 | 21 | 22 |
| 3c Coordinator (opt) | 23 | 24 | — | 25 |
| 4 Visualization | 26 | 27 | 28 | 29 |
| 5 Cutover | 30 | 31 | 32 | 33 |
| 6 Stigmergic V2 (future) | 34 | 35 | 36 | 37 |

Every numbered cell is its **own new agent conversation**, except the 🧑 column (you) and the resume-to-fix follow-ups noted in each "YOU" step. Use the Agent Manager to monitor conversations in parallel when safe.

---

## Appendix A — Why the actor/critic split

The implement step and the review/verify steps are deliberately **different agent conversations** because a single agent that both writes and judges its own work drifts toward approving it (self-review bias), and that error compounds across phases. Independent critics — a fresh Reviewer conversation (sees only spec + diff), an independent Verifier (objective output from a node), CI, and Bugbot — decide "done" with evidence, not the author's confidence. You are the cheap final gate: because the critics did the real work, your merge click is low-effort and high-trust. Resuming the implementer conversation to *fix* its own PR is fine; letting it *approve* its own PR is not.

## Appendix B — When you must step in (escalation)

Agents are told to STOP and ask you when: they'd deviate from the spec or a seam, the spec is ambiguous/contradictory, a change would be destructive on a node or touch secrets, or finishing a phase would require starting another. Everything else runs unattended. You also raise budget/safety caps if a phase requests it.
