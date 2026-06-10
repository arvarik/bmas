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
- Read docs/proposals/03-target-architecture.md §6 (the variant seam + seams checklist) — MUST be honored.
- UI work: read docs/design/DESIGN.md; compose only from existing ui/ primitives + tokens.
- Agent/Hermes work: read docs/HERMES_API.md (verified live v0.15.1; gateway is up on :8642).
## Non-negotiable invariants
- Determinism boundary: agents PROPOSE entries; the deterministic Board Gateway DISPOSES (validate →
  authorize → commit → emit). Only the gateway writes board state. (doc 04)
- Every state change is an emitted event; the board is an event log first, snapshot second. (doc 04 §5)
- The core is the NATURAL-LANGUAGE LbMAS (Han & Zhang 2025). Do not introduce JSON-Patch/schema-mutation
  into the core — that is the PatchBoard VARIANT (doc 11), built later behind the same seam.
- Never break the legacy pipeline. New behavior ships behind coordination.variant / blackboard_v2 flags.
- Keep the SQLite dual-write. Redis is real-time; SQLite is durable truth (SQLite-first ordering, doc 04 §5.1).
- Honor the CoordinationVariant seam: nothing in gateway/board/traces/UI shell may hard-code "Control Unit"
  or role names. Authors are opaque strings; auth is capability-based. The seams checklist (doc 03 §6)
  is a MERGE GATE, not advice.
- All node↔daemon HTTP surfaces present the BMAS_NODE_KEY bearer secret. Nodes never get Redis credentials.
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

> [!NOTE] Branch-protection reality check (one GitHub identity)
> If every agent conversation pushes and reviews through the **same** GitHub account, GitHub will not count the reviewer agent's review as the required approval — an author cannot approve their own PR, and a same-account review is indistinguishable from the author's. Two valid resolutions: (a) provision a **second bot identity** (separate token) that the reviewer/verifier steps use, or (b) accept that the "1 approval" gate is satisfied by **you** at merge time — the reviewer agent's blocking checklist is the substance; your approval click is the formality. Pick one in Step 0 and stick to it. Do **not** let an agent "fix" a blocked merge by weakening branch protection (the rule file's escalation clause covers this).

---

## Phase 0 — Foundations (config + flags + seam scaffolding)

### Step 1 — 🆕 NEW AGENT (`/goal`) — Implement Phase 0
```
Implement Phase 0 of docs/proposals/10-migration-and-rollout.md autonomously on branch
feat/bb-phase-0 (off feat/true-blackboard). Plan first — create an implementation plan artifact and
wait for my approval before writing code. Then implement:
(a) SQLite migration v2 from doc 07 (`SCHEMA_VERSION=2`, additive tables/columns: board_entries,
board_events, agent_traces, turns, task_files, artifacts, plus tasks/cost_entries columns);
(b) the coordination.* config block (variant / view_budget_tokens / round_execution / the complete
traditional.* key set from doc 05 §3) with fail-fast validation, variant default legacy_pipeline;
(c) the storage.* config block from doc 17 §2 (user_media_dir, artifacts_dir, caps, allowed types,
pdf_extraction) with startup directory-writability checks; (d) the blackboard_v2 build flag;
(e) the BMAS_NODE_KEY shared bearer secret (doc 03 §4) — fail-fast if unset; (f) model pricing config so
daemon-side cost_usd can be computed from Hermes token counts; (g) scaffold the CoordinationVariant seam
(daemon/src/core/variants/__init__.py per doc 03 §6) and wire the seams checklist as a guard.
No behavior change. Add/extend tests for the config validation; run tests + type-check + lint.
Open a PR with gh into feat/true-blackboard, linking doc 10 Phase 0, doc 07, doc 17 §2, and doc 03 §6,
and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md with this phase's row.
Escalate if the existing config style makes fail-fast validation ambiguous. Do not review your own PR.
```

