[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Migration & Rollout](10-migration-and-rollout.md) | [➡️ Next: Hermes & Node Topology](12-hermes-and-node-topology.md)

# 11 — Variant: PatchBoard (Schema-Grounded State Mutation)

> [!ABSTRACT]
> The complete specification of the **PatchBoard coordination variant** — [Zhang, Shi & Wang (2026), arXiv:2605.29313](https://arxiv.org/abs/2605.29313) — as a selectable alternative to the traditional LbMAS core. Agents mutate a **typed JSON state tree** through validated **RFC 6902 JSON-Patch** operations instead of posting natural-language entries; an **Architect** dynamically generates the task schema, the workflow blueprint, and the **worker agents themselves**; a **deterministic kernel** is the only writer; control is **event-driven rules, not an LLM**. This document contains everything needed to implement it later against the V1 engine — plus the UI adapter that visualizes its dynamically generated knowledge sources. **Nothing here is V1 work** except honoring the seams it plugs into.

> [!IMPORTANT] Scope and lineage — read this first
> Earlier drafts of this proposal conflated PatchBoard with the core architecture. That conflation is resolved: the **core is the natural-language LbMAS** of [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701) (docs [03](03-target-architecture.md)–[05](05-control-unit.md)); PatchBoard is a **different coordination paradigm** offered behind the same per-task dropdown ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)). The name "PatchBoard" is used with attribution to the published paper, not as our coinage. The peer-review re-grade that demoted it from flagship to variant — and the determinism lessons the core *kept* from it — is [doc 02 §2.4](02-peer-review.md#24-patchboard--json-patch--deterministic-kernel-suggestion-4--defer-to-variant-absorb-its-determinism-lessons).

---

## 1. The paper, distilled

PatchBoard's claim: free-form natural-language coordination is the dominant *reliability* failure mode in LLM multi-agent systems — agents hallucinate state, drift from the task, and contaminate shared context. Its fix is to make shared state **structured, schema'd, and mutable only through validated patches**:

1. **Shared state is a typed JSON document** (the *state tree*) governed by a **task-specific JSON Schema** — generated per task, not fixed. (Their ablation: generated schemas outperform fixed general-purpose schemas.)
2. **Agents never write prose into shared state.** Each turn returns a list of **RFC 6902 operations** (`add`/`replace`/`remove`/`test`) against the tree.
3. **A deterministic kernel is the sole writer.** Every proposed patch is schema-validated, contract-checked (may this worker write this path?), precondition-checked (`test` ops), and committed **transactionally** — all ops in a proposal apply atomically or none do. Rejections are logged with machine-readable reasons and *fed back to the agent*, which retries against fresh state.
4. **Knowledge sources are dynamically generated.** An **Architect** agent reads the task and emits a *blueprint*: the schema, a set of **worker specifications** (persona, goal, readable paths, writable paths = its *write contract*), and **workflow rules** (which worker activates on which state condition). The agent roster is born from the task, not configured.
5. **Control is deterministic.** No LLM scheduler: the kernel evaluates the blueprint's workflow rules against the state tree after every commit (event-driven). A **circuit policy** detects livelock deterministically.
6. **Everything is auditable.** The transaction log *is* the coordination history: who proposed what, what was rejected and why, what committed, in what order.

**Headline results** (their evaluation): 84.6% task success on ALFWorld vs 30.8% for a LangGraph baseline; **zero committed-state contamination** under fault injection (malformed/adversarial agent outputs never corrupted shared state — they died at validation); the circuit policy halted 96% of injected no-op/oscillation livelocks; **bounded context views** were one of the two largest ablation effects, with the *smallest* tested view budget performing best.

### 1.1 Why it is a variant, not the core

The trade is explicit ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms) table):

| | Traditional core | PatchBoard |
|:--|:--|:--|
| Strength | Open-ended reasoning, debate, critique — the chat/coding-harness regime | Long-horizon **stateful** tasks; token frugality; hard reliability/audit guarantees |
| Medium | Natural-language entries — anything sayable is postable | Only what the schema models is expressible |
| Failure mode | Verbosity, drift (bounded by Cleaner + rails) | Schema can't express a needed insight; Architect's blueprint is wrong and everything downstream inherits it |
| Token profile | Full board per turn (Cleaner-bounded) | **Bounded views** — each worker sees only its readable paths; dramatically cheaper on long tasks |

Pick PatchBoard (via the dropdown) for: multi-step extraction/transformation pipelines, stateful tool orchestration, long-horizon plans with many trackable sub-states, anything where "did the system corrupt its own state?" matters more than rhetorical richness.

## 2. Where it plugs in: the `CoordinationVariant` seam

