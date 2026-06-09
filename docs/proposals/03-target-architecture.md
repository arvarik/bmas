[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Peer Review](02-peer-review.md) | [➡️ Next: Blackboard Protocol](04-blackboard-protocol.md)

# 03 — Target Architecture: A True Blackboard

> [!ABSTRACT]
> This document defines the destination. It introduces the three canonical blackboard components, the OODA execution loop that replaces the fixed DAG, and the push↔pull spectrum we deliberately sit in the middle of. Implementation detail for each component lives in [04](04-blackboard-protocol.md), [05](05-control-unit.md), and [06](06-agent-traces.md).

---

## 1. The three components

A classical blackboard system (Hayes-Roth, 1985; Han & Zhang, 2025) has exactly three parts. We map each onto a concrete bMAS owner.

| Component | Definition | bMAS owner (target) |
|:--|:--|:--|
| **Blackboard** | The shared, structured workspace. The single source of truth all agents read and write. | Redis (live, event-sourced) + SQLite (durable archive). A **deterministic kernel** in the daemon guards all writes. |
| **Knowledge Sources (KS)** | Independent specialists that observe the board and contribute when they can advance the solution. | Hermes agents on edge LXCs, invoked per-turn, reading the board and proposing **patches**. |
| **Control Unit (CU)** | Schedules KS activations opportunistically based on board state; decides when the solution is complete. | A thin LLM-assisted scheduler + a deterministic Decider in the daemon. The **referee**, not the puppeteer. |

The single most important change versus today: **the daemon stops being a Knowledge Source.** Right now it *is* the planner/executor/auditor coordinator that holds the solution logic. In the target, the daemon owns only the Blackboard kernel and the Control Unit; the *reasoning* lives entirely in the Knowledge Sources, which coordinate through the board.

```
                         ┌──────────────────────────────────────────┐
                         │            CONTROL UNIT (referee)          │
                         │  • observes board snapshot + salience      │
                         │  • selects which KS to activate this turn  │
                         │  • Decider scores consensus → terminate?   │
                         │  • enforces round/duration/budget caps     │
                         └───────────────┬────────────────────────────┘
                                         │ activates (push) / invites (pull)
            ┌────────────────────────────┼────────────────────────────┐
            ▼                            ▼                            ▼
   ┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
   │  KS: Planner    │         │  KS: Critic     │         │  KS: Expert(s)  │
   │  (Hermes node)  │         │  (Hermes node)  │         │  (Hermes node)  │
   └────────┬────────┘         └────────┬────────┘         └────────┬────────┘
            │ proposes JSON Patch        │ proposes patch            │ proposes patch
            ▼                            ▼                            ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                      DETERMINISTIC KERNEL (daemon)                          │
   │   validate(schema) → resolve concurrency (CAS) → commit → emit event        │
   └──────────────────────────────────┬───────────────────────────────────────┘
                                       ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │   BLACKBOARD   public entries · private sub-boards · patch log · salience    │
   │   Redis (live + Pub/Sub)  ⇄  SQLite (durable archive, replay)                │
   └──────────────────────────────────┬───────────────────────────────────────┘
                                       │ SSE (board patches, traces, consensus)
                                       ▼
                          Mission Control (live graph + trace inspector)
```

## 2. The execution loop: OODA replaces the DAG