### Step 2 — 🆕 NEW AGENT — Independent review of Phase 0
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/10-migration-and-rollout.md (Phase 0) and the seams checklist in
docs/proposals/03-target-architecture.md §6. Run `gh pr diff <PR#>` and read ONLY the diff + the
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
instead of hermes -z, capturing doc 06's trace schema and returning the TaskResponse v2 fields
(entries, usage, trace_count, artifacts — doc 06 §3.1); keep hermes -z as the documented fallback
(doc 06 §8); ingest traces → Redis + SQLite (doc 07); capture cost per-task/per-model/per-node with
cost_usd computed DAEMON-SIDE + the joules_estimate hook (doc 10 Phase 1). Ships behind a flag; must NOT
require the board rewrite.
VERIFY LIVE: ssh to 192.168.4.103, submit a ci-verify-* task through :8642 with the bearer key, and paste
the real SSE events + the populated usage payload (this also closes Q2) into the PR. Remember
`/v1/capabilities` booleans are under `features.*`, and `usage` contains tokens only, not cost. Add tests for
the event parsing; run them. Open a PR with gh into feat/true-blackboard linking doc 06/07, and PRINT the
PR number. Update docs/proposals/MIGRATION_STATUS.md with this phase's row.
Do not review your own PR. Escalate per the rule if the live payload shape differs from doc 06.
```

### Step 5 — 🆕 NEW AGENT — Independent review of Phase 1
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/06-agent-traces.md + 07-data-model.md and the seams checklist (doc 03 §6). Run
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
doc 06's schema and that cost is captured per-task/per-model/per-node with cost_usd computed by the daemon.
Post pass/fail on PR <PR#> with the raw command + output. Use only the ci-verify-* namespace; make no config
changes. Do not edit the code.
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
capture (accuracy/$/tokens/latency/rounds/terminated_by/joules_estimate); the A/B harness that swaps ONLY
coordination.variant and emits a side-by-side report; and failure-injection tooling (hook) for the
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
captured, does the A/B harness change ONLY the variant. Do NOT fix anything. If clean, say so explicitly.
```

### Step 10 — 🧑 YOU
**If findings:** **♻️ RESUME Step 8** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge `<PR#>`.

---

## Phase 2 — Board Gateway + fork-capable event log

### Step 11 — 🆕 NEW AGENT (`/goal`) — Implement Phase 2
```
Implement Phase 2 behind blackboard_v2 per docs/proposals/04-blackboard-protocol.md + 07-data-model.md on
branch feat/bb-phase-2. Plan first — create an implementation plan artifact and wait for my approval before
writing code. This is a large phase; if you hit context-window limits, escalate and we will split into
sub-branches. Build the entry envelope model (typed envelopes, natural-language bodies — doc 04 §1) and the
Board Gateway (doc 04 §4: normalize → validate envelope → capability-based authorization → commit → emit;
remove/set_status/set_meta; the pluggable recompute_derived hook computing salience; the free-text
envelope_fallback wrap from doc 04 §3). The gateway is deterministic and MUST NOT hard-code roles or
"control unit" (doc 03 §6). NO JSON-Patch, NO schemas-per-entry-payload, NO per-entry CAS — that is the
PatchBoard variant (doc 11), not this phase. Implement the event log with the durability contract (doc 04
§5.1: task-local seq, SQLite-first ordering, recovery rules) and fork-from-event (doc 04 §5.2), the Redis v2
layout (doc 04 §8), and SSE emission (board_entry / entry_removed / entry_status_changed / entry_rejected).
Heavy unit tests with an in-memory fake (no LLM, no Redis): envelope validation, capability rejection,
fallback wrapping, salience recompute, event emission, and a replay+fork test that re-materializes board
state to event N. Run the suite and paste the output in the PR. Open a PR with gh into feat/true-blackboard
linking doc 04/07 + doc 03 §6, and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md.
Escalate before changing the Redis deployment topology. Do not review your own PR.
```

### Step 12 — 🆕 NEW AGENT — Independent review of Phase 2
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/04-blackboard-protocol.md + 07-data-model.md and the seams checklist (doc 03 §6).
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite yourself —
confirm the output matches what the implementer claims. Blocking checklist: determinism boundary intact
(only the gateway writes), NO hard-coded roles/"control unit" in gateway/board, NO JSON-Patch machinery
smuggled into the core, capability-based auth (not role names), the durability contract + fork-from-event
implemented (not just linear replay), envelope_fallback tested, "done" claims backed by pasted test output.
Do NOT fix anything. If clean, say so explicitly.
```

### Step 13 — 🆕 NEW AGENT — Independent verification (replay/fork)
```
You are an INDEPENDENT verifier. Check PR <PR#>'s replay/fork claim objectively: run the gateway's
replay+fork test and a daemon kill/restart-mid-task scenario; confirm the board re-materializes from the
event log without corruption and that fork-from-event works. Post pass/fail with raw output on PR <PR#>.
Do not edit the code.
```

### Step 14 — 🧑 YOU
**If findings:** **♻️ RESUME Step 11** → `Address review + verifier findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 12–13 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 2F — Files & artifacts