PatchBoard is implemented as `PatchBoardVariant` against the seam from [03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms). What it reuses from the engine, unchanged: node dispatch + Runs API transport and traces ([06](06-agent-traces.md)), the `board_events` log and persistence ([07](07-data-model.md)), files/artifacts ([17](17-files-and-artifacts.md)), SSE, triage, cost accounting and rails, and the UI shell.

```python
# daemon/src/core/variants/patchboard.py  (sketch)
class PatchBoardVariant(CoordinationVariant):
    name = "patchboard"

    async def genesis(self, task):
        bp = await self._run_architect(task)          # §3 — one Hermes run (it's knowledge work)
        self._validate_blueprint(bp)                  # §3.3 — deterministic checks before anything runs
        await self.kernel.init_state(task.id, bp.schema, bp.initial_state)
        self.workers, self.rules = bp.workers, bp.rules
        await self._emit_genesis_events(task, bp)     # blueprint_created, worker_generated×N

    def build_turn_payload(self, task, actor, board):
        w = self.workers[actor]
        return self._bounded_view(task, w)            # §4.2 — readable paths only, token-capped

    def parse_agent_response(self, task, actor, raw):
        return self._extract_patch_ops(raw)           # §4.1 — RFC 6902 list (this variant's contract)

    async def apply(self, task, mutations):
        return await self.kernel.propose(task.id, mutations)   # §5 — NOT the core gateway

    async def step(self, task, board):
        if (halt := self.circuit.check(board)):       # §5.2 — deterministic, before anything else
            return StepResult(terminal=True, reason=halt)
        fired = self._eval_rules(board.state)         # §6 — pure predicate evaluation, no LLM
        return StepResult(terminal=not fired, activations=self._to_activations(fired),
                          reason=None if fired else "workflow_complete")

    def is_terminal(self, board):
        return self._completion_rule(board.state)     # blueprint's completion predicate
```

The seam guarantees this is a drop-in: the task runner calls the same five methods it calls for `traditional`; `board_events` stores namespaced event types (`patch_committed`, `patch_rejected`, `blueprint_created`, `worker_generated`, `rule_fired`, `circuit_tripped`); actors are opaque strings (`architect`, `worker.extractor-2`); traces flow through the identical pipeline.

## 3. Genesis: the Architect and dynamic worker generation

This is the variant's signature move and the reason the UI must treat agent rosters as data ([seam rule 3](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)): **the knowledge sources do not exist until the task does.**

### 3.1 The Architect run

