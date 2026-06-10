[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Gap Analysis](01-gap-analysis.md) | [➡️ Next: Target Architecture](03-target-architecture.md)

# 02 — Peer Review of the External Model's Suggestions

> [!ABSTRACT] How to read this
> The external review was strong and largely correct in *direction*. But several recommendations are either contrary to the bMAS paper, mismatched to the Hermes runtime as actually deployed, or premature for a homelab cost profile. This document grades each suggestion **Adopt / Adapt / Defer / Reject**, explains why, and adds the recommendations the external model missed.
>
> **Re-graded after the architecture decision** ([doc 03](03-target-architecture.md)): the V1 core is the **paper-faithful LbMAS** — natural-language entries, an LLM Control Unit, typed envelopes behind a deterministic gateway. PatchBoard-style schema-grounded mutation is now an **optional variant** ([doc 11](11-variant-patchboard.md)), not the flagship. Several verdicts below changed as a result; the original verdicts are preserved struck-through where they moved.

---

## 1. Scorecard

| # | Suggestion | Verdict | One-line rationale |
|:--|:--|:--|:--|
| 1 | Shift push → pure pull (delete central dispatch) | **Adapt** | Right instinct, wrong absolute. The paper keeps a Control Unit. Go *hybrid*. |
| 2 | Eliminate single-task bottleneck; per-key optimistic locks | **Adapt** | The parallelism goal is core; the mechanism changed — append-only entries behind a single-writer gateway need no CAS ([04 §6](04-blackboard-protocol.md#6-concurrency-append-only-makes-it-easy)). |
| 3 | Formalize Control Unit vs Knowledge Sources | **Adopt** | This *is* the central fix. |
| 4 | PatchBoard — JSON-Patch + deterministic kernel + schema | ~~Adopt (flagship)~~ → **Defer to variant** | Excellent published design — but it replaces the paper's natural-language medium. Lives in [doc 11](11-variant-patchboard.md); its *determinism lessons* are absorbed into the core gateway. |
| 5 | Pheromone decay / Stigmergic Blackboard Protocol | **Adapt / Defer** | Adopt a simple salience score now ([04 §7](04-blackboard-protocol.md#7-salience-a-cheap-explainable-importance-signal)); true pheromone fields are the stigmergic variant ([doc 16](16-variant-stigmergic.md)). |
| 6 | Hierarchical sub-blackboards | **Adopt (scoped)** | Maps cleanly to the paper's private spaces; medium priority. |
| 7 | Hermes Profiles for role isolation | **Adapt** | Verified against the runtime ([doc 12](12-hermes-and-node-topology.md)); useful but not on the critical path. |
| 8 | Shared procedural memory via `external_dirs` | **Defer** | High value, unverified mechanism, not needed for the inversion. |
| 9 | Indexed memory auto-routing | **Adapt** | Reframe as token budgeting: full board by default, index+`read_entry` fallback at scale ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)). |
| 10 | Native context compression | **Defer** | Runtime-dependent; the paper's Cleaner role covers the near-term need. |

---

## 2. Detailed assessment

### 2.1 Push → Pull (Suggestion 1) — **Adapt, don't take literally**

The external model says: *"the central orchestrator does not assign tasks… agents should independently subscribe… and 'volunteer' to pull the task."*

**Where it's right:** Today's daemon is a puppeteer (see [Gap G1/G2](01-gap-analysis.md)). Agents must become participants that read the board and choose to contribute. The data-flow inversion is exactly correct.

**Where it overshoots:** The bMAS paper (Han & Zhang, §3.2) is explicit that a **Control Unit** exists and "dynamically schedules which agents should act next." Pure, uncontrolled stigmergy with no central referee is a *different* architecture (closer to ant-colony optimization) and brings two homelab-hostile properties:

- **Cost runaway.** Three agents free-spinning on cloud LLMs with no governor is how you wake up to a large Gemini bill. The Control Unit is also the **cost governor**.
- **Unobservable termination.** Without a Decider, "when is this done?" has no answer, and the UI has nothing to show as a finish line.

There is also a hard runtime constraint the external model didn't account for: Hermes is invoked **per-turn** (`hermes -z` / a Runs API call). There is no persistent agent process sitting idle, subscribed to Redis, waiting to volunteer. True pull would require running long-lived Hermes daemons with crons polling Redis — a large operational change for unclear benefit.

**Our position:** Implement the **paper's referee-driven loop** (full spec in [05](05-control-unit.md)). The Control Unit posts the objective to the board; each round it selects, via its own LLM judgment plus deterministic guards, the agents whose role is relevant to the board's current state (rather than a fixed sequence). Agents read the live board on each turn and may decline to contribute ("no new information"). This delivers blackboard semantics — data-driven scheduling, reaction to peers, dynamic round count — while keeping a cost governor and a definable terminal state. Pure pull (Hermes crons watching Redis) is exactly the **stigmergic variant** ([doc 16](16-variant-stigmergic.md)), selectable later from the same UI dropdown — not the baseline.

> [!NOTE] Published support for the pull instinct
> [Salemi et al. (2025), arXiv:2510.01285](https://arxiv.org/abs/2510.01285) demonstrate a blackboard LLM-MAS where subordinate agents **volunteer** to answer requests posted on a shared blackboard, beating master-slave orchestration by 13–57% on data-discovery benchmarks — evidence that pull-flavored participation works *and* prior art to cite ([doc 15 §1](15-novelty-and-research-directions.md#1-honest-framing-prior-art-vs-contribution)). Note, however, that their design still has a central agent posting requests; it supports the hybrid position here, not the pure-swarm extreme.

### 2.2 Per-key optimistic locking (Suggestion 2) — **Adapt: the goal, not the mechanism**

The *goal* — kill the single-writer-per-task bottleneck so multiple agents contribute concurrently — is core to the whole proposal and fully adopted. The *mechanism* the external model proposed (per-entry `version`/CAS with `WATCH`/`MULTI` retries) solved a problem the V1 board no longer has: in the paper-faithful protocol, agents **append new entries**; they never edit each other's entries in place. Append-only writes funneled through a single-writer **Board Gateway** per task are conflict-free by construction — concurrent turns interleave at entry granularity with no retries and no version fields ([04 §6](04-blackboard-protocol.md#6-concurrency-append-only-makes-it-easy)).

Per-path optimistic concurrency *is* the right tool when agents mutate a shared structured state tree — which is precisely the PatchBoard variant's regime; that machinery lives there ([doc 11 §5](11-variant-patchboard.md#5-the-deterministic-kernel)).

### 2.3 Formalize Control Unit vs Knowledge Sources (Suggestion 3) — **Adopt**

This is the heart of the fix and aligns with both the paper and the existing [roadmap](../roadmap/control-unit.md). We promote the orchestrator's hidden assumptions into explicit, named components: a **Control Unit** (LLM selector + deterministic guards + Decider role) and a set of **Knowledge Sources** (Planner, Critic, Conflict-Resolver, Cleaner, Decider, and dynamic Experts). See [05](05-control-unit.md).

### 2.4 PatchBoard / JSON-Patch + deterministic kernel (Suggestion 4) — **Defer to variant; absorb its determinism lessons**

This was originally graded *Adopt (flagship)*. It is demoted — deliberately, and not because the idea got worse.

> [!IMPORTANT] The design is published prior art — and a different paradigm
> [Zhang, Shi & Wang (2026), arXiv:2605.29313](https://arxiv.org/abs/2605.29313) published *"PatchBoard: Schema-Grounded State Mutation for Reliable and Auditable LLM Multi-Agent Collaboration"*: validated [RFC 6902](https://datatracker.ietf.org/doc/html/rfc6902) JSON-Patch mutations over shared structured state, a deterministic kernel as the only writer, role-scoped write contracts, transactional commits, logged rejections, replayable transaction logs — with strong results (84.6% vs 30.8% LangGraph on ALFWorld; zero committed-state contamination under fault injection).

**Why it is not the V1 core.** PatchBoard replaces the blackboard's *medium*: agents emit patch operations against a schema'd state tree instead of natural-language contributions. That is a different communication paradigm from [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701), whose entire mechanism — experts critiquing, rebutting, and building on each other's *prose* — is what we are reproducing, and which chat-style and coding-harness tasks prefer ([doc 03](03-target-architecture.md)). Building PatchBoard first would have shipped the 2026 paper while claiming to implement the 2025 one — the exact conflation this revision removes.

**What the core keeps from it** (the lessons survive the demotion):
- **A deterministic single writer.** The Board Gateway validates every proposed entry's *envelope* and is the only component that commits ([04 §4](04-blackboard-protocol.md#4-the-board-gateway)) — kernel discipline applied to envelopes instead of patches. Malformed and unauthorized writes are rejected *and logged as events*, exactly PatchBoard's auditability property.
- **Event-sourced, replayable state.** The append-only `board_events` log ([04 §5](04-blackboard-protocol.md#5-the-board-as-an-event-log)) gives the same replay/audit wins without constraining what agents may say.
- **Bounded context discipline.** Their ablation showed small context views winning; our token-budgeted payload tiers apply the same finding ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)).
- **Deterministic livelock circuit-breakers.** Their circuit policy halted 96% of injected no-op/oscillation loops; our stall-breaker guard is the envelope-world equivalent ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).

**Where the full design lives:** [doc 11 — Variant: PatchBoard](11-variant-patchboard.md) — schema generation, the patch kernel, dynamic worker generation, transaction-log UI, and how it plugs into the `CoordinationVariant` seam ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)) behind the same UI dropdown.

### 2.5 Pheromone decay / SBP (Suggestion 5) — **Adapt now, defer the full version**

Elegant but easy to over-engineer. Exponential pheromone decay with per-agent "olfactory thresholds" is genuinely hard to *tune* and *debug*, and tuning it on paid models is expensive.

**Now:** give each board entry a `salience` score derived from `confidence × recency × references` (cheap, deterministic, explainable). The Control Unit's stall detection, the Cleaner's pruning hints, and the UI's visual weighting use it. Specified in [04 §7](04-blackboard-protocol.md#7-salience-a-cheap-explainable-importance-signal). **Later:** true time-based decay, pressure fields, and threshold-gated self-activation are the **stigmergic variant** — fully specified with its extensibility story in [doc 16](16-variant-stigmergic.md).

### 2.6 Hierarchical sub-blackboards (Suggestion 6) — **Adopt, scoped**

Maps directly onto the paper's public/private split and the existing `bmas:private:{session}:debate` namespace. We formalize **private sub-boards** for Conflict-Resolver-mediated debates: conflicting agents argue in a transient private space; only the reconciled entry is promoted to public ([04 §2](04-blackboard-protocol.md#2-public-and-private-spaces)). Medium priority — lands after the core loop. See [05 §4](05-control-unit.md#4-private-sub-boards-conflict-resolution).

### 2.7–2.10 Hermes-native features — **Verify first**

The external model asserts several Hermes capabilities (`hermes -p planner` profiles, `external_dirs` shared skills, `skill_manage`, native `ContextCompressor`, crons watching Redis, Indexed Memory). Live verification ([doc 12 §1, §4](12-hermes-and-node-topology.md)) confirms *some* of these (profiles, skills, crons, the Runs API) but not all in the exact form described.

- **Profiles (7):** Confirmed via `/api/profiles` — but per-request profile selection over the Runs API is **not** available ([12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)), so the dispatch mechanism must be chosen and verified before profile-dependent work. Useful, not on the critical path — persona injection via `AGENTS.md` already works. Adopt opportunistically.
- **Shared skills via `external_dirs` (8):** Mechanism unverified. High potential value, but it is an *enhancement to agent quality*, not a requirement for the blackboard inversion. **Defer** and track in the [agent-integration roadmap](../roadmap/agent-integration.md).
- **Indexed memory (9):** Reframe as a concrete, runtime-agnostic practice we control. The paper sends the **full board** to dispatched agents, and V1 defaults to that for fidelity; the **index + `read_entry`** tier engages on token pressure ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)), and the Responses API gives agents cross-round working memory without re-stuffing the board ([12 §5.2](12-hermes-and-node-topology.md#52-stateful-turns-via-the-responses-api)). **Adapt.**
- **Context compression (10):** Runtime-dependent. The paper's **Cleaner** role (removes redundant/erroneous entries between rounds) addresses the near-term token-bloat problem with an auditable mechanism (every removal is an event). **Defer** native compression.

> [!WARNING]
> Do not build on an asserted Hermes feature without confirming it on a live node. The consolidated verification checklist lives in [10 — Open Questions](10-migration-and-rollout.md#3-open-questions-verify-before-building).

---

## 3. What the external review missed

These are first-class concerns the external model did not raise, and several are *prerequisites* for its own suggestions to be observable.

1. **Traces are broken at the source.** The most consequential gap is invisible from the README: `hermes -z` discards all intermediate agent output, and the cost path is dead because the agent response has no `usage` field ([Gap G5](01-gap-analysis.md#7-the-silent-observability-failure-root-cause-for-the-ui-work)). None of the external suggestions can be *seen* until this is fixed. This is sequenced first ([06](06-agent-traces.md)).
2. **Event-sourcing the board.** The external model treats the board as a mutable store. Treating it as an **append-only event log with a derived snapshot** is what makes replay, the live graph, and crash recovery tractable — for every variant, not just the core. This is a structural decision, not an add-on ([04 §5](04-blackboard-protocol.md#5-the-board-as-an-event-log)).
3. **Termination & convergence are a product surface, not just an algorithm.** The Decider's judgment and the convergence signal must be *streamed to the UI* so the operator can watch progress and intervene (HITL). ([05 §3](05-control-unit.md#3-consensus--termination), [08](08-ui-blackboard-visualization.md)).
4. **Cost governance is part of the Control Unit.** Round caps, duration caps, a per-task budget ceiling, and decline-aware gating must be enforced by the referee, or multi-agent debate becomes a billing incident ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).
5. **Files in, files out.** The external review never addresses how a user hands the system a PDF or how an agent's created codebase reaches the operator — yet that is table stakes for the "chat system + coding harness" goal. The full pipeline (storage config, attachment entries, node staging, artifact sync) is [doc 17](17-files-and-artifacts.md), recorded as [Gap G6](01-gap-analysis.md#75-the-missing-file-pipeline-input-and-output).
6. **Backward compatibility & migration.** The dashboard, SQLite schema, and SSE event names are a live contract. The inversion must ship behind a feature flag with additive schema changes ([10](10-migration-and-rollout.md)).
7. **Variant extensibility as a seam, not a fork.** Three coordination paradigms (traditional / PatchBoard / stigmergic) must share one engine, one event log, one trace pipeline, and one UI shell, differing only behind the `CoordinationVariant` interface ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)) and the UI panel registry ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)). Without this, each future paradigm becomes a rewrite.
8. **Design-system fidelity for new UI.** Any new visualization must compose from existing primitives/tokens. The external model proposed UI in spirit; we hold it to the [DESIGN.md](../design/DESIGN.md) contract ([08](08-ui-blackboard-visualization.md), [09](09-ui-agent-trace-inspector.md)).

➡️ Continue to [03 — Target Architecture](03-target-architecture.md).