> Optional parallelism: the upload half is independent of Phase 2 (different files); the artifact-sync half needs the merged Phase-1 `api_server.py`. If running parallel conversations, keep this on its own branch.

### Step 15 — 🆕 NEW AGENT (`/goal`) — Implement Phase 2F
```
Implement the file/artifact pipeline per docs/proposals/17-files-and-artifacts.md on branch feat/bb-phase-2f.
Plan first — create an implementation plan artifact and wait for my approval before writing code. Then build:
(a) the upload pipeline — POST /tasks/{id}/files route + Mission Control attach control + validation
(size/type caps) + PDF text extraction via pymupdf + task_files rows, storing under
storage.user_media_dir/{task_id}/ (doc 17 §3); (b) attachment board entries at genesis + node staging into
/opt/bmas-workspace/{task_id}/inputs/ with BMAS_NODE_KEY-authenticated fetch (doc 17 §4–5); (c) artifact
sync — agent outputs/ → POST /ingest/artifacts → storage.artifacts_dir/{task_slug}/ + artifacts rows +
artifact board entries, with sha256, versioning, and path-traversal rejection (doc 17 §6–7); (d) UI —
attachments rail + artifact browser + download proxies (doc 17 §8). Add tests: upload validation, extraction
caps, traversal rejection, artifact versioning. VERIFY LIVE: submit a ci-verify-* task with a real PDF
attached and a task that writes files; paste the extraction excerpt and the resulting artifact tree under
storage.artifacts_dir. Open a PR with gh into feat/true-blackboard linking doc 17, and PRINT the PR number.
Update docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 16 — 🆕 NEW AGENT — Independent review of Phase 2F
```
You are an INDEPENDENT reviewer; you did not write this code. Review PR <PR#> against
docs/proposals/17-files-and-artifacts.md. `gh pr diff <PR#>`, read ONLY diff + spec. Also check out the
branch and run the test suite yourself. Blocking checklist: size/type validation enforced server-side,
extraction caps honored, path traversal tested + rejected, artifacts land under storage.artifacts_dir with
sha256 + versioning, attachment/artifact board entries created, BMAS_NODE_KEY on every node-facing route,
live PDF + artifact evidence pasted. Do NOT fix anything. If clean, say so explicitly.
```

### Step 17 — 🧑 YOU
**If findings:** **♻️ RESUME Step 15** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge `<PR#>`.

---

## Phase 3a — Author + deploy the Hermes profiles

### Step 18 — 🆕 NEW AGENT (`/goal`) — Implement + deploy profiles
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
profile-scoped run/model evidence from each node. Open a PR with gh into feat/true-blackboard linking doc 12,
and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md. Escalate before overwriting any existing
node config that isn't profile-related. Do not review your own PR.
```

### Step 19 — 🆕 NEW AGENT — Independent review of Phase 3a
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 12 §2.5–3.
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run any tests yourself. Blocking
checklist: 7 profiles present with correct toolset scoping, experts share ONE profile, CU is NOT a profile,
registry has home + fallback + dispatch_endpoint, and profile-aware dispatch is verified rather than assumed.
Do NOT fix. If clean, say so explicitly.
```

### Step 20 — 🆕 NEW AGENT — Independent live verification of profiles
```
You are an INDEPENDENT verifier. SSH to each of 192.168.4.103/.112/.122 and confirm every profile from PR
<PR#> is installed and can execute through the chosen profile-aware dispatch mechanism. Paste `hermes profile
list` plus a profile-scoped run/model proof per node. Do not accept a default-profile `/v1/runs` call as
profile verification unless the PR explicitly implements per-profile gateways/ports for that call. Post
pass/fail on PR <PR#> with raw output. Make no config changes beyond what the PR intends. Do not edit code.
```

