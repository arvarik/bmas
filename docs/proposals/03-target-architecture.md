[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Peer Review](02-peer-review.md) | [➡️ Next: Blackboard Protocol](04-blackboard-protocol.md)

# 03 — Target Architecture: A True (Distributed) LbMAS Blackboard

> [!ABSTRACT]
> This document defines the destination: a faithful, **distributed** implementation of the blackboard LLM-MAS from [Han & Zhang (2025), arXiv:2507.01701](https://arxiv.org/abs/2507.01701) ("LbMAS"), running on our 3-node Hermes cluster with a "brain" orchestrator (the daemon) and triage routing. It introduces the three canonical components, the **blackboard cycle** that replaces the fixed DAG, the agent turn contract, and the **variant seam** that lets two future coordination paradigms — [PatchBoard](11-variant-patchboard.md) and [true stigmergic](16-variant-stigmergic.md) — plug into the same engine later. Implementation detail lives in [04](04-blackboard-protocol.md), [05](05-control-unit.md), [06](06-agent-traces.md), and [17](17-files-and-artifacts.md).

> [!IMPORTANT] What this architecture is — and deliberately is not
> The primary product is a **chat-like multi-agent system**: a user submits a query (optionally with PDFs/files, [doc 17](17-files-and-artifacts.md)), watches a swarm of natural-language agents debate on a shared blackboard, and receives an answer and/or generated files. Agents communicate in **natural language**, exactly as in the paper — because chat answers, research writeups, and code produced by a coding-harness-style agent are all natural-language/free-text artifacts. We deliberately do **not** build the schema-grounded JSON-Patch substrate from [PatchBoard (2026)](https://arxiv.org/abs/2605.29313) into the core: that architecture replaces natural-language dialogue entirely and has no LLM control unit, which makes it a *different coordination paradigm*, not an upgrade. It is specified as a selectable variant in [doc 11](11-variant-patchboard.md). The earlier drafts of these proposals conflated the two; this revision separates them cleanly.

---

## 1. The three components

A classical blackboard system (Hayes-Roth, 1985; Han & Zhang, 2025) has exactly three parts. We map each onto a concrete bMAS owner.

| Component | Definition (paper) | bMAS owner (target) |
|:--|:--|:--|
| **Blackboard** | Shared workspace storing *all* messages and knowledge of the problem-solving process; public space + private spaces. Replaces per-agent memory modules. | Redis (live, event-sourced) + SQLite (durable archive). A thin deterministic **Board Gateway** in the daemon performs all writes ([04 §4](04-blackboard-protocol.md#4-the-board-gateway)). |
| **Knowledge Sources (agents)** | LLMs with role prompts that read the blackboard and write messages back. 5 constant roles (planner, decider, critic, conflict-resolver, cleaner) + per-query generated experts. | Hermes agents on the edge LXCs, invoked per turn via the Runs API ([06](06-agent-traces.md)), reading the board and returning **natural-language entries** ([04 §3](04-blackboard-protocol.md#3-the-agent-response-contract)). |
| **Control Unit** | An LLM that, each round, reads the query + current blackboard + agent ability descriptions and **selects the subset of agents to act next**. The cycle repeats until the decider gives a solution or max rounds elapse. | An LLM-assisted scheduler with deterministic guard rails in the daemon ([05](05-control-unit.md)). The **referee**, not the puppeteer. |

The single most important change versus today: **the daemon stops being a Knowledge Source.** Right now it *is* the planner/executor/auditor coordinator that holds the solution logic ([Gap G1](01-gap-analysis.md#2-evidence-the-control-component-encodes-the-solution)). In the target, the daemon owns only the Board Gateway, the Control Unit, triage, file storage, and observability; the *reasoning* lives entirely in the Knowledge Sources, which coordinate through the board.

```
                          ┌────────────────────────────────────────────┐
                          │          CONTROL UNIT (daemon, referee)      │
                          │  • genesis: triage → expert generation (AG)  │
                          │  • per round: LLM selects agent subset       │
                          │    from {constants + experts} given board    │
                          │  • deterministic guards: rounds/budget/stall │
                          │  • decider solution or SolE vote → terminate │
                          └───────────────┬─────────────────────────────┘
                                          │ activates selected agents (≤1 per node concurrently)
             ┌────────────────────────────┼─────────────────────────────┐
             ▼                            ▼                             ▼
    ┌─────────────────┐         ┌─────────────────┐          ┌─────────────────┐
    │ KS: e.g. Critic │         │ KS: e.g. Expert │          │ KS: e.g. Planner│
    │ (Hermes node-1) │         │ (Hermes node-2) │          │ (Hermes node-3) │
    └────────┬────────┘         └────────┬────────┘          └────────┬────────┘
             │ returns NL entries         │ returns NL entries         │ returns NL entries
             ▼                            ▼                            ▼
    ┌──────────────────────────────────────────────────────────────────────────┐
    │                      BOARD GATEWAY (daemon, deterministic)                  │
    │  validate envelope → authorize by role capability → stamp → append event    │
    │  → update snapshot → emit SSE                                               │
    └──────────────────────────────────┬───────────────────────────────────────┘
                                        ▼
    ┌──────────────────────────────────────────────────────────────────────────┐
    │  BLACKBOARD   public entries · private sub-boards · event log · files      │
    │  Redis (live + Pub/Sub)  ⇄  SQLite (durable archive, replay)               │
    └──────────────────────────────────┬───────────────────────────────────────┘
                                        │ SSE (board events, traces, consensus)
                                        ▼
                       Mission Control (live graph + trace inspector + artifacts)
```

## 2. The execution loop: the blackboard cycle replaces the DAG

The fixed `plan → execute → audit` sequence in `orchestrator.py` is replaced by the paper's bounded cyclic loop (Algorithm 1 of LbMAS), extended with our distribution, triage, and cost rails. This is the [roadmap's stated intent](../roadmap/control-unit.md), now made concrete.

```
GENESIS
  1. Triage classifies complexity → selects model tier, seeds max_rounds, expert count n.
  2. AG (agent-generating agent, one LiteLLM call) generates n query-related expert
     personas {(E_i, D_i)}. Each agent draws its base model from the tier's model pool
     (the paper's model-diversity mechanism).                       [05 §2]
  3. CU writes the objective entry (+ attachment entries for uploaded files, doc 17)
     and phase=Discovery to the board.

LOOP (round t = 1..K, until terminal):
  ┌── SELECT ────────────────────────────────────────────────────┐
  │ Deterministic pre-checks first (cheap, no LLM):               │
  │   decider already posted an accepted solution?  → TERMINATE   │
  │   round > max_rounds / budget exhausted / stalled? → EXIT     │
  │ Then the CU LLM call (bare LiteLLM, not a Hermes run):        │
  │   ConU(query, board, {D_1..D_n, constant roles})              │
  │   → subset of agents to act this round (1..max_concurrent)    │
  └────────────────────────────────────────────────────────────────┘
  ┌── ACT ────────────────────────────────────────────────────────┐
  │ Selected agents run (concurrently across nodes by default;    │
  │ sequentially if round_execution: sequential for paper-exact   │
  │ reproduction). Each reads the live board, reasons, returns    │
  │ natural-language entries. The Gateway validates + appends +   │
  │ emits. Conflict-resolver flows may open private sub-boards.   │
  └────────────────────────────────────────────────────────────────┘
  ┌── MAINTAIN ───────────────────────────────────────────────────┐
  │ Gateway recomputes derived fields (salience; variant hook).   │
  │ Cleaner (when selected) removes redundant/useless entries —   │
  │ the paper's token-management mechanism.                       │
  └────────────────────────────────────────────────────────────────┘

FINALIZE (solution extraction, SolE)
  If the decider posted a solution → that is the answer.
  Else (max rounds reached) → every agent answers from the board; the answer with
  the highest cumulative similarity wins (paper's majority-similarity vote). [05 §3]
  Artifacts (files created during the task) are synced to the artifacts dir. [17]
```

Phases are explicit board state (`Discovery → Debate → Convergence → Solved`) and drive the UI's phase indicator directly. Note the loop's two LLM touchpoints at the control plane — the per-round **CU selection call** and the genesis **AG call** — are *bare LiteLLM calls*, never Hermes runs ([05 §5](05-control-unit.md#5-cost-governance--safety-rails) explains why: a Hermes run carries a ~16k-token context floor).

## 3. The push ↔ pull spectrum (and where we sit)

```
  PUPPETEER                    REFEREE (our target)                 SWARM
  (today)                                                       (stigmergic variant)
  ───────────────────────────────────────────────────────────────────────────►
  Fixed pipeline.     CU LLM selects relevant agents per     Agents poll the board via
  Agents blind.       round from board state. Agents READ    Hermes crons, self-
  No board reads.     the live board and may decline.        activate, no central CU.
  No cycles.          Cyclic, bounded, cost-governed.        Hard to govern/observe.
```

We target the **referee** position because it *is the paper's position*: LbMAS keeps a central control unit precisely because its ablation (Table 5) showed that removing it costs **~3–4× the tokens** for roughly equal accuracy — agents have enough autonomy to function without a referee, but the referee is what makes the system token-economical. The pure-pull end is the [stigmergic variant](16-variant-stigmergic.md); it is explicitly *not* the baseline.

> [!NOTE] Why "agents read the board" is the linchpin
> The difference between a referee and a puppeteer is not *who triggers the agent* — both involve the daemon making a call. It is **what the agent sees**. A puppeteer's agent sees only a hand-crafted prompt. A referee's agent sees the *blackboard* — every other agent's messages — and decides for itself how to respond. This is why [04 — Blackboard Protocol](04-blackboard-protocol.md) (giving agents a real board to read and a structured-envelope way to write) is the substantive fix, and [05 — Control Unit](05-control-unit.md) is the governance layer on top.

## 4. What each turn's agent payload looks like (target)

Contrast with today's payload ([Gap G2](01-gap-analysis.md#3-evidence-agents-are-stateless-text-functions-blind-to-the-board)), which is just `{task_id, description, role_prompt}`:

```jsonc
// Daemon → Agent (target). The agent receives the blackboard, not just a prompt.
{
  "task_id": "task-a8f2",
  "turn_id": "turn-7",
  "round": 3,
  "role": "critic",                       // or "expert.valuation" for a generated expert
  "role_prompt": "<persona for this role / generated expert identity>",
  "model": "gemini-2.5-flash",            // triage-selected — must reach the run request (05 §8, 06 §3)
  "objective": "Evaluate NVIDIA as a 2026 long position",
  "phase": "Debate",
  "board": {
    "mode": "full",                        // full (default, paper-faithful) | budgeted
    "entries": [                           // every public entry, chronological — the paper's
      {                                    // "whole blackboard as prompt" contract
        "id": "e-12", "type": "finding", "author": "expert.valuation",
        "title": "DCF implies 18% upside", "refs": [], "confidence": 0.74,
        "body": "Using a 5-year DCF with 8% WACC…  (natural language, full text)"
      },
      { "id": "e-13", "type": "finding",  "author": "expert.supply",  "…": "…" },
      { "id": "e-14", "type": "critique", "author": "critic", "refs": ["e-12"], "…": "…" }
    ],
    "overflow": null                       // budgeted mode only — see the note below
  },
  "attachments": [                          // uploaded files staged for this task (doc 17)
    { "file_id": "f-1", "name": "q3-earnings.pdf", "summary_entry": "e-2",
      "fetch": "/tasks/task-a8f2/files/f-1" }
  ],
  "workspace": { "task_dir": "/opt/bmas-workspace/task-a8f2" },   // node-local scratch (doc 17 §5)
  "directives": ["Focus on the WACC assumption this round"],       // operator hints (05 §6)
  "response_contract": "entries_v1",       // the envelope the agent must return (04 §3)
  "budget_remaining_usd": 0.0421
}
```

The agent reads the board, reasons (with tools — web, code, files), and returns **one or more natural-language entries** plus a **trace** (see [04 §3](04-blackboard-protocol.md#3-the-agent-response-contract) and [06](06-agent-traces.md)). It can also return `{"action": "decline", "reason": "no new information"}` — a first-class blackboard behavior impossible today (but note: decline-gating belongs in the daemon, not in a dispatched run — [05 §5](05-control-unit.md#5-cost-governance--safety-rails)).

> [!IMPORTANT] Decision: full-board context by default, budgeted as the safety valve
> The paper's contract is explicit: *"each selected agent takes the contents of the whole blackboard as a prompt to its LLM."* Token growth is managed by the **Cleaner** (which *removes* redundant messages — the paper's ablation shows marking-instead-of-removing measurably hurts) and by entry bodies being debate-sized, not document-sized (large content lives in files/artifacts and is referenced by entries — [doc 17 §4](17-files-and-artifacts.md#4-attachments-on-the-board)). So the default is `board.mode: full`.
>
> The safety valve: when the serialized board exceeds `coordination.view_budget_tokens` (default 12000), the daemon switches the turn to `mode: budgeted` — full bodies for the top-salience entries within budget, **index lines** (id + type + author + title) for the rest, and an `overflow` pull endpoint (`GET /tasks/{task}/board/entries?ids=…`) so the agent can fetch specific bodies by id mid-turn. This keeps worst-case cost bounded without changing the paper's semantics for normal tasks.
>
> **Authentication (specified, not hand-waved):** every node↔daemon HTTP surface — the board-read endpoint above, the trace-ingest endpoint ([06 §5](06-agent-traces.md#5-transport--persistence)), the file-fetch and artifact-ingest endpoints ([17 §5–6](17-files-and-artifacts.md#5-staging-inputs-to-nodes)) — is authenticated with a shared bearer secret, `BMAS_NODE_KEY`, set in `.env` on the control plane and on each node, presented by `agent/api_server.py` as `Authorization: Bearer …` and validated by the daemon on every request. This mirrors the existing `API_SERVER_KEY` pattern already used for the Hermes gateway ([doc 12 §4](12-hermes-and-node-topology.md#4-enabling-the-runs-api-the-phase-1-unblocker---done-on-all-3-nodes-2026-06-07)). The daemon fails fast at startup if the key is unset (matching `config.py`'s existing validation style). Nodes never receive Redis credentials — the daemon is the only Redis client.

## 5. Component ownership map (files that change)

| Concern | New/changed module | Replaces / extends |
|:--|:--|:--|
| Board model + Redis v2 + event log | `daemon/src/core/blackboard.py` (rewrite) | current `Blackboard` |
| Board Gateway (validate/authorize/append/emit) | `daemon/src/core/gateway.py` (new) | logic currently inline in `orchestrator.py` |
| Variant seam | `daemon/src/core/variants/__init__.py` (new, §6) | — |
| Control Unit (cycle + CU call + Decider + SolE) | `daemon/src/core/variants/traditional.py` (new) | `Orchestrator._standard_flow` / `_complex_research_flow` |
| Roles, expert generation (AG), capabilities | `daemon/src/models/personas.py` (extend) | current `DEFAULT_PERSONAS` |
| Agent runtime (traces + entries + artifacts) | `agent/api_server.py` (rewrite to Runs API) | current `_run_hermes` |
| Files & artifacts | `daemon/src/core/files.py` + `routes/files.py` (new) | — (no file support exists today) |
| Persistence | `daemon/src/database.py` (migration v2) | current schema |
| SSE events | `daemon/src/routes/events.py` (additive event types) | current event set |
| Live board UI | `mission-control/src/components/features/BlackboardGraph.tsx` (new) | evolves `DAGVisualizer.tsx` |
| Trace UI | `mission-control/src/components/features/AgentTrace.tsx` (new) | enriches Logs tab |
| Variant dropdown + panel registry | `mission-control/src/lib/variants.ts` + composer changes (new) | — ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)) |

> [!IMPORTANT]
> Nothing here requires abandoning the dual-write model, the SSE architecture, LiteLLM, or triage. The inversion is surgical: change *who reads the board* and *what gets written*, then layer governance and observability on the existing rails.

## 6. The variant seam: one engine, three coordination paradigms

The UI will eventually offer a per-task **variant dropdown** — `traditional` (this guide), `patchboard` ([doc 11](11-variant-patchboard.md)), `stigmergic` ([doc 16](16-variant-stigmergic.md)) — so the engine must be built with the variant boundary explicit from day one. The three paradigms differ in more than scheduling:

| | **Traditional (LbMAS — the core, this guide)** | **PatchBoard (variant, doc 11)** | **Stigmergic (variant, doc 16)** |
|:--|:--|:--|:--|
| Shared state | Typed entries with **natural-language bodies** | A **JSON state tree** under a task-specific schema | Same entries as traditional + a pressure field |
| Mutation | Append entries; cleaner removes; daemon-stamped | Validated **JSON-Patch** ops under write contracts | Append entries; pheromone reinforcement + decay |
| Control | **LLM control unit** selects agents per round | **No LLM CU** — deterministic event-driven rules from an Architect blueprint | **No CU at all** — agents self-activate on pressure |
| Knowledge sources | 5 constant roles + AG-generated experts | **Architect-generated workers** with per-task contracts | Identical roleless `universal` actors |
| Termination | Decider solution / SolE similarity vote | Workflow rules + deterministic circuit policy | Pressure falls below threshold (stable basin) |
| Best for | Chat, research, debate, NL/code generation | Long-horizon stateful/tool tasks; token-frugal audit trails | Robustness/emergence research, node-loss tolerance |

What stays **shared** (the engine): the board store + append-only event log ([04 §5](04-blackboard-protocol.md#5-the-board-as-an-event-log)), node dispatch + the Runs API transport ([06](06-agent-traces.md)), the trace pipeline, persistence/dual-write ([07](07-data-model.md)), files/artifacts ([17](17-files-and-artifacts.md)), SSE, triage, cost accounting, and the UI shell with its panel registry ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)).

What each variant owns — the `CoordinationVariant` interface:

```python
# daemon/src/core/variants/__init__.py  (the seam)
class CoordinationVariant(Protocol):
    """A coordination paradigm. Owns scheduling, the agent I/O contract,
    and termination. Never owns the board store, transport, traces, or UI shell."""

    name: str                                            # "traditional" | "patchboard" | "stigmergic"

    async def genesis(self, task: Task) -> None: ...
    #   traditional: triage → AG experts → objective entry
    #   patchboard: Architect → blueprint → initial state tree
    #   stigmergic: objective + initial pressure field

    def build_turn_payload(self, task: Task, actor: str, board: BoardView) -> dict: ...
    #   what the dispatched agent receives (board serialization differs per variant)

    def parse_agent_response(self, task: Task, actor: str, raw: TaskResponse) -> list[BoardMutation]: ...
    #   traditional: entry envelopes; patchboard: JSON-Patch ops; stigmergic: entries

    async def apply(self, task: Task, mutations: list[BoardMutation]) -> list[BoardEvent]: ...
    #   delegates to the Gateway (traditional/stigmergic) or the PatchBoard kernel

    async def step(self, task: Task, board: Board) -> StepResult: ...
    #   StepResult = { activations: list[Activation], terminal: bool, reason: str|None }

    def is_terminal(self, board: Board) -> tuple[bool, str | None]: ...
```

Selected per task (the dropdown) with a config default:

```yaml
coordination:
  variant: traditional        # traditional | patchboard | stigmergic | legacy_pipeline
```

> [!IMPORTANT] The rules that guarantee extensibility (the seams checklist)
> Enforced as a **merge gate** on every core PR ([doc 10 §1](10-migration-and-rollout.md#1-sequencing-rationale)):
>
> 1. **Coordination lives behind `CoordinationVariant`** — the daemon's task runner calls `variant.step()`; it never hardcodes a sequence, a role name, or "control unit."
> 2. **The event log is variant-agnostic.** `board_events` stores `{seq, actor, event_type, payload}` with *namespaced* event types (`entry_added` for traditional, `patch_committed` for patchboard, `pheromone_decayed` for stigmergic). Replay/fork operate on the generic shape ([04 §5](04-blackboard-protocol.md#5-the-board-as-an-event-log)).
> 3. **`actor`/`author` are opaque strings** everywhere (board, traces, DB, UI) — never enums. Generated experts (`expert.valuation`), patchboard workers (`worker.extractor-2`), and roleless actors (`universal-3`) must all render; the UI backs its role-color map with a deterministic fallback color generator ([13 §7](13-ui-showcase-density.md#7-component--token-additions)).
> 4. **Write authorization is capability-based**, not role-name-based ([04 §4](04-blackboard-protocol.md#capability-profiles-who-may-write-what)) — variants assign capabilities to actors however they like.
> 5. **Derived fields are computed in one pluggable hook** (`recompute_derived(task)` after each commit). Traditional registers salience; stigmergic registers pressure + decay; patchboard registers its state hash. No second code path.
> 6. **Dispatch supports both push and pull** (`participation_mode` per node) — push now, pull (crons) for stigmergic later.
> 7. **Termination is a variant method** (`is_terminal`), not a task-runner `return`.
> 8. **The UI is registry-driven**: variant dropdown options come from the daemon's capabilities endpoint, and each variant registers its panels/graph adapters instead of being hard-wired into Mission Control ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)).

V0/V1 builds **only** the `traditional` variant (plus the `legacy_pipeline` escape hatch). The seam exists so docs [11](11-variant-patchboard.md) and [16](16-variant-stigmergic.md) are drop-ins, not rewrites.

➡️ Continue to [04 — The Blackboard Protocol](04-blackboard-protocol.md).
