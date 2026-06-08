[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Migration & Rollout](10-migration-and-rollout.md) | [➡️ Next: Hermes & Node Topology](12-hermes-and-node-topology.md)

# 11 — Extensibility & the Pure-Stigmergic Variant

> [!ABSTRACT]
> **V1 implements the paper** (Control Unit + named roles). But the architecture must be built so that a future **pure-stigmergic variant** — no control unit, no roles, identical "universal actor" agents coordinating only through pressure gradients and decaying pheromones — can be dropped in *without rewriting the substrate*. This document defines the abstraction seams that make both modes share one engine, and specifies the stigmergic variant precisely enough to build later.

---

## 1. Two coordination regimes, one substrate

| | **V1 — Paper (bMAS)** | **V2 — Pure Stigmergic** |
|:--|:--|:--|
| Control | LLM-assisted Control Unit (referee) | **None.** Emergent from pressure + decay |
| Roles | 6 named roles (Planner, Critic, …) | **None.** All agents are identical universal actors |
| Activation | CU schedules relevant role per turn | Agent self-activates on observing high local pressure |
| Termination | Decider: consensus ≥ threshold | All regional pressures fall below activation threshold ("stable basin") |
| Anti-stall | round/duration caps | **Temporal decay** of patch fitness forces continuous re-examination |
| Hermes use | push (daemon dispatch) | pull (Hermes crons poll pressure map) |

The critical insight: **both regimes operate on the same PatchBoard.** Agents read a board, compute where to act, propose patches, the kernel validates and commits, and the board's derived fields update. What differs is *who decides which agent acts and when* — and that is exactly one pluggable component.

```
        ┌──────────────────────── SHARED SUBSTRATE (unchanged across variants) ─────────────────────────┐
        │  PatchBoard (entries + patch log)  ·  Deterministic Kernel  ·  Pressure field  ·  Trace stream   │
        └───────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                                     │ implements
                          ┌──────────────────────────┴──────────────────────────┐
                          ▼                                                       ▼
              CoordinationStrategy = ControlUnitStrategy            CoordinationStrategy = StigmergicStrategy
              (V1: schedule roles via OODA + Decider)               (V2: agents self-activate on pressure; decay)
```

## 2. The seam: `CoordinationStrategy`

Make coordination a strategy interface. The daemon owns the board and kernel; a strategy object decides activation and termination. V1 ships `ControlUnitStrategy`; V2 later ships `StigmergicStrategy`. **Nothing in the kernel, board, schema, traces, or UI needs to change to swap strategies.**

```python
# daemon/src/core/coordination.py  (the seam)
class CoordinationStrategy(Protocol):
    async def genesis(self, task_id: str, objective: str) -> None: ...
    async def step(self, task_id: str, board: Board) -> StepResult: ...
    #   StepResult = { activations: list[Activation], terminal: bool, reason: str|None }
    def is_terminal(self, board: Board) -> tuple[bool, str | None]: ...

# V1 — the paper
class ControlUnitStrategy(CoordinationStrategy):
    """OODA scheduler + Decider. Roles, consensus threshold, caps. (doc 05)"""

# V2 — pure stigmergic (future; stub the class now, implement later)
class StigmergicStrategy(CoordinationStrategy):
    """No roles. step() returns activations for every agent over a high-pressure
    region; terminal when max_pressure < activation_threshold. (this doc §4)"""
```

Selected in `bmas.yaml`:

```yaml
coordination:
  strategy: control_unit        # control_unit (V1) | stigmergic (V2) | legacy_pipeline
  # ...strategy-specific blocks below...
```