### Step 21 — 🧑 YOU
**If findings:** **♻️ RESUME Step 18** → `Address findings on PR <PR#>. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 19–20 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 3b — The TraditionalVariant (Control Unit + cycle)

### Step 22 — 🆕 NEW AGENT (`/goal`) — Implement Phase 3b
```
Implement Phase 3 (the TraditionalVariant) per docs/proposals/05-control-unit.md + doc 03 §6 on branch
feat/bb-phase-3b. Plan first — create an implementation plan artifact and wait for my approval before
writing code. This is a large phase; if you hit context-window limits, escalate and we will split into
sub-branches. Create daemon/src/core/variants/traditional.py implementing CoordinationVariant:
genesis (triage tier → AG expert generation via ONE LiteLLM call with model-pool diversity → objective entry
→ attach uploads), step (deterministic guards FIRST: accepted solution / max_rounds / budget / stall —
then the paper's CU as ONE bare LiteLLM selection call per round, doc 05 §1.1, with the deterministic
fallback table on garbled output), and finalize (Decider path; SolE majority-similarity vote as the
fallback, doc 05 §3). The CU/AG are control-plane LiteLLM calls, NEVER Hermes runs (doc 05 §7). Add role
personas + capability profiles in personas.py. Dispatch via the Phase-3a registry, load-balancing experts
one-per-host; pass the triage-selected model into the run request (doc 05 §8). Cost rails in the SAME PR:
budget ceiling, round/duration caps, concurrency cap, stall breaker, and daemon-side decline gating
(doc 05 §5). Per-turn (not per-task) timeouts for multi-round execution (doc 10 Q5). Tests for the guards,
the CU-output parser + fallback, and SolE scoring against an in-memory board (no LLM); run them.
VERIFY LIVE end-to-end: run a ci-verify-* task and paste evidence that one agent critiques ANOTHER's
finding unprompted and the loop terminates via the Decider or SolE (not a fixed pipeline). traditional only
behind the flag; legacy default. Open a PR with gh into feat/true-blackboard linking doc 05 + doc 03 §6,
and PRINT the PR number. Update docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 23 — 🆕 NEW AGENT — Independent review of Phase 3b
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 05 + doc 03 §6 and the
seams checklist. `gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite
yourself — confirm the output matches what the implementer claims. Blocking checklist: deterministic guards
run BEFORE the CU LLM call, CU/AG are bare LiteLLM calls (never Hermes runs), the task runner only calls the
CoordinationVariant interface (no hard-coded sequence/roles), cost rails + stall breaker + decline gating
present in this PR, SolE implemented per doc 05 §3, legacy stays default. Do NOT fix. If clean, say so
explicitly.
```

### Step 24 — 🆕 NEW AGENT — Independent live verification of Phase 3b
```
You are an INDEPENDENT verifier. Run a fresh ci-verify-* task with coordination.variant=traditional and
confirm objectively: (a) one agent reads + critiques ANOTHER agent's finding without the daemon dictating it,
(b) the loop terminates via the Decider's solution or SolE, not a fixed pipeline length, and (c) tasks.
terminated_by / answer_source are recorded. Post pass/fail on PR <PR#> with raw evidence (the trace/board
excerpt). Use only the ci-verify-* namespace. Do not edit code.
```

### Step 25 — 🧑 YOU
**If findings:** **♻️ RESUME Step 22** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 23–24 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 3c — (OPTIONAL) Coordinator narration lane

> Skip unless you want the CU's selection rationale visible in the showcase.

### Step 26 — 🆕 NEW AGENT (`/goal`) — Implement Phase 3c
```
Optional, per docs/proposals/05-control-unit.md §1.2, branch feat/bb-phase-3c. Plan first — create an
implementation plan artifact and wait for my approval before writing code. Surface the CU selection call's
{selected, rationale} as Coordinator-lane events in the UI (doc 13 §3), gated by
coordination.traditional.coordinator_narration (default false). HARD CONSTRAINTS: it is the SAME selection
call (no extra LLM spend), a malformed rationale never blocks the loop, and the lane hides entirely when
flagged off. Test the malformed-rationale path explicitly; run tests. Open a PR with gh into
feat/true-blackboard linking doc 05 §1.2 + doc 13, and PRINT the PR number. Update
docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 27 — 🆕 NEW AGENT — Independent review of Phase 3c
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 05 §1.2. `gh pr diff
<PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite yourself. Blocking checklist:
default false, no additional LLM call introduced, malformed rationale tested + non-blocking, lane fully
hidden when off. Do NOT fix. If clean, say so explicitly.
```

### Step 28 — 🧑 YOU
**If findings:** **♻️ RESUME Step 26** → `Address findings on PR <PR#>. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge `<PR#>`.

