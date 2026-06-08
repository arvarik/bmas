[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Gap Analysis](01-gap-analysis.md) | [➡️ Next: Target Architecture](03-target-architecture.md)

# 02 — Peer Review of the External Model's Suggestions

> [!ABSTRACT] How to read this
> The external review was strong and largely correct in *direction*. But several recommendations are either contrary to the bMAS paper, mismatched to the Hermes runtime as actually deployed, or premature for a homelab cost profile. This document grades each suggestion **Adopt / Adapt / Defer / Reject**, explains why, and adds the recommendations the external model missed.

---

## 1. Scorecard

| # | Suggestion | Verdict | One-line rationale |
|:--|:--|:--|:--|
| 1 | Shift push → pure pull (delete central dispatch) | **Adapt** | Right instinct, wrong absolute. The paper keeps a Control Unit. Go *hybrid*. |
| 2 | Eliminate single-task bottleneck; per-key optimistic locks | **Adopt** | Core to blackboard parallelism. |
| 3 | Formalize Control Unit vs Knowledge Sources | **Adopt** | This *is* the central fix. |
| 4 | PatchBoard — JSON-Patch + deterministic kernel + schema | **Adopt (flagship)** | Single highest-leverage reliability + observability win. |
| 5 | Pheromone decay / Stigmergic Blackboard Protocol | **Adapt / Defer** | Adopt a simple salience+recency score now; full pheromone decay later. |
| 6 | Hierarchical sub-blackboards | **Adopt (scoped)** | Maps cleanly to the paper's private spaces; medium priority. |
| 7 | Hermes Profiles for role isolation | **Adapt** | Verify against runtime; useful but not on the critical path. |
| 8 | Shared procedural memory via `external_dirs` | **Defer** | High value, unverified mechanism, not needed for the inversion. |
| 9 | Indexed memory auto-routing | **Adapt** | Reframe as "pass the board index, not the board." Good token hygiene. |
| 10 | Native context compression | **Defer** | Runtime-dependent; our Cleaner role covers the near-term need. |

---

## 2. Detailed assessment

### 2.1 Push → Pull (Suggestion 1) — **Adapt, don't take literally**

The external model says: *"the central orchestrator does not assign tasks… agents should independently subscribe… and 'volunteer' to pull the task."*

**Where it's right:** Today's daemon is a puppeteer (see [Gap G1/G2](01-gap-analysis.md)). Agents must become participants that read the board and choose to contribute. The data-flow inversion is exactly correct.

**Where it overshoots:** The bMAS paper (Han & Zhang, §3.2) is explicit that a **Control Unit** exists and "dynamically schedules which agents should act next." Pure, uncontrolled stigmergy with no central referee is a *different* architecture (closer to ant-colony optimization) and brings two homelab-hostile properties:

- **Cost runaway.** Three agents free-spinning on cloud LLMs with no governor is how you wake up to a large Gemini bill. The Control Unit is also the **cost governor**.
- **Unobservable termination.** Without a Decider, "when is this done?" has no answer, and the UI has nothing to show as a finish line.

There is also a hard runtime constraint the external model didn't account for: Hermes is invoked **per-task** (`hermes -z` / a Runs API call). There is no persistent agent process sitting idle, subscribed to Redis, waiting to volunteer. True pull would require running long-lived Hermes daemons with crons polling Redis — a large operational change for unclear benefit.

**Our position:** Implement a **referee-driven hybrid** (full spec in [05](05-control-unit.md)). The Control Unit posts the problem and a participation invitation to the board; it then *opportunistically dispatches* the agents whose role is relevant to the board's current state (rather than a fixed sequence). Agents read the live board on each turn and may decline to contribute ("no new information"). This delivers blackboard semantics — data-driven scheduling, reaction to peers, dynamic round count — while keeping a cost governor and a definable terminal state. Pure pull (Hermes crons watching Redis) is documented as an **optional future variant**, not the baseline.

### 2.2 Per-key optimistic locking (Suggestion 2) — **Adopt**

