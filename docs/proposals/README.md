[🏠 Index](../README.md) | [🏛️ Architecture](../architecture/README.md) | [📋 Roadmap](../roadmap/README.md) | [🎨 Design System](../design/DESIGN.md)

# Proposal — From Orchestrator-Worker to a True Blackboard

> [!ABSTRACT] Purpose
> This folder is a **comprehensive implementation guide** for evolving bMAS from its current orchestrator-worker pipeline into a *true* blackboard multi-agent system, faithful to [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701). It also specifies the observability work required to make that system **legible**: real agent traces, a live blackboard graph, and a real-time worker visualization — all in the existing Mission Control design language.
>
> These documents are a **design contract and migration plan**, not finished code. Every recommendation is grounded in the current source, cites the exact files that must change, and ships with concrete schemas and code sketches.

---

## 1. The one-paragraph verdict

The diagnosis is correct: **bMAS today is a supervisor pipeline wearing blackboard vocabulary.** The daemon is a puppeteer that runs a fixed `triage → plan → execute → audit` sequence, dispatches one blocking HTTP call per role, and uses Redis as a *write-only mirror for the UI* rather than a coordination medium. Agents never read the board, the "debate" is sequential string concatenation, and intermediate agent reasoning is discarded at the edge. The fix is **not** to delete the orchestrator (the bMAS paper keeps a Control Unit) — it is to **invert the data flow**: agents read and write a structured, schema-validated blackboard; a thin LLM Control Unit acts as a *referee* that schedules opportunistically and detects convergence; and every mutation becomes an observable, replayable event that powers the UI.

## 2. Reading order

| # | Document | What it covers |
|:--|:--|:--|
| 01 | [Gap Analysis](01-gap-analysis.md) | Evidence-based teardown of the current system. Exactly where and why it is orchestrator-worker. |
| 02 | [Peer Review](02-peer-review.md) | Point-by-point critique of the external model's suggestions — what to adopt, what to reject, what to adapt — plus our own additions. |
| 03 | [Target Architecture](03-target-architecture.md) | The true-blackboard target: Control Unit + Knowledge Sources + workspace, the OODA execution loop, and the push/pull spectrum. |
| 04 | [Blackboard Protocol (PatchBoard)](04-blackboard-protocol.md) | The core reliability fix: JSON-Patch mutations, the deterministic kernel, entry schema, Redis v2 key layout, optimistic concurrency, and salience/decay. |
| 05 | [Control Unit & Roles](05-control-unit.md) | The cyclic scheduler, the paper role group (Planner/Decider/Critic/Conflict-Resolver/Cleaner + generated Experts), consensus, and cost governance. |
| 06 | [Agent Traces](06-agent-traces.md) | **Prerequisite for all visualization.** Replacing `hermes -z` with the Hermes Runs API, the trace event schema, ingestion, and persistence. |
| 07 | [Data Model](07-data-model.md) | SQLite migrations and the unified Redis schema for board entries, patches, traces, and runs. |
| 08 | [UI — Blackboard Visualization](08-ui-blackboard-visualization.md) | The live blackboard graph and worker activity view, built from existing primitives and design tokens. |
| 09 | [UI — Agent Trace Inspector](09-ui-agent-trace-inspector.md) | The per-agent trace timeline, tool-call cards, and how traces wire into existing tabs. |
| 10 | [Migration & Rollout](10-migration-and-rollout.md) | Phased plan, feature flags, backward compatibility, risks, and the verification checklist. |
| 11 | [Extensibility & Variants](11-extensibility-and-variants.md) | The `CoordinationStrategy` seam that lets V1 (paper) and a future **pure-stigmergic V2** (no roles, no control unit, pressure + decay) share one engine. |
| 12 | [Hermes & Node Topology](12-hermes-and-node-topology.md) | Verified live cluster state, **paper agents on 3 hosts via profiles**, per-role SOUL.md, enabling the Runs API, and leveraging the full Hermes API. |
| 13 | [UI Showcase Density](13-ui-showcase-density.md) | The information-dense "command center" UI philosophy for demoing the system — legible maximalism, the Mission view, and agent "minds". |
| 14 | [Implementation Runbook (Exact Prompts)](14-implementing-with-antigravity-agents.md) | **The literal build log.** 37 numbered steps, each tagged 🆕 new-agent / ♻️ resume / 🧑 you — every prompt to paste, in order. Hard **actor≠critic** separation (implementer vs reviewer/verifier are different conversations), GitHub + live-node gates. You only merge + handle escalations. |
| 15 | [Novelty & Research Directions](15-novelty-and-research-directions.md) | **Why it matters.** What's genuinely novel (distribution, the dual-regime pressure substrate, legible emergence), academic deep dives, the distributed-only contributions, and ranked showcase demos/experiments. |