---

## Phase 4 — Blackboard visualization + trace inspector + variant plumbing

> Optional parallelism: Phase 4 touches `mission-control/` only, so Steps 29–32 may run concurrently with Phases 2–3 (different files). **However, Phase 4 should not be verified or merged until Phase 1 (traces) and Phase 2 (gateway) are merged** — the UI needs real trace data and board entries to test against. Stubbed/mock data is acceptable during coding; the VERIFY step must use a live task. If you'd rather stay strictly linear, just run it here. Use the Agent Manager to run parallel conversations side-by-side.

### Step 29 — 🆕 NEW AGENT (`/goal`) — Implement Phase 4
```
Implement Phase 4 per docs/proposals/08-ui-blackboard-visualization.md + 09-ui-agent-trace-inspector.md in
doc 13's showcase philosophy, branch feat/bb-phase-4. Plan first — create an implementation plan artifact
and wait for my approval before writing code. Build, in this order: (1) the variant plumbing — the
GET /capabilities endpoint + proxy, the composer variant dropdown (one enabled option in V1, disabled
options with tooltips), variant on /submit + the task chip, and the panel-registry skeleton in
lib/variants.ts with the traditional adapter (doc 08 §2.1) — prove it with a dummy adapter that mounts a
fake panel with ZERO edits outside variants.ts + the adapter file; (2) agent-role tokens + the deterministic
author-color fallback in design-tokens.ts/globals.css/DESIGN.md (doc 08 §8); (3) BlackboardGraph (React
Flow, extending DAGVisualizer patterns — entry nodes, refs edges, salience weighting, attachment/artifact
nodes, Cleaner removal animation), WorkerLane, ConsensusMeter, the blackboard tab Graph/Stream toggle;
(4) the trace timeline + Trace/Raw toggle + turn inspector slide-over with the "Resulted in" footer;
(5) the linear replay scrubber on the Phase-2 event log. Compose ONLY from existing ui/ primitives +
DESIGN.md tokens (zero hardcoded values). Use existing SSE plumbing with rAF batching for trace events.
VERIFY: build passes, and run against a live task so the graph animates as entries land — attach a
screenshot/recording to the PR. Open a PR with gh into feat/true-blackboard linking doc 08/09/13, and PRINT
the PR number. Update docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 30 — 🆕 NEW AGENT — Independent review of Phase 4
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 08/09/13 and DESIGN.md.
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and build Mission Control yourself.
Blocking checklist: composes only from ui/ primitives + tokens (NO hardcoded colors/spacing), the dummy-
adapter test passes (zero shell edits), the dropdown is fed by /capabilities (not hardcoded options), uses
existing SSE plumbing with batching, agent-identity colors + author fallback honored, build passes, animation
evidence attached. Do NOT fix. If clean, say so explicitly.
```

### Step 31 — 🆕 NEW AGENT — Independent verification of Phase 4
```
You are an INDEPENDENT verifier. Build Mission Control from PR <PR#> and run it against a live ci-verify-*
task; confirm the blackboard graph renders and animates as entries land, the trace timeline populates, the
variant dropdown renders from /capabilities, and an attached file + a produced artifact both appear. Post
pass/fail on PR <PR#> with a screenshot/recording. Do not edit code.
```

### Step 32 — 🧑 YOU
**If findings:** **♻️ RESUME Step 29** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 30–31 if needed. **If clean + CI green:** merge `<PR#>`.

---

## Phase 5 — Advanced features + cutover