> [!IMPORTANT] The one rule that guarantees extensibility
> **Roles and the Control Unit must be confined to the strategy layer.** The kernel authorizes patches by *capability*, not by hardcoded role names; the board stores `author` as an opaque string; traces are role-agnostic; the UI colors by author identity, not by a fixed enum. If any lower layer hardcodes "planner/executor/auditor", the stigmergic variant becomes a rewrite. See the [non-negotiable seams checklist](#6-the-seams-checklist-enforce-in-v1) below.

## 3. The pressure field (generalizes salience)

[Doc 04 §6](04-blackboard-protocol.md#6-salience-the-pragmatic-pheromone) defines per-entry `salience` (importance). The stigmergic variant needs a complementary, region-level signal: **pressure** = *how much unfinished work exists here*. We add it as a derived field in V1 already, because the V1 Control Unit benefits from it too (it is exactly the "orient" signal).

```
pressure(region) =  unrebutted_critiques(region)        // errors flagged, not fixed
                  +  open_conflicts(region)              // contradictions unresolved
                  +  low_confidence_findings(region)     // weak assertions
                  +  unmet_constraints(region)           // objective sub-goals with no finding
                  −  reinforcement(region)               // accepted, corroborated entries
```

### 3.1 Mapping entry types to pressure terms

The formula above references conceptual terms. This table maps each to concrete board state from [doc 04](04-blackboard-protocol.md):

| Pressure term | Board condition (how the kernel computes it) | Weight key |
|:--|:--|:--|
| `unrebutted_critiques` | Count of entries where `type = "critique"` AND `status = "open"` AND **no** entry with `type = "rebuttal"` exists that `refs` the critique's `id` | `critique` |
| `open_conflicts` | Count of entries where `type = "conflict"` AND `status = "open"` (i.e. the Conflict-Resolver has not posted a resolution) | `conflict` |
| `low_confidence_findings` | Count of entries where `type = "finding"` AND `status = "open"` AND `confidence < confidence_floor` (default `0.5`, configurable in `pressure.confidence_floor`) | `low_confidence` |
| `unmet_constraints` | Count of sub-goals in the `objective` entry's body that have **no** `finding` or `plan` entry with a `refs` pointing to the objective — i.e. aspects of the task that no agent has addressed yet | `unmet_constraint` |
| `reinforcement` | Count of entries in the region with `status = "accepted"` **plus** entries with ≥2 corroborating `refs` from different authors (independent agreement) | `reinforcement` |

> [!NOTE]
> The weights for each term are configured in `pressure.weights` ([§7](#7-config-sketch-both-variants-visible)). The `confidence_floor` threshold is a separate config key under `pressure` to avoid baking a magic number into the kernel.

- A **region** in V1 is simply an entry and its `refs` neighborhood (a sub-graph). The field is computed by the kernel after each commit (same hook that recomputes salience) and stored in `bmas:board:{task}:pressure` (ZSet: region → pressure).
- In V1, `ControlUnitStrategy` reads the top-pressure regions to decide which role to activate ("highest pressure is an open conflict → Conflict-Resolver"). This *replaces hand-coded `if open_critiques` heuristics with a uniform signal* — and makes the V1→V2 transition continuous.
- The UI renders pressure as a **heatmap overlay** on the blackboard graph ([doc 13](13-ui-showcase-density.md)) in both variants — high-pressure regions glow. This is the most visually compelling artifact of the whole system for a showcase.

## 4. The stigmergic variant, specified

When `strategy: stigmergic`, the loop inverts from "CU picks an agent" to "agents pick regions":

```
GENESIS
  Write objective + constraints to the board. Compute initial pressure (every
  unmet constraint is a high-pressure region). No roles assigned.

EMERGENT LOOP (no central scheduler):
  Each universal actor (any node), independently and in parallel:
    1. OBSERVE   read the pressure field; pick a high-pressure region above its
                 personal activation threshold (thresholds vary slightly per
                 agent → diversity, avoids thundering herd).
    2. PROPOSE   generate a patch that attempts to *reduce* that region's pressure.
    3. VALIDATE  kernel applies; pressure is recomputed. PARALLEL VALIDATION:
                 multiple actors may target the same region; the patch that most
                 reduces measured pressure is reinforced (pheromone↑); others decay.
    4. REINFORCE accepted patches deposit "pheromone" (boost reinforcement term);
                 this attracts/repels future actors (stigmergy).

  TEMPORAL DECAY (the anti-stall mechanism):
    Patch fitness / reinforcement decays exponentially over wall-clock time.
    A region that looked "solved" slowly regains pressure unless its solution keeps
    proving robust → forces continuous re-examination, prevents premature lock-in.

TERMINATION
  Stable basin: max region pressure < global activation threshold for a sustained
  window (decay-adjusted). No Decider needed — convergence is emergent.
```

Mechanisms that must exist for V2 (and the V1 seams that anticipate them):

| V2 mechanism | V1 seam that makes it cheap to add |
|:--|:--|
| Pressure field | Built in V1 (§3); CU already consumes it |
| Pheromone reinforcement | `salience` already has a `reinforcement` term; promote to a writable, decaying field |
| Temporal decay | `recency` term in salience is already a decay; generalize to wall-clock exponential decay behind a flag ([04 §6 note](04-blackboard-protocol.md#6-salience-the-pragmatic-pheromone)) |
| Parallel validation of competing patches | Kernel already commits concurrent patches via CAS ([04 §5](04-blackboard-protocol.md#5-optimistic-concurrency)); add a "competition" resolver that scores by pressure-reduction |
| Self-activation (pull) | Hermes crons polling `bmas:board:{task}:pressure` ([doc 12 §6](12-hermes-and-node-topology.md#6-pull-mode-crons-for-the-stigmergic-future), [05 §7](05-control-unit.md#7-optional-future-pure-pull-with-hermes-crons)) |
| Roleless universal actor | A single `universal` persona/profile; kernel capability = "may patch any region" |

## 5. Why build V1 first (and not skip to V2)

- **Observability & trust.** A referee gives a definable terminal state and a streamable consensus score — essential for a *showcase* where you explain what's happening. Pure emergence is mesmerizing but hard to narrate ("why did it stop?").
- **Cost.** Uncapped self-activating agents on cloud LLMs is a billing hazard ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)). V1's caps are the safety net while you tune V2's decay constants.
- **Tuning surface.** Decay rate, activation thresholds, and pheromone weights are finicky. V1 gives you a working baseline and a pressure field to calibrate against before removing the referee.
- **Continuous path.** Because V1 already computes pressure and decaying reinforcement, V2 is "remove the scheduler, let agents read pressure directly" — an evolution, not a rebuild.

## 6. The seams checklist (enforce in V1)

Build these into V1 even though V1 doesn't need all of them, or V2 becomes a rewrite:

- [ ] Coordination lives behind `CoordinationStrategy`; the orchestrator never hardcodes a sequence.
- [ ] Kernel authorizes by **capability profile**, not role name (`can_patch: [finding, critique, …]` or `["*"]`).
- [ ] Board `author`, trace `role`, and UI identity are **opaque strings**, not enums. (Note: doc 08's `AgentRole` enum is a *display convenience* — back it with a fallback color generator for unknown authors so a roleless `universal` actor or a dynamic expert still renders.)
- [ ] `pressure` and `reinforcement` are first-class derived board fields, recomputed in one kernel hook.
- [ ] Decay is a pluggable function (`recency` in V1, exponential wall-clock in V2) selected by config.
- [ ] Agent dispatch supports both `push` (V1) and `pull` (V2) via `participation_mode` ([agent-integration roadmap](../roadmap/agent-integration.md)).
- [ ] Termination is a strategy method (`is_terminal`), not an orchestrator `return`.

## 7. Config sketch (both variants visible)

```yaml
coordination:
  strategy: control_unit          # control_unit | stigmergic | legacy_pipeline

  control_unit:                   # V1
    consensus_threshold: 0.8
    max_rounds: 4
    budget_ceiling_usd: 0.50

  stigmergic:                     # V2 (parsed but inert until strategy=stigmergic)
    activation_threshold: 0.15    # agents act on regions above this pressure
    decay_half_life_s: 120        # pheromone/fitness decay
    stable_window_s: 90           # basin must hold this long to terminate
    actor_count: 6                # identical universal actors
    budget_ceiling_usd: 0.75

pressure:                         # shared; consumed by both strategies + UI heatmap
  weights: { critique: 1.0, conflict: 1.2, low_confidence: 0.6, unmet_constraint: 1.0, reinforcement: -0.8 }
  confidence_floor: 0.5           # findings below this confidence count as low_confidence_findings (§3.1)
```

➡️ Continue to [12 — Hermes & Node Topology](12-hermes-and-node-topology.md).
