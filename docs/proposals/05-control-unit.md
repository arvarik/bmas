[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Blackboard Protocol](04-blackboard-protocol.md) | [➡️ Next: Agent Traces](06-agent-traces.md)

# 05 — Control Unit, Roles & the Blackboard Cycle

> [!ABSTRACT]
> The Control Unit is the referee that replaces the puppeteer. This document specifies the paper-faithful cycle: **an LLM control unit that selects which agents act each round**, the constant role group (planner/decider/critic/conflict-resolver/cleaner) plus AG-generated experts with model-pool diversity, decider/majority-vote termination (the paper's two answer paths), private-board conflict resolution, the Cleaner's remove semantics — and, critically for a homelab, the deterministic cost-governance rails wrapped around all of it.

---

> [!IMPORTANT] Everything here is the `traditional` variant, not the engine
> This document specifies `daemon/src/core/variants/traditional.py` — one implementation of the `CoordinationVariant` seam ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)). The gateway, board store, traces, files, and UI shell know nothing about "Control Unit" or role names. The [PatchBoard](11-variant-patchboard.md) and [stigmergic](16-variant-stigmergic.md) variants swap in without touching any lower layer. As you read, treat roles and the scheduler as living **only** in this layer.

## 1. The Control Unit is a referee, not a brain