### Step 33 — 🆕 NEW AGENT (`/goal`) — Implement Phase 5
```
Implement Phase 5 per docs/proposals/10-migration-and-rollout.md on branch feat/bb-phase-5. Plan first —
create an implementation plan artifact and wait for my approval before writing code. This is a large phase;
if you hit context-window limits, escalate and we will split into sub-branches. Then implement: native HITL
via run approvals (doc 12 §5.1), stateful turns via the Responses API (doc 12 §5.2), private sub-boards +
Conflict-Resolver (doc 05 §4), HITL directives/pause/steer (doc 05 §6), the budget gauge (doc 09 §5), and
the Mission cockpit (doc 13). Do the CUTOVER (flip coordination.variant default to traditional) ONLY after
doc 10 §6 + README §4 all pass AND the Phase-E benchmark shows traditional ≥ legacy on the eval set — run
that A/B and paste the numbers in the PR. Keep legacy_pipeline as a selectable fallback variant. Open a PR
with gh into feat/true-blackboard linking doc 10 §5–6 + doc 12 §5, and PRINT the PR number. Update
docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 34 — 🆕 NEW AGENT — Independent review of Phase 5
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 10 §5–6 + doc 12 §5.
`gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test suite yourself —
confirm the output matches what the implementer claims. Blocking checklist: legacy still selectable after
cutover, HITL approval + Responses API wired per doc 12, directives land as board entries, the A/B benchmark
numbers are pasted AND show traditional ≥ legacy, backward-compat contract updated. Do NOT fix. If clean,
say so explicitly.
```

### Step 35 — 🆕 NEW AGENT — Independent live verification of cutover
```
You are an INDEPENDENT verifier. Confirm objectively for PR <PR#>: run the Phase-E A/B (legacy vs
traditional) on the eval set and paste the metrics; exercise a run-approval HITL gate end-to-end on a node;
confirm legacy_pipeline still works when selected. Post pass/fail on PR <PR#> with raw numbers/output.
Do not edit code.
```

### Step 36 — 🧑 YOU
**If findings:** **♻️ RESUME Step 33** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. The verifier flagged: [paste findings]. Push fixes; no new PR.` Re-run 34–35 if needed. **If clean + CI green + benchmark gate met:** merge `<PR#>`, then merge `feat/true-blackboard` → `main`. **V1 is shipped.**

---

## Phase 6 — (FUTURE, SEPARATE) The variants

> Only after V1 is stable. The stigmergic variant is where the research novelty lives ([doc 15](15-novelty-and-research-directions.md)); PatchBoard is the reliability/token-economy regime ([doc 11](11-variant-patchboard.md)). They are independent of each other — run either or both, in any order.