One Hermes run (profile `architect` — a real reasoning agent with web/code tools, replicated like the other profiles, [doc 12 §2.5](12-hermes-and-node-topology.md#25-the-agents-on-3-hosts-answer-yes-via-profiles)) receives the task (+ extracted attachment text, [17 §4](17-files-and-artifacts.md#4-attachments-on-the-board)) and must return a single JSON **blueprint**:

```jsonc
{
  "schema": { /* JSON Schema (draft 2020-12) for the task state tree */ },
  "initial_state": { "goal": "…", "plan": [], "findings": {}, "status": "init" },
  "workers": [
    {
      "id": "worker.extractor",          // opaque actor id — UI renders via fallback colors
      "persona": "You extract financial figures from the attached 10-Q…",
      "model_tier": "light",              // triage tier pool, same mechanism as AG (05 §2.1)
      "reads":  ["/goal", "/findings/raw", "/attachments"],
      "writes": ["/findings/figures/*"],  // the WRITE CONTRACT — kernel-enforced (§5)
      "tools": ["web", "code_exec"]
    },
    { "id": "worker.verifier", "reads": ["/findings/figures"], "writes": ["/findings/verified/*", "/flags/*"], "…": "…" }
  ],
  "rules": [
    { "when": "/findings/raw is non-empty AND /findings/figures is empty", "activate": "worker.extractor" },
    { "when": "exists /findings/figures/* not in /findings/verified",      "activate": "worker.verifier" },
    { "completion": "/status == 'done' AND /flags is empty" }
  ]
}
```

### 3.2 How workers become Hermes runs

Workers reuse the expert mechanism exactly ([05 §2.1](05-control-unit.md#21-expert-generation-ag-and-model-pool-diversity)): a single neutral **`worker` Hermes profile** (full toolset) with the generated persona + write contract injected via per-turn `AGENTS.md`/`instructions`, dispatched one-per-node for parallelism, model drawn from the blueprint's `model_tier` against the triage pool. No per-worker profiles are created on nodes — worker identity is data, not infrastructure.

### 3.3 Blueprint validation (deterministic, before any worker runs)

The Architect is an LLM; its blueprint is *proposed*, not trusted. The daemon validates: the schema compiles; `initial_state` validates against it; every worker's `reads`/`writes` resolve to schema paths; write contracts don't overlap on the same path unless flagged `concurrent: true`; rules reference real workers and real paths; a `completion` rule exists; worker count ≤ `patchboard.max_workers`. Failures → one Architect retry with the validation errors, then task failure with a legible reason. (`blueprint_rejected` event either way.)

## 4. The agent I/O contract

### 4.1 Turn output: RFC 6902 + rationale

A worker turn returns (same `TaskResponse` envelope as the core — [06 §3.1](06-agent-traces.md#31-updated-taskresponse-schema) — with `patch_ops` in place of `entries`):

```jsonc
{
  "action": "patch",
  "patch_ops": [
    { "op": "test",    "path": "/findings/figures/revenue", "value": null },          // §4.3 precondition
    { "op": "add",     "path": "/findings/figures/revenue", "value": { "q3_2026": "35.1B", "source": "10-Q p.4" } },
    { "op": "replace", "path": "/status", "value": "extracting" }
  ],
  "rationale": "Extracted revenue from the attached 10-Q; cited page 4."   // human-readable, for the UI/trace only
}
```

The `rationale` is **never** written to shared state (that's the whole point) — it is stored on the turn/trace and shown in the transaction log UI. Malformed patch JSON is *not* wrapped leniently the way the core wraps free text ([04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)) — in this paradigm a malformed mutation is a **rejection with feedback** (`patch_rejected` → reason returned to the worker on its retry turn). Reliability of patch emission is an explicit open question ([§8](#8-open-questions-gates-before-building)).

### 4.2 Turn input: bounded views

Workers do **not** receive the whole state tree. The payload contains only the worker's `reads` paths, serialized, plus its write contract and the schema fragments for its writable paths — capped by `patchboard.view_budget_tokens`. The paper's ablation makes this the most counterintuitive finding worth honoring: **smaller views performed better**. Default the budget low (4000) and treat raising it as a measured decision, not a reflex.

### 4.3 `test` ops: stale-view preconditions

Because views are partial and turns are concurrent, a worker's view can be stale at commit time. Workers are instructed to prefix mutations with `test` ops asserting the values they based their reasoning on. A failed `test` rejects the whole transactional proposal (`reason: precondition_failed`) — the worker re-reads and retries. This is the paradigm's optimistic-concurrency primitive, surfaced to the agent itself.

## 5. The deterministic kernel

`daemon/src/core/patch_kernel.py` — the variant's counterpart to the core's Board Gateway ([04 §4](04-blackboard-protocol.md#4-the-board-gateway)), and deliberately **not** the same component: the gateway validates *envelopes around prose*; the kernel validates *mutations against schema and contracts*. They share the event-log writer and emit through the same SSE path.

Validation pipeline, per proposal (all-or-nothing):

1. **Parse** — ops are well-formed RFC 6902.
2. **Authorize** — every op's `path` matches the worker's `writes` globs (capability-based: the contract travels with the actor, no role names — seam rule 4).
3. **Preconditions** — `test` ops evaluate against current state.
4. **Dry-run apply** — the patched document validates against the task schema.
5. **Commit** — apply atomically under the per-task writer lock; append one `patch_committed` event (ops + actor + turn + resulting **state hash**); recompute derived fields (the variant registers its state-hash + per-path activity counters in the same `recompute_derived` hook the core uses for salience — seam rule 5).

Any step failing → `patch_rejected` event `{ops, actor, reason, detail}`, no state change, reason fed back to the worker. Rejections are *data*: their rate per worker is a blueprint-quality signal surfaced in the UI.

**Concurrency.** Proposals from concurrent turns serialize at the kernel (same single-writer-per-task discipline as the core gateway). Cross-turn races are handled by `test` preconditions rather than per-path revision counters in V-first implementation; if measured contention warrants it, add per-path revisions as an optimization. This is the machinery the core dropped as unnecessary for append-only entries ([04 §6](04-blackboard-protocol.md#6-concurrency-append-only-makes-it-easy)) — here it earns its keep.

### 5.1 Event-sourcing and replay

The state tree is never stored as mutable truth: `board_events` rows (`event_type: patch_committed`, payload = ops) are the source; the current tree is a fold, snapshot-cached in Redis (`bmas:board:{task}:state`) and SQLite. Replay, fork-from-event, and the UI scrubber ([08 §7](08-ui-blackboard-visualization.md#7-replay--scrubber)) work identically to the core because the log shape is shared — only the fold function differs.

### 5.2 The circuit policy

Deterministic livelock detection, checked at the top of every `step()` — the paper's version is stricter than the core's stall breaker because in-place mutation can oscillate invisibly:

- **No-op detection**: a committed proposal whose post-state hash equals its pre-state hash increments the actor's no-op counter.
- **Oscillation detection**: the rolling window of state hashes contains a cycle (A→B→A→B) → trip.
- **Rejection streaks**: `n` consecutive rejections from one worker → suspend that worker (`worker_suspended` event), let the rules route around it; if no rule can fire → trip.
- **Trip behavior**: halt with `terminated_by: circuit_policy`, emit `circuit_tripped` with the evidence window. (Paper: 96% of injected livelocks caught.)

Config: `patchboard.circuit: { hash_window: 8, max_noops_per_worker: 2, max_rejection_streak: 3 }`.

## 6. Control: event-driven rules, no LLM

After every commit (and at genesis), the variant evaluates the blueprint's `rules` against the state tree — pure predicate evaluation (JSONPath-style conditions compiled at blueprint validation). Fired rules become activations (capped by `max_concurrent_activations`, node-assigned round-robin like the core). No rule fires + completion predicate false + circuit not tripped → the task is **wedged**: emit `workflow_wedged`, activate the Architect once for a *blueprint amendment* (add a rule/worker — re-validated per §3.3), then halt if still wedged. Cost rails (budget ceiling, duration cap) are the engine's and apply unchanged ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)); `max_rounds` maps to a `max_commits` cap here.

## 7. UI extensibility

This is what the panel-registry seam ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)) was built for. The `patchboard` `VariantUIAdapter` registers:

- **Graph**: the center surface renders the **state tree** (collapsible JSON tree with per-path freshness/activity coloring) instead of an entry graph; the overlay slot gets a **region-activity glow** (paths hot with recent commits/rejections — reuses the heat ramp tokens, [13 §7](13-ui-showcase-density.md#7-component--token-additions)).
- **Mission panels** ([13 §3.1](13-ui-showcase-density.md#31-variants-in-the-cockpit)):
  - **Blueprint inspector** — the generated schema, worker cards (persona, contract paths, model), and rules; **worker spawn/retire animations** at genesis make the "dynamically generated knowledge sources" visibly dramatic — the roster materializes as the Architect's blueprint commits.
  - **Transaction log** — virtualized live tail of `patch_committed`/`patch_rejected` with op diffs, rationale, and per-worker rejection-rate chips. This is the variant's firehose-equivalent and its best showcase surface ("watch validated mutations land; watch a hallucinated one bounce off the kernel").
  - **Convergence strip** plugs blueprint completion % (satisfied rule predicates / total) into the shared sparkline slot.
- **Agent Minds**: the roster is `architect` + generated workers — pure data, rendered with fallback colors; a worker card shows its write contract on hover.
- **Composer extras**: optional "review blueprint before execution" toggle — a HITL gate where genesis pauses for operator approval of the Architect's plan (reuses the approval surface, [12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)).
- **Event handlers**: the namespaced SSE events (`blueprint_created`, `worker_generated`, `patch_committed`, `patch_rejected`, `rule_fired`, `worker_suspended`, `circuit_tripped`, `workflow_wedged`) — ignored by the shell, consumed here.

Acceptance: the adapter mounts with **zero edits** outside `variants.ts` + adapter files (the dummy-adapter test from [10 Phase 4](10-migration-and-rollout.md#phase-4--blackboard-visualization) proves the seam before this variant exists).

## 8. Open questions (gates before building)

| # | Question | Why it gates |
|:--|:--|:--|
| P1 | Can the deployed Hermes models reliably emit valid RFC 6902 against a live schema? | The core dodged this (envelope + free-text fallback); PatchBoard cannot. Prompt-test against the live Runs API; measure rejection rates per model tier before committing to the build. |
| P2 | Architect blueprint quality at our model tiers | The paper's results assume a competent Architect. A bad blueprint poisons everything downstream — test on representative tasks; consider pinning the Architect to the highest tier regardless of triage. |
| P3 | Schema-generation cost/latency at genesis | One heavyweight run before any work starts; acceptable for long-horizon tasks, hostile for chat-class ones — reinforces the per-task dropdown rather than a global default. |
| P4 | JSONPath rule-engine scope | Keep the predicate language deliberately tiny (exists/eq/non-empty/count); resist a DSL. |
| P5 | Interplay with files/artifacts | Attachments surface as read-only state paths (`/attachments`); artifact sync is unchanged ([17 §6](17-files-and-artifacts.md#6-artifacts-agent-created-files)). Verify the staging contract needs no variant-specific change. |

## 9. Config sketch

```yaml
coordination:
  variant: patchboard               # selected per task via the dropdown; this is just the default
  patchboard:                       # parsed but inert until selected (validated fail-fast in Phase 0)
    max_workers: 6
    max_commits: 60                 # the round-cap analogue
    view_budget_tokens: 4000        # bounded views — small on purpose (§4.2)
    architect_model_tier: complex   # P2 — pin high
    blueprint_approval: false       # HITL gate at genesis (§7)
    circuit: { hash_window: 8, max_noops_per_worker: 2, max_rejection_streak: 3 }
```

➡️ Continue to [12 — Hermes & Node Topology](12-hermes-and-node-topology.md). The other variant — true stigmergy — is [doc 16](16-variant-stigmergic.md).
