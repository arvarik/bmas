[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Novelty & Research Directions](15-novelty-and-research-directions.md) | [➡️ Next: Files & Artifacts](17-files-and-artifacts.md)

# 16 — Variant: True Stigmergic (Pressure Fields & Emergent Coordination)

> [!ABSTRACT]
> The complete specification of the **stigmergic coordination variant**: no control unit, no roles — identical "universal actor" agents that self-activate on a decaying **pressure field** computed over the board, coordinating only through the traces their work leaves in the shared medium (true [stigmergy](https://en.wikipedia.org/wiki/Stigmergy)). This is the project's **research-novelty variant** ([doc 15](15-novelty-and-research-directions.md)) and the third option in the per-task dropdown ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)). Like [doc 11](11-variant-patchboard.md), nothing here is V1 work except honoring the seams it plugs into — but the seams it needs are the most demanding, which is why they are merge gates ([10 §1](10-migration-and-rollout.md#1-sequencing-rationale)).

---

## 1. What "true stigmergy" means here (and what it is not)

In the traditional core, coordination is *refereed*: an LLM Control Unit reads the board and selects who acts ([05 §1](05-control-unit.md#1-the-control-unit-is-a-referee-not-a-brain)). Stigmergic coordination removes the referee entirely. Agents are drawn to where the *environment itself* signals unfinished work — the way ant colonies allocate labor through pheromone gradients, with no ant in charge:

- **No scheduler.** Nothing decides "the critic should act now." There is no critic.
- **No roles.** Every agent is the same `universal` persona ([doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)) with the full toolset. Differentiation is *behavioral and emergent* — an actor that lands on a high-pressure critique region does critic-like work because that is what reduces pressure there.
- **No decider.** Termination is emergent: the system stops when no region's pressure clears any actor's activation threshold for a sustained window (a *stable basin*).
- **The medium is the message.** Agents communicate only by writing entries to the board (same natural-language entries as the core — [04 §1](04-blackboard-protocol.md#1-board-entries-typed-envelopes-natural-language-bodies)); the pressure field is *derived from* those entries, never directly written.

What it is **not**: uncapped chaos. The engine's cost rails (budget ceiling, duration cap, concurrency cap — [05 §5](05-control-unit.md#5-cost-governance--safety-rails)) apply unchanged. Emergence is bounded by the same governor as everything else; only *selection* is decentralized.

## 2. The regime at a glance

| | **Traditional (core)** | **Stigmergic (this doc)** |
|:--|:--|:--|
| Control | LLM CU selects agents per round | **None** — emergent from pressure + decay |
| Roles | 5 constant + AG experts | **None** — N identical `universal` actors |
| Activation | CU dispatch (push) | Self-activation on local pressure > personal threshold (push-simulated now, true pull later — §4) |
| Board | Typed NL entries | **Identical** entries + a derived pressure field |
| Anti-stall | Stall breaker (rounds without accepted entries) | **Temporal decay** — "solved" regions slowly regain pressure unless their solutions keep proving robust |
| Termination | Decider solution / SolE vote | Stable basin: max pressure < threshold for `stable_window_s` |
| Hermes | Daemon dispatch per turn | Same transport; optionally crons polling pressure (§4) |
| Best for | Chat, research, code-gen | Robustness/emergence research, node-loss tolerance, the showcase's most striking demos |

The critical design fact: **the stigmergic variant uses the core's board unchanged.** Same entry envelopes, same Board Gateway, same event log, same traces. What differs is one derived field and who decides to act — exactly the two pluggable seams (`recompute_derived`, `CoordinationVariant.step`).

## 3. The pressure field

Per-entry `salience` ([04 §7](04-blackboard-protocol.md#7-salience-a-cheap-explainable-importance-signal)) answers "how important is this entry?" Pressure answers a different question, region-level: **"how much unfinished work exists here?"**

```
pressure(region) =  w₁·unrebutted_critiques(region)     // errors flagged, not fixed
                  + w₂·open_conflicts(region)           // contradictions unresolved
                  + w₃·low_confidence_findings(region)  // weak assertions
                  + w₄·unmet_constraints(region)        // objective sub-goals with no finding
                  − w₅·reinforcement(region)            // accepted, corroborated work (decaying)
```

### 3.1 Mapping entry types to pressure terms

Every term is computable from board state already defined in [doc 04](04-blackboard-protocol.md) — no new agent behavior required:

| Pressure term | Board condition (deterministic) | Weight key |
|:--|:--|:--|
| `unrebutted_critiques` | `type="critique"` AND `status="open"` AND no `rebuttal` entry `refs` it | `critique` |
| `open_conflicts` | `type="conflict"` AND `status="open"` | `conflict` |
| `low_confidence_findings` | `type="finding"` AND `status="open"` AND `confidence < confidence_floor` (default 0.5) | `low_confidence` |
| `unmet_constraints` | sub-goals in the `objective` body with no `finding`/`plan` entry `refs`-ing the objective | `unmet_constraint` |
| `reinforcement` | entries with ≥2 corroborating `refs` from **different authors** (independent agreement), plus solution-type entries — subject to decay (§3.3) | `reinforcement` |

### 3.2 Regions

A **region** is an entry and its `refs` neighborhood: the ZSet `bmas:board:{task}:pressure` is keyed by entry id, and `pressure(e)` is computed over the sub-graph within `refs`-distance ≤ 1 of `e` — the entry, what it cites, what cites it. Regions overlap by design (a hot critique raises both its own pressure and the finding it targets). "Top-pressure regions" = top-N ZSet members after skipping ids already inside a higher-scoring member's neighborhood.

The field is recomputed in the **`recompute_derived` hook** after every gateway commit (seam rule 5, [03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)) — the same hook where the traditional variant computes salience. This is why the hook must be pluggable in V1: the stigmergic variant registers `pressure + decay` there and **nothing in the gateway changes** ([04 §7](04-blackboard-protocol.md#7-salience-a-cheap-explainable-importance-signal)).

### 3.3 Temporal decay and reinforcement (the anti-stall mechanism)

Reinforcement decays exponentially over wall-clock time: `reinforcement(t) = r₀ · 2^(−(t−t₀)/half_life)`. Consequences, both intended:

- A region that looked "solved" **slowly regains pressure** unless its solution keeps attracting corroboration — forcing continuous re-examination and preventing premature lock-in (the emergent analogue of the core's contested-solution rule, [05 §3](05-control-unit.md#3-consensus--termination)).
- Stale debris loses its pull. There is no Cleaner role; **decay is the cleaner**. (Entries are not removed — their regions just go cold. Board growth is bounded by the budget/duration rails, and `view_budget_tokens` budgeted mode handles serialization size — [03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target).)

Decay must be implemented as a **pluggable function in the derived hook** (recency-scaling for traditional salience; exponential wall-clock here) selected by variant — never hard-coded into the gateway.

## 4. Pull-mode activation

Two implementation stages, deliberately separated so the variant doesn't block on infrastructure:

**Stage A — daemon-simulated stigmergy (build first).** The daemon's task runner still ticks `variant.step()`, but `step()` contains no scheduler intelligence: it reads the pressure ZSet, finds regions above each actor's threshold, and dispatches up to `max_concurrent_activations` actors (push transport, same as the core). Semantically stigmergic — selection is purely field-driven — while reusing the entire dispatch path. This stage is sufficient for all the research demos.

**Stage B — true pull (Hermes crons).** Each node runs a Hermes cron that polls `bmas:board:{task}:pressure` (read-only daemon endpoint, `BMAS_NODE_KEY`-authenticated) and self-activates when a region exceeds the node's threshold, submitting its own run. The daemon becomes a pure validator/bookkeeper. **Provisioning caveat (verified live):** `features.jobs_admin=false` — crons must be created via the Hermes CLI/`config.yaml` over SSH, not HTTP ([doc 12 §6](12-hermes-and-node-topology.md#6-pull-mode-crons-for-the-stigmergic-future), [10 Q9](10-migration-and-rollout.md#3-open-questions-verify-before-building)). The dispatch seam's `participation_mode: push|pull` per node (seam rule 6) is what makes Stage B a config flip.

**Thundering-herd control:** actor thresholds are jittered per actor (`activation_threshold ± jitter`), and an actor "claims" a region with a short-TTL Redis lock before running, so three actors don't all attack the same hottest region — unless competition is *wanted* (§5, parallel proposals are legitimate stigmergy).

## 5. The emergent loop, specified

```
GENESIS (variant.genesis)
  Post the objective entry (+ attachment entries, doc 17 §4). Parse the objective's
  sub-goals → initial unmet_constraints make every sub-goal a high-pressure region.
  No roles assigned, no experts generated. Spawn the actor pool (actor_count
  identical `universal` actors, ids universal-1…N — opaque strings, seam rule 3).

EMERGENT LOOP (no central scheduler; per actor, independently):
  1. OBSERVE    read the pressure field; claim a region above the actor's personal
                threshold (jittered — diversity without roles).
  2. ACT        one Hermes run: full tools, the claimed region's entries (and refs)
                as focus + budgeted board context; the actor does whatever reduces
                pressure — research, critique, rebut, reconcile, synthesize.
  3. PROPOSE    return ordinary entries_v1 envelopes (04 §3) tagged with the claimed
                region; the Board Gateway validates/commits exactly as for the core.
  4. FIELD      recompute_derived: pressure falls where work landed (critique answered,
                constraint met), rises where new critiques/conflicts appeared;
                reinforcement deposits on corroborated entries ("pheromone").
  COMPETITION   two actors on overlapping regions both commit (append-only — no
                conflict); the proposal that reduces measured pressure more gains
                reinforcement; the other's contribution decays. Selection by
                consequence, not by judge.

TERMINATION (variant.is_terminal)
  Stable basin: max region pressure < activation_threshold sustained for
  stable_window_s (decay-adjusted). Then a final SYNTHESIS activation: one actor
  is asked to write the answer from the board (the only scripted step — emergent
  systems are bad at knowing they're done writing prose). Engine rails (budget,
  duration, concurrency) remain hard stops throughout, with terminated_by recorded.
```

## 6. UI extensibility

The `stigmergic` `VariantUIAdapter` ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)) registers:

- **Graph overlay: the pressure heatmap.** High-pressure regions glow through the overlay slot on the core entry graph ([13 §3](13-ui-showcase-density.md#3-the-mission-layout-a-multi-panel-command-center)), using the shared heat-ramp tokens ([13 §7](13-ui-showcase-density.md#7-component--token-additions)). Watching heat bloom around a fresh critique and drain as an actor answers it is the single most compelling visual in the whole project — and it is *honest*: the glow is the actual coordination signal, not decoration.
- **Mission panels** ([13 §3.1](13-ui-showcase-density.md#31-variants-in-the-cockpit)): a **pressure/decay strip** (max-pressure sparkline falling toward the threshold line = visible convergence; per-term stacked contributions); an **actor-claims lane** showing which actor holds which region (claims/releases animating).
- **Agent Minds**: a flat roster of `universal-1…N` (fallback colors — no role enum), each card showing its claimed region + live reasoning. Idle actors muted, displaying their personal threshold.
- **Convergence strip** plug-in: `max_pressure ↓` replaces the core's open-critiques sparkline.
- **Event handlers**: namespaced events — `pressure_updated` (throttled — see note), `region_claimed`/`region_released`, `reinforcement_deposited`, `pheromone_decayed`, `basin_reached`.

> [!IMPORTANT] Performance note for the adapter
> Decay means pressure changes *continuously*, not just on commits. Emit `pressure_updated` on a fixed cadence (e.g. 2s) with the full ZSet top-K, never per-recompute; the heatmap interpolates between updates client-side. The 60fps rules in [13 §5](13-ui-showcase-density.md#5-keeping-it-snappy-density-without-jank) are binding.

## 7. Seam mapping & config

```python
# daemon/src/core/variants/stigmergic.py  (sketch)
class StigmergicVariant(CoordinationVariant):
    name = "stigmergic"

    async def genesis(self, task):           # objective + attachments + actor pool; no AG, no roles
    def build_turn_payload(self, task, actor, board):
        # claimed region entries in full + budgeted remainder + the actor's threshold context
    def parse_agent_response(self, task, actor, raw):
        # entries_v1 — IDENTICAL to traditional (the gateway path is shared)
    async def apply(self, task, mutations):  # core Board Gateway, unchanged
    async def step(self, task, board):       # Stage A: field-driven activation (§4); no LLM anywhere
    def is_terminal(self, board):            # stable-basin test (§5)
```

```yaml
coordination:
  variant: stigmergic                 # selected per task via the dropdown
  stigmergic:                         # parsed but inert until selected (Phase-0 validation)
    actor_count: 6                    # identical universal actors (2 per node on the 3-node cluster)
    activation_threshold: 0.15
    threshold_jitter: 0.05            # per-actor diversity (§4)
    decay_half_life_s: 120            # reinforcement decay (§3.3)
    stable_window_s: 90               # basin must hold this long to terminate
    budget_ceiling_usd: 0.75          # engine rail — emergent ≠ uncapped
    participation_mode: push          # push (Stage A) | pull (Stage B crons)
    pressure:
      weights: { critique: 1.0, conflict: 1.2, low_confidence: 0.6, unmet_constraint: 1.0, reinforcement: -0.8 }
      confidence_floor: 0.5
```

**What V1 must have built for this to be a drop-in** (the seams checklist rows this variant exercises hardest — [03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)): pluggable `recompute_derived` (rule 5), pluggable decay (rule 5), opaque authors end-to-end (rule 3), capability-based gateway auth — the `universal` capability profile is "may write any entry type" ([04 §4](04-blackboard-protocol.md#capability-profiles-who-may-write-what)) (rule 4), `participation_mode` in dispatch (rule 6), and `is_terminal` as a variant method (rule 7).

## 8. Why the traditional core ships first

- **Observability & trust.** A referee gives a definable terminal state and a narratable loop — essential while the substrate is being debugged. Pure emergence is mesmerizing but hard to debug *and* explain simultaneously ("why did it stop?" must have a better answer than "the field went quiet").
- **Cost.** Self-activating agents on cloud LLMs without proven rails is a billing hazard. The core's rails get hardened on refereed behavior first.
- **Tuning surface.** `decay_half_life_s`, thresholds, jitter, and weights are genuinely finicky, and tuning on paid models is expensive. The traditional variant produces the labeled task corpus + cost baselines to calibrate against.
- **The research story needs a control.** The variant's claims ([doc 15](15-novelty-and-research-directions.md)) are comparative: same tasks, same engine, same nodes — swap only `coordination.variant`. That A/B is only meaningful if `traditional` exists and is solid ([10 Phase E](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation)).

**The experiments this variant unlocks** (ranked in [15 §5](15-novelty-and-research-directions.md#5-concrete-demos--experiments-to-build-ranked-by-wow-per-effort)): the **kill-a-node resilience demo** (no scheduler to kill — actors on surviving nodes keep following the field; degradation should be graceful, and that is measurable); convergence-dynamics studies (pressure trajectories across task types); referee-vs-emergence quality/cost frontiers; threshold-diversity ablations.

## 9. Open questions (gates before building)

| # | Question | Why it gates |
|:--|:--|:--|
| S1 | Do NL-entry agents *reduce* pressure reliably, or thrash? | The loop assumes acting on a region usually lowers its pressure. If actors mostly add new critiques (raising it), tasks never reach a basin. Measure on the traditional corpus first: simulate the field over completed traditional-task event logs (free — replay, no LLM) before spending a dollar on live emergence. |
| S2 | `unmet_constraints` extraction quality | Parsing sub-goals from the objective body is the one semi-fuzzy pressure term. Options: a genesis-time LLM decomposition (one call), or explicit operator-provided sub-goals. Decide before building. |
| S3 | Termination honesty | A basin can mean "solved" or "everyone's threshold is mis-tuned." The synthesis step + `terminated_by` evidence (pressure trajectory attached to the task record) must make the difference auditable. |
| S4 | Stage-B cron mechanics | `jobs_admin=false` — CLI/SSH provisioning only ([Q9](10-migration-and-rollout.md#3-open-questions-verify-before-building)); also per-node Hermes runs submitting *themselves* need a turn-accounting path in the daemon (runs arriving without a dispatching turn). Stage A defers all of this. |
| S5 | Cost profile vs traditional | Each activation pays the ~16k-token Hermes context floor ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)). Field-driven activation may fire more, smaller turns than a CU — the budget arithmetic needs live measurement at small `actor_count` first. |

➡️ Continue to [17 — Files & Artifacts](17-files-and-artifacts.md). The other variant — PatchBoard — is [doc 11](11-variant-patchboard.md).