### Step 37 — 🆕 NEW AGENT (`/goal`) — Implement the stigmergic variant
```
Implement the StigmergicVariant against the UNCHANGED engine per docs/proposals/16-variant-stigmergic.md on
branch feat/bb-variant-stigmergic. Plan first — create an implementation plan artifact and wait for my
approval before writing code. Then implement Stage A (daemon-simulated stigmergy, doc 16 §4): the pressure
field registered in the recompute_derived hook (doc 16 §3, weights/regions/decay per the config sketch),
roleless universal actors with jittered thresholds + region claims, exponential reinforcement decay,
stable-basin termination + the final synthesis activation, and the stigmergic UI adapter (pressure heatmap
overlay via the GraphOverlaySlot, pressure/decay strip, actor-claims lane — doc 16 §6) registered in
lib/variants.ts. Before any live run, execute the S1 simulation: replay completed traditional-task event
logs through the pressure computation (no LLM cost) and paste the pressure trajectories. It MUST run on the
same gateway/board/traces/UI shell with coordination.variant: stigmergic. If it does NOT, the seams
checklist (doc 03 §6) was violated earlier — find and fix the seam, do not special-case. Tests for pressure
terms, decay, and basin termination. Open a PR with gh linking doc 16 + doc 15, and PRINT the PR number.
Update docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 38 — 🆕 NEW AGENT — Independent review of the stigmergic variant
```
You are an INDEPENDENT reviewer; you did not write this. Review PR <PR#> against doc 16 + the seams
checklist (doc 03 §6). `gh pr diff <PR#>`, read ONLY diff + spec. Also check out the branch and run the test
suite yourself. Blocking checklist: NO changes to the gateway/board/traces/UI shell were needed (if they
were, a seam was violated — flag it), pressure lives in the recompute_derived hook, decay + basin
termination implemented + tested, the UI adapter registers via lib/variants.ts only, the S1 simulation
evidence is pasted, engine cost rails still bind. Do NOT fix. If clean, say so explicitly.
```

### Step 39 — 🆕 NEW AGENT — Run the novelty experiments
```
You are an INDEPENDENT experimenter. Using the Phase-E harness, run on the node cluster: (1) the
traditional-vs-stigmergic A/B on the eval set; (2) the kill-a-node resilience experiment (partition a node
mid-task; measure degradation for traditional vs stigmergic). Capture all metrics (accuracy/$/latency/
rounds + recovery behavior) and post a results summary on PR <PR#>. Use only the ci-verify-* namespace.
Do not edit code.
```

### Step 40 — 🆕 NEW AGENT (`/goal`) — (Optional) Implement the PatchBoard variant
```
Implement the PatchBoardVariant against the UNCHANGED engine per docs/proposals/11-variant-patchboard.md on
branch feat/bb-variant-patchboard. Plan first — create an implementation plan artifact and wait for my
approval before writing code. Gate first on P1 (doc 11 §8): prompt-test RFC 6902 emission against the live
Runs API and paste the measured rejection rates BEFORE building. Then implement: the Architect profile +
blueprint generation with deterministic blueprint validation (doc 11 §3), the patch kernel (parse →
authorize-by-write-contract → test preconditions → dry-run schema validation → atomic commit → state hash,
doc 11 §5), bounded views (doc 11 §4.2), the circuit policy (doc 11 §5.2), event-driven rule control
(doc 11 §6), and the patchboard UI adapter (state-tree view, blueprint inspector with worker spawn
animations, transaction log — doc 11 §7) in lib/variants.ts. Same seam rule: zero engine/shell edits or a
seam was violated. Heavy kernel unit tests (validation pipeline, transactional all-or-nothing, circuit
trips). Open a PR with gh linking doc 11, and PRINT the PR number. Update
docs/proposals/MIGRATION_STATUS.md. Do not review your own PR.
```

### Step 41 — 🧑 YOU
**If findings on any variant PR:** **♻️ RESUME the implementing step** → `Address findings on PR <PR#> and CI. The reviewer flagged: [paste findings]. Push fixes; no new PR.` **If clean + CI green:** merge. You now have the novel artifact + measured results for the showcase.

---

## Quick reference: the run map

| Phase | Implement (🆕) | Review (🆕) | Verify (🆕) | You (🧑) |
|:--|:--|:--|:--|:--|
| 0 Foundations | 1 | 2 | — | 3 |
| 1 Traces ⭐ | 4 | 5 | 6 | 7 |
| E Eval/A-B | 8 | 9 | — | 10 |
| 2 Gateway | 11 | 12 | 13 | 14 |
| 2F Files & artifacts | 15 | 16 | — | 17 |
| 3a Profiles | 18 | 19 | 20 | 21 |
| 3b TraditionalVariant | 22 | 23 | 24 | 25 |
| 3c Coordinator (opt) | 26 | 27 | — | 28 |
| 4 Visualization | 29 | 30 | 31 | 32 |
| 5 Cutover | 33 | 34 | 35 | 36 |
| 6 Variants (future) | 37 / 40 | 38 | 39 | 41 |

Every numbered cell is its **own new agent conversation**, except the 🧑 column (you) and the resume-to-fix follow-ups noted in each "YOU" step. Use the Agent Manager to monitor conversations in parallel when safe.

---

## Appendix A — Why the actor/critic split

The implement step and the review/verify steps are deliberately **different agent conversations** because a single agent that both writes and judges its own work drifts toward approving it (self-review bias), and that error compounds across phases. Independent critics — a fresh Reviewer conversation (sees only spec + diff), an independent Verifier (objective output from a node), CI, and Bugbot — decide "done" with evidence, not the author's confidence. You are the cheap final gate: because the critics did the real work, your merge click is low-effort and high-trust. Resuming the implementer conversation to *fix* its own PR is fine; letting it *approve* its own PR is not.

## Appendix B — When you must step in (escalation)

Agents are told to STOP and ask you when: they'd deviate from the spec or a seam, the spec is ambiguous/contradictory, a change would be destructive on a node or touch secrets, or finishing a phase would require starting another. Everything else runs unattended. You also raise budget/safety caps if a phase requests it.