Correct and necessary. The current lock is task-scoped (`orchestrator:{task_id}`), not truly global across all tasks; the real flaw is that within a task the daemon is the single meaningful writer and the standard flow is sequential ([Gap G4](01-gap-analysis.md)). Replace that per-task, daemon-owned mutation model with **per-entry optimistic concurrency**: each board entry carries a `version`/`rev`; writes use Redis `WATCH`/`MULTI` (or a Lua CAS) and retry on conflict. This lets multiple agents commit to *different* entries concurrently and serializes only true contention on the *same* entry. Detailed in [04 §5](04-blackboard-protocol.md#5-optimistic-concurrency).

### 2.3 Formalize Control Unit vs Knowledge Sources (Suggestion 3) — **Adopt**

This is the heart of the fix and aligns with both the paper and the existing [roadmap](../roadmap/control-unit.md). We promote the orchestrator's hidden assumptions into explicit, named components: a **Control Unit** (scheduler + Decider) and a set of **Knowledge Sources** (Planner, Critic, Conflict-Resolver, Cleaner, and dynamic Experts). See [05](05-control-unit.md).

### 2.4 PatchBoard / JSON-Patch + deterministic kernel (Suggestion 4) — **Adopt as the flagship**

This is the strongest idea in the external review and we elevate it to the centerpiece. Agents do not write free-form text into shared state; they **propose [RFC 6902](https://datatracker.ietf.org/doc/html/rfc6902) JSON Patches** against typed board entries. The daemon is a **deterministic kernel**: it validates each proposed patch against a JSON Schema, rejects malformed/hallucinated mutations, and only then commits — emitting the applied patch as an event.

Why this is worth the effort, beyond hallucination safety:

- **Observability for free.** Every committed patch is an event. The live blackboard graph ([08](08-ui-blackboard-visualization.md)) and replay are literally a fold over the patch log. The UI animation frames *are* the patches.
- **Auditability.** "Who changed what, when, and was it accepted?" becomes a first-class query, not forensic log-grepping.
- **Concurrency.** Patches are small and addressed to a path, making optimistic CAS natural.

Full schema, kernel design, and rejection semantics in [04](04-blackboard-protocol.md).

### 2.5 Pheromone decay / SBP (Suggestion 5) — **Adapt now, defer the full version**

Elegant but easy to over-engineer. Exponential pheromone decay with per-agent "olfactory thresholds" is genuinely hard to *tune* and *debug*, and tuning it on paid models is expensive. 

**Now:** give each board entry a `salience` score derived from `confidence × recency × references` (cheap, deterministic, explainable). The Control Unit and Cleaner use it for prioritization and pruning. **Later:** add true time-based decay and threshold-gated activation as an opt-in module once the system is stable and observable. Specified in [04 §6](04-blackboard-protocol.md#6-salience-the-pragmatic-pheromone).

### 2.6 Hierarchical sub-blackboards (Suggestion 6) — **Adopt, scoped**

Maps directly onto the paper's public/private split and the existing `bmas:private:{session}:debate` namespace. We formalize **private sub-boards** for Conflict-Resolver-mediated debates: conflicting agents argue in a transient private board; only the resolved entry is promoted to public. Medium priority — lands after the core loop and PatchBoard. See [05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution).

### 2.7–2.10 Hermes-native features — **Verify first**

The external model asserts several Hermes capabilities (`hermes -p planner` profiles, `external_dirs` shared skills, `skill_manage`, native `ContextCompressor`, crons watching Redis, Indexed Memory). Our own [HERMES_API.md](../HERMES_API.md) confirms *some* of these (profiles, skills, crons, the Runs API) but not all in the exact form described.

- **Profiles (7):** Confirmed via `/api/profiles`. Useful for per-role model/skill isolation, but **not on the critical path** — persona injection via `AGENTS.md` already works. Adopt opportunistically.
- **Shared skills via `external_dirs` (8):** Mechanism unverified in our docs. High potential value, but it is an *enhancement to agent quality*, not a requirement for the blackboard inversion. **Defer** and track in the [agent-integration roadmap](../roadmap/agent-integration.md).
- **Indexed memory (9):** Reframe as a concrete, runtime-agnostic practice we control: **pass the board *index* (a table of contents of entry IDs + titles + salience), not the full board.** Agents request specific entries by ID via a `read_entry` affordance. This is just good context hygiene and we own it entirely. **Adapt.**
- **Context compression (10):** Runtime-dependent. Our **Cleaner** role (prunes low-salience entries between rounds) addresses the near-term token-bloat problem deterministically. **Defer** native compression.

> [!WARNING]
> Do not build on an asserted Hermes feature without confirming it on a live node. The consolidated verification checklist lives in [10 — Open Questions](10-migration-and-rollout.md#open-questions-verify-before-building).

---

## 3. What the external review missed

These are first-class concerns the external model did not raise, and several are *prerequisites* for its own suggestions to be observable.

1. **Traces are broken at the source.** The most consequential gap is invisible from the README: `hermes -z` discards all intermediate agent output, and the cost path is dead because the agent response has no `usage` field ([Gap G5](01-gap-analysis.md#7-the-silent-observability-failure-root-cause-for-the-ui-work)). None of the external suggestions can be *seen* until this is fixed. This is sequenced first ([06](06-agent-traces.md)).
2. **Event-sourcing the board.** The external model treats the board as a mutable store. Treating it as an **append-only event log with a derived snapshot** is what makes replay, the live graph, and crash recovery tractable. This is a structural decision, not an add-on ([04 §2](04-blackboard-protocol.md#2-the-board-as-an-event-log)).
3. **Termination & convergence are a product surface, not just an algorithm.** The Decider's consensus score must be *streamed to the UI* so the operator can watch convergence happen and intervene (HITL). ([05 §3](05-control-unit.md#3-consensus--termination), [08](08-ui-blackboard-visualization.md)).
4. **Cost governance is part of the Control Unit.** Round caps, duration caps, and a per-task budget ceiling must be enforced by the referee, or stigmergy becomes a billing incident ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).
5. **Backward compatibility & migration.** The dashboard, SQLite schema, and SSE event names are a live contract. The inversion must ship behind a feature flag with additive schema changes ([10](10-migration-and-rollout.md)).
6. **Design-system fidelity for new UI.** Any new visualization must compose from existing primitives/tokens. The external model proposed UI in spirit; we hold it to the [DESIGN.md](../design/DESIGN.md) contract ([08](08-ui-blackboard-visualization.md), [09](09-ui-agent-trace-inspector.md)).

➡️ Continue to [03 — Target Architecture](03-target-architecture.md).