## 3. Design tenets for this proposal

1. **Invert, don't amputate.** Keep the daemon, SQLite dual-write, LiteLLM, triage, and Mission Control. Change *who reads the board* and *what gets written to it*.
2. **Determinism at the boundary.** LLMs propose; a deterministic kernel disposes. Free-form text never mutates shared state directly — it arrives as validated patches.
3. **Every state change is an event.** The board is an event log first and a snapshot second. This single decision powers replay, the live graph, and trace correlation for free.
4. **Observability is a feature, not an afterthought.** Traces must be fixed *before* the visualization work, because you cannot visualize data you are not collecting.
5. **Build V1, architect for V2.** V1 implements the paper (Control Unit + 5 constant roles + generated Experts). The substrate is built behind a `CoordinationStrategy` seam so a future **pure-stigmergic** variant (no roles, no control unit, emergent consensus via pressure + decay) drops in without a rewrite. See [doc 11](11-extensibility-and-variants.md).
6. **Tokens are sacred; density is not minimalism.** All UI composes from the `ui/` primitives and tokens in [DESIGN.md](../design/DESIGN.md). But because this is a **showcase artifact**, the visualization surfaces favor *legible maximalism* — surface as much agent thought and board state as possible at once. See [doc 13](13-ui-showcase-density.md).

> [!NOTE] Grounded in the live cluster
> The control plane can SSH to every node. The setup was inspected directly (2026-06-06/07): all three agent nodes are **byte-for-byte identical** — Hermes **v0.15.1**, a **single generic SOUL.md** (no profiles, no per-node customization). The Phase-1 prerequisite has since been completed live: the **Runs API gateway is now enabled and boot-persistent on all 3 nodes** (`:8642`) and the `:9119` dashboard was restored. These verified facts shape the plan — see [doc 12](12-hermes-and-node-topology.md) and the refreshed [HERMES_API.md](../HERMES_API.md#appendix-c-verified-live-cluster-state-2026-06-06-updated-2026-06-07).

## 4. What "done" looks like

- [ ] An agent posts a finding to the board; a *different* agent reads it, disagrees, and posts a critique — without the daemon telling it to.
- [ ] Multiple agents work concurrently on one task; the board shows their contributions interleaving in real time.
- [ ] The Control Unit halts a task when a consensus threshold is met, not when a fixed pipeline ends.
- [ ] Opening a running task shows live agent traces (reasoning steps, tool calls, token deltas), not just "Processing…".
- [ ] The Blackboard tab renders a live graph of entries and the agents acting on them, animating as patches land.
- [ ] Killing and restarting the daemon mid-task replays the board from the event log without corruption.

### Showcase / novelty "done" (the artifact goal)

Beyond the engineering bar above, the project is an artifact to *show* — see [doc 15](15-novelty-and-research-directions.md). "Done" for the showcase means:

- [ ] At least the V1-only demos land: **live consensus formation**, the **cost/locality frontier**, and **counterfactual replay** ([doc 15 §5](15-novelty-and-research-directions.md#5-concrete-demos--experiments-to-build-ranked-by-wow-per-effort)) — these need V1 hooks that are now tracked as phase tasks.
- [ ] The **evaluation harness** ([doc 10 Phase E](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation)) produces a comparable metrics table (accuracy/$/tokens/latency/rounds), so novelty claims are *measured*, not asserted.
- [ ] V1 ships without foreclosing V2: every PR passes the [seams checklist](11-extensibility-and-variants.md#6-the-seams-checklist-enforce-in-v1). The headline novel work — the **stigmergic regime + its robustness experiment** — remains a clean drop-in, built separately.

> [!NOTE]
> Items that depend on Hermes-runtime capabilities (the Runs API, profiles, crons) are flagged throughout and consolidated in [§10 Open Questions](10-migration-and-rollout.md#open-questions-verify-before-building). Verify them on a live node before committing engineering time.