Today, `Orchestrator._standard_flow` *is* the solution logic ([Gap G1](01-gap-analysis.md#2-evidence-the-control-component-encodes-the-solution)). The Control Unit (CU) removes that logic and keeps only **selection and termination** — the paper's eq. (2): `{selected agents} = ConU(query, board, {agent ability descriptions})`.

```python
# daemon/src/core/variants/traditional.py  (sketch)
class TraditionalVariant(CoordinationVariant):
    name = "traditional"

    async def genesis(self, task):
        tier = await self.triage.classify(task.query)            # existing triage, now effective
        self.experts = await self._generate_experts(task, tier)  # AG — one LiteLLM call (§2.1)
        await self.gateway.append(task.id, "control_unit", ["decision_writer"],
                                  [objective_entry(task)], turn_id="genesis")
        await self.files.attach_uploads(task)                     # doc 17 §4

    async def step(self, task, board) -> StepResult:
        # 1) deterministic guards — no LLM (§5)
        if accepted_solution(board):            return StepResult(terminal=True, reason="solution")
        if board.round >= self.max_rounds:      return StepResult(terminal=True, reason="max_rounds")
        if board.budget_spent >= self.ceiling:  return StepResult(terminal=True, reason="budget")
        if self._stalled(board):                return StepResult(terminal=True, reason="stalled")
        # 2) the paper's CU — one bare LiteLLM call (never a Hermes run)
        selected = await self._cu_select(task.query, board, self.agent_descriptions)
        return StepResult(terminal=False,
                          activations=self._to_activations(selected))   # capped, node-assigned

    async def finalize(self, task, board, reason):
        answer = accepted_solution(board) or await self._solution_extraction(task, board)  # §3
        await self.files.sync_artifacts(task)                     # doc 17 §6
        return answer
```

### 1.1 The CU selection call, precisely

One **bare LiteLLM call** per round (cheap — the prompt is the board, which the Cleaner keeps small; there is no Hermes context floor because it is not a Hermes run):

- **Input:** the query; the serialized board ([04 §10 `serialize_for_prompt`](04-blackboard-protocol.md#10-blackboard-api-surface-replaces-ad-hoc-methods)); the roster — constant roles + generated experts, each as `(name, ability description D_i)`; the round number and remaining budget.
- **Instruction** (the paper's CU prompt, adapted): *"Select the agents that should act next, based on what the blackboard currently needs. Select the decider only when the board plausibly contains enough to answer. Return JSON: `{"selected": ["critic", "expert.valuation"], "rationale": "…"}`."*
- **Output handling:** parse the JSON; drop unknown names (warn); clamp to `max_concurrent_activations`; empty/garbled output after 1 retry → **deterministic fallback** (below). The rationale string is kept and surfaced in the UI ([§1.2](#12-the-coordinator-narration-lane-optional-showcase-flourish)).

**Deterministic fallback policy** (also useful as a cheaper `cu_mode: heuristic_first` option, off by default): round 1 → planner + all experts; open `critique`s without `rebuttal` → the critiqued authors; open `conflict` → conflict-resolver; entry count > cleaner threshold → cleaner; otherwise → decider. These mirror the paper's intended dynamics but are a *fallback*, not the mechanism — the paper's CU is an LLM and ours is too.

> [!NOTE] Why an LLM CU is worth its cost (the paper's own ablation)
> Removing the control unit (agents self-select every round) left accuracy roughly unchanged but cost **~3–4× the tokens** (LbMAS Table 5: e.g. MMLU 5.07M → 18.8M). The CU's per-round selection call is the token governor. Conversely, this is why we don't make selection purely heuristic: the paper's result is that *LLM selection against the live board* is what adapts the collaboration to the task.

### 1.2 The Coordinator narration lane (optional showcase flourish)

The CU's selection rationale is a showcase asset: the operator can watch the system *decide what to think about*. Render each round's `{selected, rationale}` as a **Coordinator lane** event in the UI ([13 §3](13-ui-showcase-density.md#3-the-mission-layout-a-multi-panel-command-center)). Hard constraints: the narration is the *same* selection call (no extra spend); a malformed rationale never blocks the loop; `coordination.traditional.coordinator_narration: false` hides the lane entirely. If richer narration is ever wanted, route the selection call through a thin `coordinator` Hermes profile — but only behind this flag, off the critical path, with the deterministic fallback intact ([doc 12 §2.1](12-hermes-and-node-topology.md#21-should-the-control-unit-be-a-profile-mostly-no)).

## 2. The agent group

The paper's group: **5 constant roles** + **n query-generated experts**. Roles are **logical** — a persona + a capability profile ([04 §4](04-blackboard-protocol.md#capability-profiles-who-may-write-what)) — decoupled from physical nodes: any node can assume any role per turn (the paper's model; the [roadmap](../roadmap/control-unit.md#dynamic-role-assignment-6-role-bmas) goal; mechanics in [doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)).

| Role | Duty (paper) | Reads | Writes (capability) | Persona home |
|:--|:--|:--|:--|:--|
| **Planner** | plans; decomposes complex queries | board | `plan` (`plan_writer`) | `personas.py` (exists) |
| **Critic** | points out errors/hallucinations; forces rethink | board | `critique` (`critique_writer`) | `personas.py` (add) |
| **Conflict-Resolver** | detects contradictions; **names the conflicting agents** for a private debate | board | `conflict` (`conflict_mediator`) | `personas.py` (add) |
| **Cleaner** | detects useless/redundant messages and **removes** them | board | removals (`board_maintenance`) | `personas.py` (add) |
| **Decider** | judges whether the board suffices; posts the solution | board | `solution` (`decision_writer`) | `personas.py` (add) |
| **Experts** | domain reasoning for *this* query | board | `finding`, `rebuttal` (`finding_writer`) | generated per task (§2.1) |

> [!NOTE]
> The existing `auditor` persona bundles "Critic, Conflict-Resolver, Cleaner" into one description (`personas.py` lines 63–88). The work is to *split* that monolith into separately schedulable roles with enforced capabilities, so the CU can select them at different points of the cycle instead of all-at-once at the end. The existing `executor` becomes a legacy alias for `expert` ([04 §4 note](04-blackboard-protocol.md#capability-profiles-who-may-write-what)).

### 2.1 Expert generation (AG) and model-pool diversity

The paper's **agent-generating agent (AG)** runs once at genesis: a bare LiteLLM call that, given the query, returns `n` expert tuples `(E_i, D_i)` — identity + one-line ability description. In our system:

- AG lives in the daemon (it is control-plane work, not knowledge work — same rationale as the CU, [doc 12 §2.1](12-hermes-and-node-topology.md#21-should-the-control-unit-be-a-profile-mostly-no)). It evolves the existing expert-spawn call in `orchestrator._complex_research_flow` (L369–393).
- `n` is seeded by triage tier: SIMPLE→0 (constants only), LIGHT→1, MEDIUM→2, COMPLEX→3 (one per node — true parallel discovery). Configurable under `coordination.traditional.experts_per_tier`.
- Expert identities are injected into the shared `expert` Hermes profile via per-turn instructions ([doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)); their `author` string is `expert.<slug>` (opaque to every lower layer — seam rule 3).
- **Model diversity, the paper's way, bounded by triage:** the paper assigns each agent a base LLM *randomly from a pool* and shows mixed pools beat any single model. We reconcile that with cost-routing: triage selects the **tier**, and each agent draws uniformly from `models.pools.<tier>` (a new `bmas.yaml` list per tier, defaulting to the current single `routing.<tier>` model). The chosen model is recorded per turn (`turns.model`, [07 §1.4](07-data-model.md#14-turns--one-row-per-ks-activation)) and **actually passed** to the run request — fixing the dead triage path (§8).

## 3. Consensus & termination

The paper has **two answer paths**, and we implement both:

1. **Decider path (primary):** when the CU selects the decider and the decider judges the board sufficient, it posts a `solution` entry. The CU's next guard pass sees an accepted solution → terminal (`reason: solution`). A solution is "accepted" if it survives one round-trip: no new `critique` referencing it was posted in the same round it appeared (cheap deterministic check). If it *was* critiqued, the cycle continues — the solution stays on the board as a contested entry.
2. **Solution extraction / majority-similarity vote (fallback, `SolE`):** when `max_rounds` elapses (or the task stalls/budgets out) without an accepted solution, every agent is asked — *one bare LiteLLM call each, not Hermes runs* — to answer from the final board. The answer with the highest cumulative similarity to the others wins: `V(aᵢ)=Σⱼ sim(aᵢ,aⱼ)`, `argmax V`. Implement `sim` in tiers: exact-match after normalization for short/numeric/multiple-choice answers (the paper's benchmark shapes); embedding cosine (LiteLLM `/embeddings`) for free text; a cheap LLM judge only if embeddings are unavailable. Record which path produced the answer (`tasks.terminated_by`, `tasks.answer_source`).

**The convergence signal for the UI** (the meter in [08 §6](08-ui-blackboard-visualization.md#6-convergence-meter--rejection-overlay)) is defined honestly — it is a *progress heuristic*, not the termination test: `consensus_signal = ratio of findings with no open critique, weighted by salience`, emitted as a `consensus` SSE event each round alongside `{open_critiques, decider_state, phase}`. Termination itself follows paths 1–2 above, exactly as in the paper.

Config in `bmas.yaml` — nested under `coordination.*`, the **single** config shape shared with the variant docs and validated fail-fast in Phase 0 ([doc 10](10-migration-and-rollout.md#2-phases)). The complete traditional key set:

```yaml
coordination:
  variant: traditional              # traditional | patchboard | stigmergic | legacy_pipeline
  view_budget_tokens: 12000         # full-board default; budgeted mode above this (03 §4)
  round_execution: concurrent       # concurrent (distributed default) | sequential (paper-exact)
  traditional:
    max_rounds: 4                   # paper's recommended default (their avg rounds: 2.9–3.3)
    max_duration_s: 1800
    budget_ceiling_usd: 0.50        # per-task hard cap (§5 — sanity-check vs the context floor)
    max_concurrent_activations: 3   # default = node count
    experts_per_tier: { simple: 0, light: 1, medium: 2, complex: 3 }
    cleaner_entry_threshold: 12     # fallback-policy hint; the CU LLM usually handles this
    stall_rounds: 2                 # §5 — rounds with no accepted entries → halt
    cu_mode: llm                    # llm (paper) | heuristic_first (cost-saver, fallback table §1.1)
    coordinator_narration: false    # §1.2
    sole_similarity: auto           # auto | exact | embedding | judge  (§3 path 2)
```

## 4. Private sub-boards (conflict resolution)

Direct implementation of the paper's private spaces, scoped to their stated use: the Conflict-Resolver detects contradictions and **names the agents** involved; those agents debate privately; each then writes a new message to the public board.

```
Conflict-Resolver posts conflict entry naming e-12 (expert.valuation) ⨯ e-13 (expert.supply)
   │
   ├─ CU opens bmas:board:{task}:private:conflict-12-13 (04 §2)
   │     the named authors are activated INTO the private board for ≤2 private rounds
   │     (same turn machinery; entries land in the private space; traces still captured)
   │
   └─ on resolution → each involved agent posts its reconciled message to the PUBLIC board
         (the paper's contract); conflicting originals get status: superseded;
         private space archived to SQLite, wiped from Redis.
```

The UI shows a collapsed conflict marker on the public graph that expands into the private debate ([08 §5](08-ui-blackboard-visualization.md#5-private-sub-boards-and-conflicts)). Private rounds count against the same budget/round rails.

## 5. Cost governance & safety rails

The CU wrapper is the cost governor. Without this, a cyclic LLM system is a financial hazard. Non-negotiable, all deterministic, all shipped **in the same PR as the loop**:

- **Budget ceiling** (`budget_ceiling_usd`): the CU tracks `budget_spent` from the trace cost events ([06](06-agent-traces.md)) and terminates when exceeded. Surfaced live in the task header (the cost ticker already exists in `TopBar.tsx`).
- **Round cap** (`max_rounds`) and **duration cap** (`max_duration_s`): hard stops independent of the decider.
- **Stall breaker** (`stall_rounds`): rounds that produce **no accepted entries** (all declines/rejections), or whose accepted entries are near-duplicates of existing ones (normalized-body hash already on the board), increment a stall counter; at `stall_rounds`, force one decider activation, then halt with `terminated_by: stalled`. Pure log arithmetic, no LLM. (The PatchBoard variant ships a stricter state-hash circuit policy suited to its in-place-mutation model — [doc 11 §5.2](11-variant-patchboard.md#52-the-circuit-policy); the core's append-only model needs only this simpler breaker.)
- **Decline is *not* free on Hermes — gate in the daemon.** A live run measured a **~16k-token input floor per Hermes run** (system prompt + skills + memory load on every run — [06 §3.1 note](06-agent-traces.md#31-updated-taskresponse-schema)). Dispatching an agent just so it can return `{"action": "decline"}` costs real money regardless of model tier. "Should this role even act?" is the **CU's job, daemon-side** — that is precisely what the paper's control unit is for. `decline` remains a valid agent response (an activated agent may legitimately find nothing to add), but it is the fallback, not the gating mechanism.
- **Do the budget arithmetic before trusting the defaults.** At the ~16k-token context floor, a cloud model at ~$1.25/M input tokens costs ≈$0.02 per activation *before output tokens*; 4 rounds × 3 concurrent agents ≈ $0.24 of pure context. The default `budget_ceiling_usd: 0.50` is workable only if (a) the Cleaner actually runs (board stays debate-sized), (b) bodies stay under `max_entry_chars` with bulk content in artifacts ([17](17-files-and-artifacts.md)), and (c) repeat activations of the same agent use the Responses API (`previous_response_id`, [doc 12 §5.2](12-hermes-and-node-topology.md#52-stateful-turns-via-the-responses-api)) instead of re-stuffing context. Treat all three as **cost rails**, not optimizations; revisit the ceiling per model mix.
- **Concurrency cap** (`max_concurrent_activations`): bounds burst spend per round (default = node count).
- **Abort still works**: the existing `_check_abort` / `bmas:public:abort:{task}` HITL path is preserved and checked between rounds; mid-turn abort uses the Runs API stop endpoint ([06 §7](06-agent-traces.md#7-daemon-side-changes)).

> [!WARNING]
> Ship the rails in the **same PR** as the loop, not after. Treat `budget_ceiling_usd` as a required config key (fail-fast in `config.py`, matching the existing validation style).

## 6. HITL during the loop

Be precise about the baseline: today only **abort** is wired end-to-end (`_check_abort` polls `bmas:public:abort:{task}`). Pause and hints are **UI-side stubs** — the dashboard writes `bmas:public:state.pause` and `bmas:public:hints:{task}`, and `blackboard.py` ships `set_pause`/`is_paused`/`push_hint`/`pop_hints`, but the orchestrator never calls any of them. The cyclic model is where pause/hints become *real* for the first time, and richer:

- **Hint = a `directive` entry.** Operator hints are written as `directive` board entries by the CU on the operator's behalf, so agents see them as first-class board state next round.
- **Pause** halts the loop between rounds (clean boundary), not mid-dispatch.
- **Steer** (new): the operator can boost/pin an entry's salience or retract an entry (status → `superseded`, with an event), directly shaping the next round's board.
- **Approvals** (Phase 5): Hermes run-approvals surface as inline Approve/Deny in the trace UI ([doc 12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)).

## 7. What runs where (control plane vs. nodes)

A recurring source of confusion — fixed here as a table. "LLM call" = bare LiteLLM (no Hermes context floor); "Hermes run" = full agent with tools/traces.

| Function | Where | Kind |
|:--|:--|:--|
| Triage classification | control plane | local vLLM classifier (existing) |
| AG (expert generation) | control plane | LLM call |
| CU selection (per round) | control plane | LLM call |
| Knowledge-source turns (all roles, experts) | **nodes** | **Hermes run** (tools, traces, files) |
| SolE answer collection | control plane | LLM call per agent identity |
| SolE similarity scoring | control plane | embeddings / deterministic |
| Gateway, rails, files, persistence | control plane | deterministic code |

## 8. Migration of the existing flows

| Today | Becomes |
|:--|:--|
| `_standard_flow` (plan→exec→audit) | the blackboard cycle; SIMPLE/LIGHT tasks typically converge in 1–2 rounds (cheap parity with today) |
| `_complex_research_flow` (3 experts in parallel + synth) | genesis AG (n=3) + cycle: experts in Discovery, critic in Debate, decider in Convergence — but *selected by the CU*, not hardcoded |
| Triage routing | **Made real, not preserved.** Today triage's model choice is recorded as task metadata (`model_used` in SQLite) but **never reaches the agent** — each node runs its fixed `LITELLM_MODEL` env. The new dispatch path passes the per-agent model (tier pool, §2.1) into the run request (the Runs API accepts `model`; [doc 06 §3](06-agent-traces.md#3-rearchitected-agent-server)), making triage routing effective for the first time. Triage also seeds `max_rounds` (SIMPLE→2, COMPLEX→4) and `experts_per_tier`. |

> [!NOTE]
> Keep `coordination.variant: legacy_pipeline` as the escape hatch during migration so the old `_standard_flow` can run side-by-side for A/B comparison and rollback ([10](10-migration-and-rollout.md)).

➡️ Continue to [06 — Agent Traces](06-agent-traces.md).
