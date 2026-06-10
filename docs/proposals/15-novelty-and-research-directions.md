[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Implementing with Antigravity Agents](14-implementing-with-antigravity-agents.md) | [➡️ Next: Variant — Stigmergic](16-variant-stigmergic.md)

# 15 — Novelty, Research Directions & the Distributed Angle

> [!ABSTRACT]
> This document answers three questions for the showcase: **what is genuinely novel here** (stated defensibly, separating prior art from contribution), **what academic deep dives** the system enables, and **what falls out specifically from deploying a blackboard MAS as a real distributed system** rather than a single-process simulation. It is the "why this matters" companion to the engineering docs.

---

## 1. Honest framing: prior art vs. contribution

Be precise about this when you present — credibility comes from *not* overclaiming.

| Layer | Status | Who |
|:--|:--|:--|
| Blackboard architecture | Prior art (1985) | Hayes-Roth; Nii |
| Blackboard for **LLM** MAS (LbMAS): control unit + roles + consensus | Prior art (2025) | [Han & Zhang](https://arxiv.org/abs/2507.01701) — **the core we implement** ([03](03-target-architecture.md)–[05](05-control-unit.md)) |
| Blackboard LLM-MAS where agents **volunteer** based on board content (pull-flavored) | Prior art (2025) | [Salemi et al.](https://arxiv.org/abs/2510.01285) |
| Schema-grounded JSON-Patch mutation through a deterministic kernel (**PatchBoard**) | Prior art (2026) | [Zhang, Shi & Wang, arXiv:2605.29313](https://arxiv.org/abs/2605.29313) — implemented here as an **attributed variant** ([doc 11](11-variant-patchboard.md)), never claimed |
| **A *physically distributed* blackboard LLM-MAS on heterogeneous edge hardware** | **Contribution** | this project |
| **One engine that runs control-unit, schema-mutation, and roleless-stigmergic coordination unchanged** (the `CoordinationVariant` seam) | **Contribution** | this project ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms), [11](11-variant-patchboard.md), [16](16-variant-stigmergic.md)) |
| **The true-stigmergic regime itself** (pressure field + decay + roleless self-activation on a NL blackboard) | **Contribution (designed; hypothesis until measured)** | this project ([doc 16](16-variant-stigmergic.md)) |
| **Cost/locality-aware coordination** ($0 local edge vs. cloud, routed by triage) on a blackboard | **Contribution** | this project |
| **Real-time legibility of multi-agent coordination** (live board graph, traces, convergence, pressure overlay) | **Contribution (instrument)** | this project ([doc 13](13-ui-showcase-density.md)) |

> [!IMPORTANT] The one-sentence pitch
> *"The paper's LbMAS is a logical, single-process simulation where every agent is an LLM call; we built the first **physically distributed** blackboard multi-agent system on real heterogeneous edge hardware, with one engine that can run the paper's control-unit regime, a schema-grounded PatchBoard regime, **or** a fully decentralized stigmergic regime on the same machinery — and we made the coordination visible."*

## 2. What's novel enough to showcase

### 2.1 Distribution is the headline
The paper has **no notion of hosts, network, locality, or failure** — all "agents" are LLM calls inside one program. The moment you make the blackboard a *real* shared substrate (Redis + SQLite behind the [Board Gateway](04-blackboard-protocol.md#4-the-board-gateway)) across *real* nodes, a whole class of systems questions appears that the paper could not ask:
- agents coordinating through shared state over a network, with real concurrency and a real serialization point (the gateway);
- **locality**: each node has a local $0 inference engine *and* cloud access — placement matters;
- **failure**: nodes can partition; the simulation can't.

This reframes LbMAS from an *algorithm* into a *distributed system*. That reframing is the contribution.

### 2.2 One engine, three coordination paradigms
The `CoordinationVariant` seam ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)) means the *same* board store, event log, dispatch, traces, cost rails, and UI shell run:
- **Traditional (the paper):** an LLM control unit selects agents; natural-language debate; Decider/SolE termination.
- **PatchBoard (2026 paper, attributed):** an Architect generates schema + workers; validated JSON-Patch mutation; deterministic rules ([doc 11](11-variant-patchboard.md)).
- **Stigmergic (ours):** no scheduler, no roles — actors self-activate on a decaying pressure field ([doc 16](16-variant-stigmergic.md)).

Selecting between them is a per-task dropdown ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)). Showing the **A/B/C of directed vs. contract-driven vs. emergent coordination on identical hardware and queries** is a genuinely novel demo — the original paper only ablated its control unit crudely (Table 5); this runs *qualitatively different regimes* on one engine and one event-log format.

### 2.3 Cost/locality-aware coordination
The paper randomly assigns LLMs to agents *for diversity* and measures tokens. You route by **triage complexity** to a **cost/latency/locality frontier** ($0 local 4B ↔ cloud Pro) on real hardware — triage tiers feed the AG's model pools ([05 §2.1](05-control-unit.md#21-expert-generation-ag-and-model-pool-diversity)). So you can report **dollars, latency, and energy per solved task**, not just tokens — and study *when a $0 local model suffices vs. when cloud is worth it inside a blackboard loop.*

