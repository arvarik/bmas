[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Blackboard Protocol](04-blackboard-protocol.md) | [➡️ Next: Agent Traces](06-agent-traces.md)

# 05 — Control Unit, Roles & Cyclic Execution

> [!ABSTRACT]
> The Control Unit is the referee that replaces the puppeteer. This document specifies the OODA scheduler, the paper role group (5 constant roles + generated experts), consensus/termination, private-board conflict resolution, and — critically for a homelab — the cost-governance rails that keep stigmergy from becoming a billing incident.

---

> [!IMPORTANT] The Control Unit is one pluggable strategy, not the engine
> Everything in this document is the **V1 `ControlUnitStrategy`** — one implementation of the `CoordinationStrategy` seam ([doc 11 §2](11-extensibility-and-variants.md#2-the-seam-coordinationstrategy)). The kernel, board, traces, and UI know nothing about "Control Unit" or role names. A future `StigmergicStrategy` (no CU, no roles) swaps in without touching any lower layer. As you read, treat roles and the scheduler as living **only** in this layer.

## 1. The Control Unit is a referee, not a brain

Today, `Orchestrator._standard_flow` *is* the solution logic ([Gap G1](01-gap-analysis.md#2-evidence-the-control-component-encodes-the-solution)). The Control Unit (CU) removes that logic and keeps only scheduling and termination. It runs the OODA loop from [03 §2](03-target-architecture.md#2-the-execution-loop-ooda-replaces-the-dag).

```python
# daemon/src/core/control_unit.py  (sketch)
class ControlUnit:
    async def run(self, task_id: str, objective: str):
        await self.bb.genesis(task_id, objective, self.threshold, self.weights)
        round_no = 0
        while True:
            round_no += 1
            board = await self.bb.snapshot(task_id)             # OBSERVE
            state = self._orient(board)                         # ORIENT (cheap, deterministic first)
            decision = await self._decide(board, state, round_no)  # DECIDE (LLM-assisted only if needed)
            if decision.terminal:
                break
            await self._act(task_id, decision.activations, board)  # ACT (concurrent dispatch)
            await self._update_meta(task_id, round_no, decision)
        await self._finalize(task_id)
```

### Two-tier DECIDE (cost discipline)

Do **not** spend an LLM call every turn just to pick the next agent. Use a deterministic policy for the common cases and only escalate to an LLM "scheduler" when the board is genuinely ambiguous:

| Board condition (deterministic) | Action |
|:--|:--|
| Phase = Discovery, no findings | Activate Planner + all Experts (parallel) |
| Open `finding`s with no `critique` | Activate Critic(s) |
| Open `critique`s with no `rebuttal` | Activate the critiqued authors (parallel) |
| Open `conflict`s | Open private sub-board, activate Conflict-Resolver |
| Board noisy (entries > N, many low-salience) | Activate Cleaner |
| Consensus score ≥ threshold | Terminate (Decider confirms) |
| None of the above clearly applies | **Escalate**: LLM scheduler reads board → returns role set |

This keeps most turns LLM-free at the CU level (the *agents* still use LLMs to do the work), which is both cheaper and more debuggable.

> [!NOTE] These heuristics are really "read the pressure field"
> Each row above is a special case of *orienting toward the highest-pressure region* ([doc 04 §6.1](04-blackboard-protocol.md#61-salience-vs-pressure-the-v2-seam)). Implement the table against the `pressure` ZSet (open conflict = high pressure → Conflict-Resolver; etc.) rather than ad-hoc `if` checks. This makes the V1 scheduler and the V2 roleless self-activation read the *same signal*, so the variant transition is continuous, not a rewrite.

### 1.1 The Coordinator narration agent (optional showcase flourish)

The deterministic scheduler is fast but **invisible** — for a showcase artifact, the operator can't *see* the system deciding what to think about. This optional add-on makes the OODA loop legible without compromising it.

**What it is:** a thin `coordinator` Hermes profile that the **escalation path** (the last row of the DECIDE table) routes through instead of a bare LiteLLM call. When the board is genuinely ambiguous and the CU must ask a model "which region is hottest / who should act next," that call runs as a *named agent* whose output is a short rationale + the chosen role set. Its reasoning is captured as a [trace](06-agent-traces.md) like any other agent and rendered as a dedicated **Coordinator lane** in the UI ([doc 13](13-ui-showcase-density.md)) — so the audience watches the system *think about what to think about*.

**Hard constraints (so the flourish never becomes a liability):**

1. **Off the critical path.** A coordinator stall or error must never block the loop. If it doesn't return within a tight deadline, the CU falls back to the deterministic policy (treat as "no clear action → pick highest-pressure region"). It is *narration over* a decision the deterministic layer can always make alone.
2. **It does not run every turn.** Only on the escalation row. The common deterministic rows ([table above](#two-tier-decide-cost-discipline)) stay LLM-free. This keeps the cost story intact.
3. **It is not the scheduler.** The `ControlUnitStrategy` ([doc 11 §2](11-extensibility-and-variants.md#2-the-seam-coordinationstrategy)) still owns control; the coordinator is an *advisory, observable* call it may make. The scheduler stays deterministic and unit-testable in the daemon.
4. **Pluggable + flag-gated.** A `coordination.control_unit.coordinator_narration: false` config flag toggles it. With it off, the escalation path is a plain LiteLLM call and the system behaves identically minus the lane.

This is the cleanest way to satisfy the showcase goal — see the [profile note in doc 12 §2.1](12-hermes-and-node-topology.md#21-should-the-control-unit-be-a-profile-mostly-no) (it adds an 8th, optional `coordinator` profile) and the [Coordinator lane in doc 13](13-ui-showcase-density.md).

## 2. The paper role group

Extends today's three static roles. The paper has **5 constant roles** (Planner, Decider, Critic, Conflict-Resolver, Cleaner) plus **query-generated Experts**; "six-role" was an earlier shorthand for "the constant role set plus experts." Roles are **logical** — they are personas + a capability profile ([04 §4](04-blackboard-protocol.md#capability-matrix-who-may-write-what)), decoupled from physical nodes. Any node can assume any role per turn (the paper's model; the [roadmap](../roadmap/control-unit.md#dynamic-role-assignment-6-role-bmas) goal).

| Role | Replaces / adds | Reads | Writes | Persona home |
|:--|:--|:--|:--|:--|
| **Planner** | existing planner | objective | `plan` | `personas.py` (exists) |
| **Critic** | *new* | findings | `critique` | `personas.py` (add) |
| **Conflict-Resolver** | *new* | conflicting entries | `conflict`, promotes resolution | `personas.py` (add) |
| **Cleaner** | *new* | low-salience entries | `status` mutations only | `personas.py` (add) |
| **Decider** | folded into auditor today | whole board | `consensus`, scores | `personas.py` (add) |
| **Experts** | existing complex-flow experts | objective + findings | `finding`, `rebuttal` | `generate_expert_persona` (exists) |

> [!NOTE]
> The existing `auditor` persona already bundles "Critic, Conflict-Resolver, Cleaner" in its description (`personas.py` lines 63–88). The work here is to *split* that monolith into separately schedulable roles with enforced authorization, so they can act at different points in the loop instead of all-at-once at the end.

### Physical assignment stays simple

`bmas.yaml` keeps mapping nodes to a *default* role for capacity planning, but the CU may assign any logical role to any healthy node per turn. Add an optional `roles: [...]` capability list per node, and a `participation_mode` (`push` default; `pull` future) per [agent-integration roadmap](../roadmap/agent-integration.md).

## 3. Consensus & termination

The Decider produces a **streamed consensus score** so the operator can *watch convergence* — a product surface the external review missed.

```python
async def _decide(self, board, state, round_no) -> Decision:
    score = self._consensus_score(board)          # Gate A: salience-weighted, no-open-critique ratio
    answer_agreed = self._answer_agreement(board) # Gate B: a Decider answer entry, uncritiqued
    await self.bb.set_meta(board.task_id, consensus_score=score, round=round_no)
    await self.bb.emit(board.task_id, "consensus",
                       {"score": score, "threshold": self.threshold,
                        "answer_agreed": answer_agreed, "phase": board.phase})
    if score >= self.threshold and answer_agreed:   # BOTH gates (see "Consensus score" below)
        return Decision(terminal=True, reason="consensus")
    if round_no >= self.max_rounds:
        return Decision(terminal=True, reason="max_rounds")
    if board.budget_spent >= self.budget_ceiling:
        return Decision(terminal=True, reason="budget")
    return Decision(terminal=False, activations=self._select_roles(state))
```

**Consensus score** — start simple and deterministic, upgrade later:

1. **v1 (ship first):** a **two-gate** check, because "no unresolved objections" is *not* the same as "the agents agree on an answer":
   - **Gate A — stability:** ratio of `finding`s that are `accepted` and have no open `critique`, weighted by salience. Cheap, explainable, no extra LLM call. This measures *absence of unresolved objection*.
   - **Gate B — answer agreement:** there exists a single `consensus`/answer entry (posted by the Decider) that the round's active authors do **not** critique. This measures *actual agreement on the answer*, which is what the paper's consensus is about.
   - Terminate on consensus only when **both** gates pass. Gate A alone can be high while the accepted findings still point at different conclusions — terminating there would declare "consensus" with no agreement, exactly the failure the convergence demo would expose.
2. **v2 (optional):** replace Gate B with embedding-similarity across agents' current position entries (cumulative similarity `V(aᵢ)=Σ sim(aᵢ,aⱼ)`, per the paper). Add behind a flag.

> [!NOTE] Why this matters
> The paper's consensus is **answer convergence** (the decider's judgment, or a similarity vote), not "the board has no open critiques." A salience-weighted finding ratio is a good *progress signal* but a poor *termination signal* on its own. Keeping both gates makes the v1 metric a faithful (if cheaper) version of the paper's notion rather than a different one.

Config in `bmas.yaml` — nested under `coordination.*`, the **single** config shape shared with [doc 11 §7](11-extensibility-and-variants.md#7-config-sketch-both-variants-visible) and validated fail-fast in Phase 0 ([doc 10](10-migration-and-rollout.md#2-phases)). This is the complete V1 key set:

```yaml
coordination:
  strategy: control_unit            # control_unit | stigmergic | legacy_pipeline
  view_budget_tokens: 4000          # per-turn bounded board view (doc 03 §4)
  control_unit:
    consensus_threshold: 0.8        # 0..1
    max_rounds: 4                   # paper's recommended default
    max_duration_s: 1800            # long-horizon cap
    budget_ceiling_usd: 0.50        # per-task hard cap (§5 — sanity-check vs the per-turn context floor)
    consensus_mode: ratio           # ratio | similarity
    max_concurrent_activations: 3   # concurrency cap (§5); default = node count
    stall_rounds: 2                 # livelock circuit-breaker (§5)
    coordinator_narration: false    # §1.1 — optional showcase flourish
```

## 4. Private sub-boards (conflict resolution)

Adopts the external review's hierarchical sub-boards, scoped to its highest-value use: resolving contradictions without polluting the public board (the [blackboard roadmap](../roadmap/blackboard.md) item).

```
Conflict-Resolver detects e-12 ⨯ e-13 contradiction
   │
   ├─ kernel.open_private(task, topic="conflict-12-13")  → bmas:board:{task}:private:conflict-12-13
   │     authors of e-12 and e-13 are activated INTO the private board
   │     they exchange rebuttals there (not on the public board)
   │
   └─ on resolution → kernel.promote(): a single reconciled `finding` lands public;
         e-12/e-13 marked `superseded`; private board wiped.
```

The UI shows a collapsed "conflict" marker on the public graph that expands into the private debate ([08 §5](08-ui-blackboard-visualization.md#5-private-sub-boards-and-conflicts)).

## 5. Cost governance & safety rails

The CU is the cost governor. Without this, the inversion is a financial hazard. Non-negotiable rails:

- **Budget ceiling** (`budget_ceiling_usd`): the CU tracks `budget_spent` from the trace cost events ([06](06-agent-traces.md)) and terminates when exceeded. Surfaced live in the task header (the cost ticker already exists in `TopBar.tsx`).
- **Round cap** (`max_rounds`) and **duration cap** (`max_duration_s`): hard stops independent of consensus.
- **Livelock circuit-breaker** (`stall_rounds`): the caps above bound *spend*, not *progress* — without this rail, agents can commit valid patches that oscillate or no-op for the full `max_rounds`. Detect stalls deterministically from the kernel's rolling **board state hash** ([04 §4](04-blackboard-protocol.md#the-board-state-hash-livelock-support)): if the hash is unchanged for `stall_rounds` consecutive rounds, or a short hash cycle repeats (`A→B→A`), or the same actor's proposals are rejected `stall_rounds` consecutive times — halt with `terminated_by: stalled` (or force one Decider escalation first, then halt). Pure hash/log arithmetic, no LLM call. [PatchBoard (2026)](https://arxiv.org/abs/2605.29313) showed this class of deterministic circuit policy halts **96%** of injected no-op/oscillation cycles, vs 12% for a plain blackboard.
- **Decline is *not* free on Hermes — gate in the daemon.** A live run measured a **~16k-token input floor per Hermes run** (system prompt + skills + memory are loaded on every run — [06 §3.1 note](06-agent-traces.md#31-updated-taskresponse-schema)). Dispatching an agent just so it can return `{"action": "decline"}` therefore costs real money regardless of model tier. "Should this role even act?" gating must happen **daemon-side**: first deterministically (the pressure-field rows in the DECIDE table), and only if still ambiguous via a cheap *bare LiteLLM* call — never by dispatching a Hermes run. `decline` remains a valid agent response (an activated agent may legitimately find nothing to add), but it is the fallback, not the gating mechanism.
- **Do the budget arithmetic before trusting the defaults.** At the ~16k-token context floor, a cloud model at ~$1.25/M input tokens costs ≈$0.02 per activation *before any output tokens*; 4 rounds × 3 concurrent agents ≈ $0.24 of pure context. The default `budget_ceiling_usd: 0.50` is workable only if (a) agents receive the board **index**, not the full board ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)), and (b) repeat activations of the same agent use the Responses API (`previous_response_id`, [doc 12 §5.2](12-hermes-and-node-topology.md#52-stateful-turns-via-the-responses-api)) instead of re-stuffing context. Treat both as **cost rails**, not optimizations; revisit the ceiling per model mix.
- **Concurrency cap** (`max_concurrent_activations`): max KS activated per turn (default = node count) to bound burst spend.
- **Abort still works**: the existing `_check_abort` / `bmas:public:abort:{task}` HITL path is preserved and checked each loop iteration.

> [!WARNING]
> Ship the rails in the **same PR** as the loop, not after. A cyclic LLM system without a budget ceiling can run up a large bill from a single malformed objective. Treat `budget_ceiling_usd` as a required config key (fail-fast in `config.py`, matching the existing validation style).

## 6. HITL during the loop

Be precise about the baseline: today only **abort** is wired end-to-end (`_check_abort` polls `bmas:public:abort:{task}`). Pause and hints are **UI-side stubs** — the dashboard writes `bmas:public:state.pause` and `bmas:public:hints:{task}`, and `blackboard.py` ships `set_pause`/`is_paused`/`push_hint`/`pop_hints`, but the orchestrator never calls any of them; the flags are written and ignored. The cyclic model is where pause/hints become *real* for the first time, and richer:

- **Hint = a `directive` entry.** Operator hints are written as `directive` board entries by the CU on the operator's behalf, so agents see them as first-class board state next turn.
- **Pause** halts the loop between turns (clean boundary), not mid-dispatch.
- **Steer** (new): operator can pin/boost an entry's salience or retract a hallucinated entry, directly shaping the next OODA cycle.

## 7. Optional future: pure pull with Hermes crons

Documented for completeness; **not the baseline** ([Peer Review §2.1](02-peer-review.md#21-push--pull-suggestion-1--adapt-dont-take-literally)). Hermes supports crons, but live `features.jobs_admin=false`; V2 should provision cron jobs through CLI/`config.yaml` unless job-admin HTTP writes are explicitly verified (see [HERMES_API.md](../HERMES_API.md#appendix-a-gateway-api-server-port-8642)). A future variant could:

- run a long-lived Hermes process per node with a cron that polls `bmas:board:{task}:meta`/index;
- let each agent self-activate when its role's trigger condition appears on the board;
- demote the CU to pure referee (consensus + caps only, no activation).

This is genuinely closer to "pure stigmergy" but adds operational weight (persistent processes, deduplication of concurrent self-activations, harder cost control). Revisit only after the referee model is stable and observable.

## 8. Migration of the existing flows

| Today | Becomes |
|:--|:--|
| `_standard_flow` (plan→exec→audit) | CU loop with deterministic role selection; SIMPLE/LIGHT tasks may converge in 1 round (cheap parity with today) |
| `_complex_research_flow` (3 experts in parallel + synth) | CU loop seeded with Experts in Discovery, Critic in Debate, Decider in Convergence |
| Triage routing | **Made real, not preserved.** Today triage's model choice is recorded as task metadata (`model_used` in SQLite) but **never reaches the agent** — each node runs its fixed `LITELLM_MODEL` env. The new dispatch path must actually pass the triage-selected model into the run request (the Runs API accepts `model`; [doc 06 §3](06-agent-traces.md#3-rearchitected-agent-server)), making triage routing effective for the first time. Triage can also seed `max_rounds` (SIMPLE→1, COMPLEX→4). |

> [!NOTE]
> Keep a `legacy_pipeline: true` escape hatch in config during migration so the old `_standard_flow` can run side-by-side for A/B comparison and rollback ([10](10-migration-and-rollout.md)).

➡️ Continue to [06 — Agent Traces](06-agent-traces.md).