The fixed `plan → execute → audit` sequence in `orchestrator.py` is replaced by a bounded cyclic loop. This is the [roadmap's stated intent](../roadmap/control-unit.md), now made concrete.

```
GENESIS
  CU writes objective + consensus_threshold + phase=Discovery to the board.

LOOP (until terminal):
  ┌── OBSERVE ──────────────────────────────────────────────┐
  │ CU reads board snapshot: entries, salience, open         │
  │ conflicts, unanswered critiques, round/budget counters.  │
  └──────────────────────────────────────────────────────────┘
  ┌── ORIENT ───────────────────────────────────────────────┐
  │ CU classifies board state: Are there findings to verify? │
  │ Open conflicts? Unaddressed critiques? Redundant noise?  │
  └──────────────────────────────────────────────────────────┘
  ┌── DECIDE ───────────────────────────────────────────────┐
  │ CU selects the KS set to activate this turn (may be >1,  │
  │ run concurrently). Decider scores consensus.             │
  │  • consensus ≥ threshold  → phase=Verified_Complete, EXIT│
  │  • round ≥ max_rounds      → EXIT (timeout)              │
  │  • budget exhausted        → EXIT (budget)              │
  └──────────────────────────────────────────────────────────┘
  ┌── ACT ──────────────────────────────────────────────────┐
  │ Selected KS run, read the live board, propose patches.   │
  │ Kernel validates + commits. Events stream to the UI.     │
  └──────────────────────────────────────────────────────────┘

FINALIZE
  Cleaner prunes; CU promotes the consensus entry to bmas:public:results.
```

Phases are explicit board state (`Discovery → Debate → Convergence → Verified_Complete`), as the roadmap specifies — and they drive the UI's phase indicator directly.

## 3. The push ↔ pull spectrum (and where we sit)

```
  PUPPETEER                    REFEREE (our target)                 SWARM
  (today)                                                         (pure pull)
  ───────────────────────────────────────────────────────────────────────────►
  Fixed pipeline.     CU selects relevant KS per turn       Agents poll Redis via
  Agents blind.       based on board state. Agents READ     Hermes crons, self-
  No board reads.     the live board and may decline.       activate, no central CU.
  No cycles.          Cyclic, bounded, cost-governed.       Hard to govern/observe.
```

We target the **referee** position because it captures the essential blackboard properties — data-driven scheduling, agents reacting to peers, dynamic round counts, concurrent contribution — while remaining **governable** (cost caps) and **observable** (definable terminal state, streamable consensus). The pure-pull end is documented as an optional future variant in [05 §7](05-control-unit.md#7-optional-future-pure-pull-with-hermes-crons); it is explicitly *not* the baseline.

> [!NOTE] Why "agents read the board" is the linchpin
> The difference between a referee and a puppeteer is not *who triggers the agent* — both involve the daemon making a call. It is **what the agent sees**. A puppeteer's agent sees only a hand-crafted prompt. A referee's agent sees the *entire relevant board state* and decides for itself how to respond. This is why [04 — PatchBoard](04-blackboard-protocol.md) (giving agents a real board to read and a structured way to write) is the substantive fix, and [05 — Control Unit](05-control-unit.md) is the governance layer on top.

## 4. What each turn's agent payload looks like (target)

Contrast with today's payload ([Gap G2](01-gap-analysis.md#3-evidence-agents-are-stateless-text-functions-blind-to-the-board)), which is just `{task_id, description, role_prompt}`:

```jsonc
// Daemon → Agent (target). The agent receives the board index and a read affordance,
// not just a prompt.
{
  "task_id": "task-a8f2",
  "turn_id": "turn-7",
  "role": "critic",
  "role_prompt": "<persona for this role>",
  "model": "gemini-2.5-flash",           // triage-selected — must reach the run request (05 §8, 06 §3)
  "objective": "Evaluate NVIDIA as a 2026 long position",
  "phase": "Debate",
  "board_index": [                       // table of contents, not full payloads
    {"id": "e-12", "type": "finding",  "author": "expert.valuation", "salience": 0.82, "title": "DCF implies 18% upside"},
    {"id": "e-13", "type": "finding",  "author": "expert.supply",    "salience": 0.71, "title": "Tariff exposure on HBM"},
    {"id": "e-14", "type": "critique", "author": "critic",           "salience": 0.40, "title": "DCF discount rate unjustified"}
  ],
  "open_questions": ["Is the 8% WACC defensible?"],
  "read_entry_contract": {
    "mode": "prehydrated",                // prehydrated (default) | daemon_api | workspace_file
    "view_budget_tokens": 4000,           // bounded view — see the decision note below
    "entries": { "e-12": {"…full payload…": "…"}, "e-13": {"…": "…"}, "e-14": {"…": "…"} },
    "overflow_endpoint": "/tasks/task-a8f2/board/entries",  // pull-by-id escape hatch (bearer-auth)
    "allowed_ids": ["e-12", "e-13", "e-14"]
  },
  "patch_target_schema": "finding|critique|rebuttal",  // what the kernel will accept this turn
  "budget_remaining_usd": 0.0421
}
```

The agent reads the index, pulls the specific entries it needs (by ID), reasons, and returns **patches** plus a **trace** (see [04](04-blackboard-protocol.md) and [06](06-agent-traces.md)). The `read_entry_contract` is not optional — it is the concrete mechanism that makes "agents read the board" true:

- **`prehydrated` (the default)**: the daemon materializes the view — the index plus full payloads for `allowed_ids` — directly in the turn request, under an explicit **view budget**.
- **`daemon_api` (the escape hatch)**: for boards that outgrow the budget, the node calls `GET /tasks/{task}/board/entries?ids=e-12,e-13` to pull specific entries by ID mid-turn.
- **`workspace_file`**: a delivery variant of `prehydrated` — the agent server writes the selected entries to a per-turn file in the Hermes workspace and tells the agent where to read them. Useful when the agent consumes the board via file tools rather than prompt context.

> [!IMPORTANT] Decision: default to `prehydrated` bounded views, sized by `coordination.view_budget_tokens`
> This is no longer an open choice ([Q11 — resolved](10-migration-and-rollout.md#3-open-questions-verify-before-building)). [PatchBoard (2026)](https://arxiv.org/abs/2605.29313) measured that kernel-materialized **bounded views** were one of the two largest contributors to success in ablation, and that the *smallest* tested context budget had the best success-per-token profile — more board state did not help and cost more. So the daemon owns view construction: it selects entries by salience/pressure under `coordination.view_budget_tokens` (default 4000; per-role overrides allowed), includes full bodies for the selected ids, and compresses anything beyond the budget into index lines (id + type + title + salience) that the agent can pull via `daemon_api` if it truly needs them. This also keeps the per-turn cost floor predictable ([05 §5](05-control-unit.md#5-cost-governance--safety-rails)).
>
> **Authentication (specified, not hand-waved):** every node↔daemon HTTP surface — the board-read endpoint above and the trace-ingest endpoint ([06 §5](06-agent-traces.md#5-transport--persistence)) — is authenticated with a shared bearer secret, `BMAS_NODE_KEY`, set in `.env` on the control plane and on each node, presented by `agent/api_server.py` as `Authorization: Bearer …` and validated by the daemon on every request. This mirrors the existing `API_SERVER_KEY` pattern already used for the Hermes gateway ([doc 12 §4](12-hermes-and-node-topology.md#4-enabling-the-runs-api-the-phase-1-unblocker--done-on-all-3-nodes-2026-06-07)). The daemon fails fast at startup if the key is unset (matching `config.py`'s existing validation style). Nodes never receive Redis credentials — the daemon is the only Redis client ([06 §5](06-agent-traces.md#5-transport--persistence)).

It can also return `{"action": "decline", "reason": "no new information"}` — a first-class blackboard behavior impossible today (but note: decline-gating belongs in the daemon, not in a dispatched run — [05 §5](05-control-unit.md#5-cost-governance--safety-rails)).

## 5. Component ownership map (files that change)

| Concern | New/changed module | Replaces / extends |
|:--|:--|:--|
| Blackboard kernel (validate/commit/emit) | `daemon/src/core/kernel.py` (new) | logic currently inline in `orchestrator.py` |
| Board model + Redis v2 + event log | `daemon/src/core/blackboard.py` (rewrite) | current `Blackboard` |
| Control Unit (OODA scheduler + Decider) | `daemon/src/core/control_unit.py` (new) | `Orchestrator._standard_flow` / `_complex_research_flow` |
| Roles & schemas | `daemon/src/models/personas.py` (extend), `daemon/src/models/schemas.py` (new) | current `DEFAULT_PERSONAS` |
| Agent runtime (traces) | `agent/api_server.py` (rewrite to Runs API) | current `_run_hermes` |
| Persistence | `daemon/src/database.py` (migration v2) | current schema |
| SSE events | `daemon/src/routes/events.py` (additive event types) | current event set |
| Live board UI | `mission-control/src/components/features/BlackboardGraph.tsx` (new) | evolves `DAGVisualizer.tsx` |
| Trace UI | `mission-control/src/components/features/AgentTrace.tsx` (new) | enriches Logs tab |

> [!IMPORTANT]
> Nothing here requires abandoning the dual-write model, the SSE architecture, LiteLLM, or triage. The inversion is surgical: change *who reads the board* and *what gets written*, then layer governance and observability on the existing rails.

➡️ Continue to [04 — Blackboard Protocol (PatchBoard)](04-blackboard-protocol.md).
