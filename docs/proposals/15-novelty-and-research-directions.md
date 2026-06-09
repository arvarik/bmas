[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Implementing with Antigravity Agents](14-implementing-with-antigravity-agents.md) | [🧬 Extensibility & Variants](11-extensibility-and-variants.md)

# 15 — Novelty, Research Directions & the Distributed Angle

> [!ABSTRACT]
> This document answers three questions for the showcase: **what is genuinely novel here** (stated defensibly, separating prior art from contribution), **what academic deep dives** the system enables, and **what falls out specifically from deploying a blackboard MAS as a real distributed system** rather than a single-process simulation. It is the "why this matters" companion to the engineering docs.

---

## 1. Honest framing: prior art vs. contribution

Be precise about this when you present — credibility comes from *not* overclaiming.

| Layer | Status | Who |
|:--|:--|:--|
| Blackboard architecture | Prior art (1985) | Hayes-Roth; Nii |
| Blackboard for **LLM** MAS (LbMAS): control unit + roles + consensus | Prior art (2025) | [Han & Zhang](https://arxiv.org/abs/2507.01701) |
| Blackboard LLM-MAS where agents **volunteer** based on board content (pull-flavored) | Prior art (2025) | [Salemi et al.](https://arxiv.org/abs/2510.01285) |
| **Schema-grounded JSON-Patch mutation through a deterministic kernel ("PatchBoard")** | **Prior art (2026)** | [Zhang, Shi & Wang, arXiv:2605.29313](https://arxiv.org/abs/2605.29313) — independently the same design *and the same name* as [doc 04](04-blackboard-protocol.md); we cite it and import its validated lessons |
| **A *physically distributed* blackboard LLM-MAS on heterogeneous edge hardware** | **Contribution** | this project |
| **A pressure-field substrate that runs *both* a control-unit and a roleless-stigmergic regime unchanged** | **Contribution** | this project ([doc 11](11-extensibility-and-variants.md)) |
| **Cost/locality-aware coordination** ($0 local edge vs. cloud, routed by triage) on a blackboard | **Contribution** | this project |
| **Real-time legibility of emergent coordination** (live pressure field, traces, consensus) | **Contribution (instrument)** | this project ([doc 13](13-ui-showcase-density.md)) |

> [!IMPORTANT] The one-sentence pitch
> *"The paper's LbMAS is a logical, single-process simulation where every agent is an LLM call; we built the first **physically distributed** blackboard multi-agent system on real heterogeneous edge hardware, with a coordination substrate that can run the paper's control-unit regime **or** a fully decentralized stigmergic regime on the same machinery — and we made the emergence visible."*

## 2. What's novel enough to showcase

### 2.1 Distribution is the headline
The paper has **no notion of hosts, network, locality, or failure** — all "agents" are LLM calls inside one program. The moment you make the blackboard a *real* shared substrate (Redis) across *real* nodes, a whole class of systems questions appears that the paper could not ask:
- agents coordinating through shared state over a network, with real concurrency and a real serialization point (the [kernel](04-blackboard-protocol.md));
- **locality**: each node has a local $0 inference engine *and* cloud access — placement matters;
- **failure**: nodes can partition; the simulation can't.

This reframes LbMAS from an *algorithm* into a *distributed system*. That reframing is the contribution.

### 2.2 One substrate, two regimes (the pressure field)
The elegant idea ([doc 11](11-extensibility-and-variants.md), [doc 05 §1.1](05-control-unit.md#11-the-coordinator-narration-agent-optional-showcase-flourish)): the control unit's scheduling heuristics are all special cases of *"orient toward the highest-pressure region."* Define `pressure` as generalized salience, and:
- **V1 (paper):** an LLM control unit *reads* the pressure field to pick agents.
- **V2 (novel):** roleless `universal` actors *self-activate* on pressure gradients with temporal decay — **no control unit at all**.

Both run on the *same* kernel, board, traces, and UI. Showing the **A/B of directed vs. emergent coordination on identical hardware and queries** is a genuinely novel demo — the paper only ablated the control unit crudely (Table 5); you can run a *true* stigmergic regime.

### 2.3 Cost/locality-aware coordination
The paper randomly assigns LLMs to agents *for diversity* and measures tokens. You route by **triage complexity** to a **cost/latency/locality frontier** ($0 local 4B ↔ cloud Pro) on real hardware. So you can report **dollars, latency, and energy per solved task**, not just tokens — and study *when a $0 local model suffices vs. when cloud is worth it inside a blackboard loop.*

### 2.4 Legible emergence (interpretability instrument)
Most MAS work reports final accuracy. Your live pressure-field heatmap, parallel agent-trace lanes, and a consensus-forming sparkline make the **process of emergence** observable. That's both a showcase wow-factor and a real **interpretability instrument for multi-agent dynamics** — rare in the literature.

## 3. Academic deep dives (grouped by field)

### 3.1 Self-organization & swarm intelligence
- **Does emergent (stigmergic) consensus match directed (control-unit) consensus?** Extend the paper's ablation into a *true* roleless regime with decay. Measure the accuracy/cost gap.
- **Self-organized criticality:** model the board's pressure field as a dynamical system. Are there attractors (consensus = basin), damping (decay), oscillation/deadlock regimes? Does the system sit near a critical point between frozen and chaotic? (Connects to Bak's SOC, ant-colony optimization.)
- **Decay-rate phase transitions:** sweep the pheromone decay constant `λ`. Predict a phase transition: too-fast decay → no memory → no convergence; too-slow → stale dominance → premature lock-in. Locating that transition is a clean, publishable result.

### 3.2 Distributed systems
- **What consistency model does a blackboard MAS actually need?** You use optimistic concurrency (CAS on `rev`) through a single kernel. Explore the spectrum: single-writer kernel (now) → **CRDT-replicated board per node** for partition tolerance → eventual consistency. Where does coordination quality break?
- **CAP for cognition:** the blackboard is shared mutable state; the CAP trade-offs are real here. Characterize behavior under partition for *both* regimes (see §4).
- **The kernel as a contention bottleneck / SPOF:** measure throughput limits; propose sharding the board by pressure-region.

### 3.3 Information theory & token economy
- **Information gain per patch:** treat the board as a channel. Measure bits-per-patch, redundancy (what the [cleaner](05-control-unit.md#2-the-paper-role-group) removes), and **board entropy over time** (high early → low at consensus). Reframe the paper's token savings information-theoretically.
- **Optimal stopping:** the decider is an optimal-stopping problem (stop when expected marginal information < cost). Formalize it.

### 3.4 LLM-MAS scaling & diversity
- **Agent scaling curves:** performance vs. `n` experts and vs. `max_rounds` (paper fixed rounds=4; map the diminishing-returns curve on real hardware).
- **Heterogeneity frontier:** empirically chart accuracy vs. the local/cloud model mix — when does diversity (paper's claim) actually pay, and at what cost?

### 3.5 Causality & replay (enabled by event-sourcing)
Because every board mutation is an **event** ([doc 04](04-blackboard-protocol.md)), you can **replay and fork** any task. That makes **counterfactual analysis** possible: *"replay this task but suppress agent X's critique — does consensus change?"* A causal-inference instrument for multi-agent reasoning that a stateful, non-event-sourced system cannot offer.

## 4. The distributed angle: what *only* physical distribution gives you

> [!TIP] The killer experiment
> **Stigmergy predicts robustness to agent loss** (ant colonies don't collapse when ants die). **Hypothesis: the roleless stigmergic regime (V2) is more partition-/failure-tolerant than the control-unit regime (V1).** Kill a node mid-task and measure degradation in each regime. If V2 heals (decay + self-activation route around the loss) while V1 stalls (the control unit waits on a dead role), that is a **striking, novel, biologically-motivated result** — and a jaw-dropping live demo.

Other distribution-only contributions:
- **Locality-aware placement:** an optimization problem absent from any single-process MAS — which persona runs on which node given (a) network latency to the shared board and (b) the $0 local inference endpoint. ([doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles) "home node" idea is the seed.)
- **Partition healing via decay:** temporal decay isn't just for stigmergy — under a network partition, stale entries naturally lose pressure, so the board *self-heals* toward the still-connected agents. Test whether decay alone provides graceful degradation.
- **Energy/carbon per task:** real Vulkan iGPUs draw real watts. Report **joules per solved task**, local vs. cloud — a sustainability framing single-process simulations can't make.
- **Heterogeneous-hardware diversity:** model diversity (paper) becomes *hardware* diversity too — different nodes, different local models, genuinely independent failure domains.

## 5. Concrete demos & experiments to build (ranked by wow-per-effort)

| # | Demo / experiment | What it shows | Effort |
|:--|:--|:--|:--|
| 1 | **Live consensus formation** — pressure heatmap cooling, consensus sparkline rising | Emergence made visible | Low (falls out of [doc 13](13-ui-showcase-density.md)) |
| 2 | **Kill-a-node resilience** — partition mid-task, watch V2 heal vs V1 stall | The §4 killer result | Med (needs V2) |
| 3 | **V1 vs V2 A/B** — same query, control-unit vs stigmergic, side by side | One substrate, two regimes | Med |
| 4 | **Cost/locality frontier** — accuracy vs $ vs latency across model mixes | Cost-aware coordination | Low (instrument the cost path) |
| 5 | **Counterfactual replay** — suppress an agent's turn, re-run from the event log | Causal analysis instrument | Med (needs event-sourced board) |
| 6 | **Decay phase transition** — sweep `λ`, plot convergence vs decay rate | SOC / phase-transition result | Low once V2 exists |

### 5.1 What V1 must lay down so the novel demos aren't a rewrite

V2 and most experiments are built *later/separately*, but several of the demos above die quietly if V1 skips a hook. Build these hooks during V1 even though the payoff is later (tracked in [doc 10 Phase E](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation) and the phase tasks):

| Demo / experiment | Needs V2? | V1 groundwork that must exist (don't skip) |
|:--|:--|:--|
| 1 — Live consensus formation | No | Pressure field + event stream + the [doc 13](13-ui-showcase-density.md) surfaces |
| 2 — Kill-a-node resilience | Yes | V1 substrate must have **node-health + graceful degradation**; **failure-injection tooling** (Phase E) |
| 3 — V1 vs V2 A/B | Yes | The **`CoordinationStrategy` seam** + the **A/B harness** (Phase E); UI side-by-side surface |
| 4 — Cost/locality frontier | No | **Per-task / per-model / per-node cost + `joules_estimate`** captured from Phase 1 |
| 5 — Counterfactual replay | No | **Fork-from-event** event log (Phase 2), not just linear replay |
| 6 — Decay phase transition | Yes | **Pluggable decay** (seams checklist) so `λ` is a swept parameter, not a constant |

> [!IMPORTANT] The through-line
> Every row that says "No" in *Needs V2?* is a demo you can ship from V1 alone — front-load those for the showcase. Every "Yes" row still has a **V1 hook** that, if missed, turns V2 into a rewrite. This is why the [seams checklist](11-extensibility-and-variants.md#6-the-seams-checklist-enforce-in-v1) is a merge gate ([doc 10 §1](10-migration-and-rollout.md#1-sequencing-rationale)).

## 6. Caveats (so the showcase stays honest)

- The base algorithm is the paper's; lead with *distribution + the dual-regime substrate + legibility*, not "we invented the blackboard."
- **The validated-patch substrate is published prior art.** [PatchBoard (Zhang, Shi & Wang 2026, arXiv:2605.29313)](https://arxiv.org/abs/2605.29313) independently published the JSON-Patch + deterministic-kernel + write-contracts design under the same name we use in [doc 04](04-blackboard-protocol.md). Cite it everywhere the substrate is described; we keep the name as an **attributed reference**, not a claim. The contribution is what runs *on* the substrate (distribution, the dual-regime pressure field, legible emergence) — never the substrate itself. Their results are also the strongest external evidence the substrate works: 84.6% vs 30.8% (LangGraph) on 630 matched ALFWorld episodes at the lowest tokens-per-success, and **zero** committed-state contamination from invalid/unauthorized patches under fault injection.
- The stigmergic-robustness result is a **hypothesis** until measured — present it as the experiment you designed the system to run, not a proven fact.
- N=3 nodes is small; frame distributed claims as a *testbed/existence proof*, with scaling as future work.

---

➡️ Back to the [Proposal Index](README.md). For the regime mechanics see [doc 11](11-extensibility-and-variants.md); for the demos' UI see [doc 13](13-ui-showcase-density.md).