### 2.4 Legible coordination (interpretability instrument)
Most MAS work reports final accuracy. The live board graph, parallel agent-trace lanes, the convergence strip — and, under the stigmergic variant, the pressure heatmap — make the **process of multi-agent coordination** observable and replayable. That's both a showcase wow-factor and a real **interpretability instrument for multi-agent dynamics** — rare in the literature.

## 3. Academic deep dives (grouped by field)

### 3.1 Self-organization & swarm intelligence
- **Does emergent (stigmergic) consensus match directed (control-unit) consensus?** Extend the paper's ablation into a *true* roleless regime with decay ([16 §8](16-variant-stigmergic.md#8-why-the-traditional-core-ships-first)). Measure the accuracy/cost gap.
- **Self-organized criticality:** model the board's pressure field as a dynamical system. Are there attractors (consensus = basin), damping (decay), oscillation/deadlock regimes? Does the system sit near a critical point between frozen and chaotic? (Connects to Bak's SOC, ant-colony optimization.)
- **Decay-rate phase transitions:** sweep `decay_half_life_s`. Predict a phase transition: too-fast decay → no memory → no convergence; too-slow → stale dominance → premature lock-in. Locating that transition is a clean, publishable result.

### 3.2 Distributed systems
- **What consistency model does a blackboard MAS actually need?** The traditional core gets conflict-freedom from append-only entries behind a single-writer gateway ([04 §6](04-blackboard-protocol.md#6-concurrency-append-only-makes-it-easy)); PatchBoard reintroduces precondition-checked in-place mutation ([11 §5](11-variant-patchboard.md#5-the-deterministic-kernel)). Explore the spectrum further: single-writer gateway (now) → **CRDT-replicated board per node** for partition tolerance → eventual consistency. Where does coordination *quality* break?
- **CAP for cognition:** the blackboard is shared state; the CAP trade-offs are real here. Characterize behavior under partition for each regime (see §4).
- **The gateway as a contention bottleneck / SPOF:** measure throughput limits; propose sharding the board by region.

### 3.3 Information theory & token economy
- **Information gain per entry:** treat the board as a channel. Measure bits-per-entry, redundancy (what the [Cleaner](05-control-unit.md#2-the-agent-group) removes — and note the paper's finding that *removal* beats *marking*), and **board entropy over time** (high early → low at consensus). Compare against PatchBoard's token-frugal bounded views ([11 §4.2](11-variant-patchboard.md#42-turn-input-bounded-views)) for an NL-vs-structured token-economy result.
- **Optimal stopping:** the Decider is an optimal-stopping problem (stop when expected marginal information < cost). Formalize it; SolE ([05 §3](05-control-unit.md#3-consensus--termination)) is the forced-stop estimator.

### 3.4 LLM-MAS scaling & diversity
- **Agent scaling curves:** performance vs. `n` experts and vs. `max_rounds` (the paper fixed rounds=4; map the diminishing-returns curve on real hardware via the [Phase E harness](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation)).
- **Heterogeneity frontier:** empirically chart accuracy vs. the local/cloud model mix — when does diversity (the paper's claim) actually pay, and at what cost?

### 3.5 Causality & replay (enabled by event-sourcing)
Because every board mutation — in every variant — is an **event** ([04 §5](04-blackboard-protocol.md#5-the-board-as-an-event-log)), you can **replay and fork** any task ([04 §5.2](04-blackboard-protocol.md#52-fork-from-event-counterfactual-replay)). That makes **counterfactual analysis** possible: *"replay this task but suppress agent X's critique — does the outcome change?"* A causal-inference instrument for multi-agent reasoning that a stateful, non-event-sourced system cannot offer.

## 4. The distributed angle: what *only* physical distribution gives you

> [!TIP] The killer experiment
> **Stigmergy predicts robustness to agent loss** (ant colonies don't collapse when ants die). **Hypothesis: the roleless stigmergic regime is more partition-/failure-tolerant than the control-unit regime.** Kill a node mid-task and measure degradation in each regime. If the stigmergic variant heals (decay + self-activation route around the loss) while the traditional one stalls (the CU waits on a dispatched turn that will never return), that is a **striking, novel, biologically-motivated result** — and a jaw-dropping live demo ([16 §8](16-variant-stigmergic.md#8-why-the-traditional-core-ships-first)).

Other distribution-only contributions:
- **Locality-aware placement:** an optimization problem absent from any single-process MAS — which persona runs on which node given (a) network latency to the shared board and (b) the $0 local inference endpoint. ([doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles) "home node" idea is the seed.)
- **Partition healing via decay:** temporal decay isn't just for stigmergy — under a network partition, stale entries naturally lose pull, so the board *self-heals* toward the still-connected agents. Test whether decay alone provides graceful degradation.
- **Energy/carbon per task:** real Vulkan iGPUs draw real watts. Report **joules per solved task**, local vs. cloud ([Phase 1 hook](10-migration-and-rollout.md#phase-1--agent-traces-ship-standalone-high-roi-)) — a sustainability framing single-process simulations can't make.
- **Heterogeneous-hardware diversity:** model diversity (paper) becomes *hardware* diversity too — different nodes, different local models, genuinely independent failure domains.

## 5. Concrete demos & experiments to build (ranked by wow-per-effort)

| # | Demo / experiment | What it shows | Effort |
|:--|:--|:--|:--|
| 1 | **Live convergence** — board graph filling, critiques resolving, convergence sparkline rising, Decider landing | Coordination made visible | Low (falls out of [doc 13](13-ui-showcase-density.md)) |
| 2 | **Kill-a-node resilience** — partition mid-task, watch stigmergic heal vs traditional stall | The §4 killer result | Med (needs the stigmergic variant) |
| 3 | **Variant A/B** — same query, traditional vs stigmergic (vs PatchBoard), side by side | One engine, three regimes | Med |
| 4 | **Cost/locality frontier** — accuracy vs $ vs latency across model mixes | Cost-aware coordination | Low (instrument the cost path) |
| 5 | **Counterfactual replay** — suppress an agent's turn, re-run from the event log | Causal analysis instrument | Med (needs fork-from-event) |
| 6 | **Decay phase transition** — sweep the half-life, plot convergence vs decay rate | SOC / phase-transition result | Low once the stigmergic variant exists |
| 7 | **NL vs schema token economy** — same long-horizon task, traditional vs PatchBoard, tokens/success | The 2025-vs-2026 paper trade, measured | Med (needs PatchBoard) |

### 5.1 What V1 must lay down so the novel demos aren't a rewrite

The variants and most experiments are built *later/separately*, but several demos above die quietly if V1 skips a hook. Build these hooks during V1 even though the payoff is later (tracked in [doc 10 Phase E](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation) and the phase tasks):

| Demo / experiment | Needs a variant? | V1 groundwork that must exist (don't skip) |
|:--|:--|:--|
| 1 — Live convergence | No | Trace pipeline + board events + the [doc 13](13-ui-showcase-density.md) surfaces |
| 2 — Kill-a-node resilience | Yes (stigmergic) | **Node-health + graceful degradation** in the substrate; **failure-injection tooling** (Phase E) |
| 3 — Variant A/B | Yes | The **`CoordinationVariant` seam** + the **A/B harness** (Phase E); the dropdown + panel registry ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)) |
| 4 — Cost/locality frontier | No | **Per-task / per-model / per-node cost + `joules_estimate`** captured from Phase 1 |
| 5 — Counterfactual replay | No | **Fork-from-event** event log (Phase 2), not just linear replay |
| 6 — Decay phase transition | Yes (stigmergic) | **Pluggable decay** in the derived-fields hook (seam rule 5) so the half-life is a swept parameter, not a constant |
| 7 — NL vs schema economy | Yes (PatchBoard) | Variant-agnostic `board_events` + per-turn token accounting (Phases 1–2) |

> [!IMPORTANT] The through-line
> Every row that says "No" is a demo you can ship from V1 alone — front-load those for the showcase. Every "Yes" row still has a **V1 hook** that, if missed, turns the variant into a rewrite. This is why the [seams checklist](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms) is a merge gate ([doc 10 §1](10-migration-and-rollout.md#1-sequencing-rationale)).

## 6. Caveats (so the showcase stays honest)

- The base algorithm is the 2025 paper's; lead with *distribution + the multi-regime engine + legibility*, not "we invented the blackboard."
- **PatchBoard is published prior art, implemented as an attributed variant.** [Zhang, Shi & Wang (2026)](https://arxiv.org/abs/2605.29313) published the JSON-Patch + deterministic-kernel + write-contracts design under that name; [doc 11](11-variant-patchboard.md) implements it with citation, and the core deliberately absorbed only its *determinism lessons* ([02 §2.4](02-peer-review.md#24-patchboard--json-patch--deterministic-kernel-suggestion-4--defer-to-variant-absorb-its-determinism-lessons)). Their results are also the strongest external evidence for that variant: 84.6% vs 30.8% (LangGraph) on matched ALFWorld episodes at the lowest tokens-per-success, and **zero** committed-state contamination under fault injection.
- The stigmergic-robustness result is a **hypothesis** until measured — present it as the experiment the system was designed to run, not a proven fact ([16 §9](16-variant-stigmergic.md#9-open-questions-gates-before-building)).
- N=3 nodes is small; frame distributed claims as a *testbed/existence proof*, with scaling as future work.

---

➡️ Back to the [Proposal Index](README.md). For the variant mechanics see [doc 11](11-variant-patchboard.md) and [doc 16](16-variant-stigmergic.md); for the demos' UI see [doc 13](13-ui-showcase-density.md).
