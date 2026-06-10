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
