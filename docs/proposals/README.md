[🏠 Index](../README.md) | [🏛️ Architecture](../architecture/README.md) | [📋 Roadmap](../roadmap/README.md) | [🎨 Design System](../design/DESIGN.md)

# Proposal — From Orchestrator-Worker to a True (Distributed) Blackboard

> [!ABSTRACT] Purpose
> This folder is a **comprehensive implementation guide** for evolving bMAS from its current orchestrator-worker pipeline into a *true* blackboard multi-agent system, faithful to [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701) — running as a real **distributed system** (3 Hermes nodes + a "brain" orchestrator with triage), with chat-class capabilities the paper never needed: **PDF/file input, configurable output directories, and agent-created artifacts** ([doc 17](17-files-and-artifacts.md)). It also specifies the observability work required to make the system **legible**: real agent traces, a live blackboard graph, and a real-time worker visualization — all in the existing Mission Control design language.
>
> Two further coordination paradigms — **PatchBoard** ([Zhang, Shi & Wang 2026](https://arxiv.org/abs/2605.29313), [doc 11](11-variant-patchboard.md)) and **true stigmergy** ([doc 16](16-variant-stigmergic.md)) — are specified as *selectable variants* behind a per-task UI dropdown, **not** as part of the core. The non-variant docs describe only the 2025 paper's architecture; the engine and UI are built with explicit seams so the variants drop in later without rewrites.
>
> These documents are a **design contract and migration plan**, not finished code. Every recommendation is grounded in the current source, cites the exact files that must change, and ships with concrete schemas and code sketches — explicit enough for a lower-capability coding model to implement without judgment calls.

---

## 1. The one-paragraph verdict

The diagnosis is correct: **bMAS today is a supervisor pipeline wearing blackboard vocabulary.** The daemon is a puppeteer that runs a fixed `triage → plan → execute → audit` sequence, dispatches one blocking HTTP call per role, and uses Redis as *daemon-side bookkeeping and a UI event bus* rather than a coordination medium — no agent ever reads or writes it. Agents never read the board, the "debate" is sequential string concatenation, intermediate agent reasoning is discarded at the edge, and there is no way to hand the system a file or get files back out. The fix is **not** to delete the orchestrator (the bMAS paper keeps a Control Unit) — it is to **invert the data flow**: agents read the live board and write natural-language entries to it through a deterministic gateway; an LLM Control Unit acts as the paper's *referee*, selecting who speaks each round and detecting termination; and every board change becomes an observable, replayable event that powers the UI.

## 2. Reading order

**Core (the 2025 paper, distributed — this is V0/V1):**

| # | Document | What it covers |
|:--|:--|:--|
| 01 | [Gap Analysis](01-gap-analysis.md) | Evidence-based teardown of the current system. Exactly where and why it is orchestrator-worker — plus the missing file pipeline (G6). |
| 02 | [Peer Review](02-peer-review.md) | Point-by-point critique of the external model's suggestions — adopt/adapt/defer/reject — re-graded after the LbMAS-core decision, plus our own additions. |
| 03 | [Target Architecture](03-target-architecture.md) | The distributed LbMAS target: Blackboard + Knowledge Sources + Control Unit, the blackboard cycle, the agent payload contract, and the **`CoordinationVariant` seam + seams checklist** (§6). |
| 04 | [Blackboard Protocol](04-blackboard-protocol.md) | Natural-language entries in typed envelopes, the **Board Gateway** (validate/authorize/commit/emit), the event-sourced board with durability + fork, Redis v2, salience. |
| 05 | [Control Unit & Roles](05-control-unit.md) | The paper's LLM control unit + deterministic guards, the agent group (5 constant roles + AG-generated experts with model-pool diversity), Decider/SolE termination, private sub-boards, cost governance, HITL. |
| 06 | [Agent Traces](06-agent-traces.md) | **Prerequisite for all visualization.** Replacing `hermes -z` with the Hermes Runs API, the trace event schema, TaskResponse v2, ingestion, and persistence. |
| 07 | [Data Model](07-data-model.md) | SQLite migration v2 (board, events, traces, turns, **files, artifacts**), new daemon endpoints (incl. `/capabilities`), Redis schema, retention. |
| 08 | [UI — Blackboard Visualization](08-ui-blackboard-visualization.md) | The live blackboard graph and worker activity view — plus the **variant dropdown + panel registry** (§2.1) that keep Mission Control variant-extensible. |
| 09 | [UI — Agent Trace Inspector](09-ui-agent-trace-inspector.md) | The per-agent trace timeline, tool-call cards, turn inspector, and real cost integration. |
| 10 | [Migration & Rollout](10-migration-and-rollout.md) | Phased plan (incl. Phase 2F files/artifacts), feature flags, backward compatibility, risks, and the live-verified open-questions table. |
| 12 | [Hermes & Node Topology](12-hermes-and-node-topology.md) | Verified live cluster state, **paper agents on 3 hosts via profiles**, per-role SOUL.md, the Runs API enablement, and leveraging the full Hermes API. |
| 13 | [UI Showcase Density](13-ui-showcase-density.md) | The information-dense "command center" UI philosophy — legible maximalism, the Mission cockpit, agent "minds", and how variant panels slot in. |
| 14 | [Implementation Runbook (Exact Prompts)](14-implementing-with-antigravity-agents.md) | **The literal build log.** 41 numbered steps, each tagged 🆕 new-agent / ♻️ resume / 🧑 you — every prompt to paste, in order. Hard **actor≠critic** separation, GitHub + live-node gates. |
| 15 | [Novelty & Research Directions](15-novelty-and-research-directions.md) | **Why it matters.** Honest prior-art framing, the distribution contributions, academic deep dives, and ranked showcase demos/experiments. |
| 17 | [Files & Artifacts](17-files-and-artifacts.md) | **PDF/file input → board attachments → node staging**, and **agent outputs → artifact sync → `{artifacts_dir}/{task}/`** — the `storage.*` config on the brain node, security, and the UI surfaces. |

**Variants (post-V1 drop-ins; the engine/UI seams for them are V1 merge gates):**

| # | Document | What it covers |
|:--|:--|:--|
| 11 | [Variant — PatchBoard](11-variant-patchboard.md) | The 2026 paper as a selectable variant: Architect-generated schemas + **dynamically generated workers**, validated JSON-Patch mutation through a deterministic kernel, circuit policy, bounded views — and its UI adapter (blueprint inspector, transaction log). |
| 16 | [Variant — Stigmergic](16-variant-stigmergic.md) | True stigmergy as a selectable variant: no control unit, no roles — a decaying **pressure field**, self-activating universal actors, stable-basin termination — and its UI adapter (pressure heatmap, decay strip). |

## 3. Design tenets for this proposal

1. **Invert, don't amputate.** Keep the daemon, SQLite dual-write, LiteLLM, triage, and Mission Control. Change *who reads the board* and *what gets written to it*.
2. **The paper's medium is natural language — keep it.** Agents communicate in prose (typed envelopes around NL bodies); the chat-LLM + coding-harness use cases this system targets *prefer* it. Determinism lives **at the boundary**: agents propose, the deterministic Board Gateway disposes. Schema-grounded mutation is the PatchBoard *variant*, not the core ([02 §2.4](02-peer-review.md#24-patchboard--json-patch--deterministic-kernel-suggestion-4--defer-to-variant-absorb-its-determinism-lessons)).
3. **Every state change is an event.** The board is an event log first and a snapshot second — for every variant. This single decision powers replay, fork, the live graph, and crash recovery for free.
4. **Observability is a feature, not an afterthought.** Traces must be fixed *before* the visualization work, because you cannot visualize data you are not collecting.
5. **Build the core, architect for the variants.** V1 implements the 2025 paper (LLM Control Unit + 5 constant roles + generated Experts). Everything coordination-specific lives behind the **`CoordinationVariant` seam** ([03 §6](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms)) and the UI's **variant dropdown + panel registry** ([08 §2.1](08-ui-blackboard-visualization.md#21-the-variant-selector-and-the-panel-registry)), so PatchBoard ([11](11-variant-patchboard.md)) and stigmergic ([16](16-variant-stigmergic.md)) are drop-ins. The seams checklist is a merge gate on every V1 PR.
6. **Files in, files out.** A chat-class system accepts PDFs and produces codebases. Storage locations are operator config on the brain node (`storage.user_media_dir`, `storage.artifacts_dir` — e.g. `/opt/output/{task}/`), uploads become first-class board attachments, and agent-created files sync back as versioned artifacts ([doc 17](17-files-and-artifacts.md)).
7. **Tokens are sacred; density is not minimalism.** All UI composes from the `ui/` primitives and tokens in [DESIGN.md](../design/DESIGN.md). But because this is a **showcase artifact**, the visualization surfaces favor *legible maximalism* — surface as much agent thought and board state as possible at once. See [doc 13](13-ui-showcase-density.md).

> [!NOTE] Grounded in the live cluster
> The control plane can SSH to every node. The setup was inspected directly (2026-06-06/07): all three agent nodes are **byte-for-byte identical** — Hermes **v0.15.1**, a **single generic SOUL.md** (no profiles, no per-node customization). The Phase-1 prerequisite has since been completed live: the **Runs API gateway is now enabled and boot-persistent on all 3 nodes** (`:8642`) and the `:9119` dashboard was restored. These verified facts shape the plan — see [doc 12](12-hermes-and-node-topology.md) and the refreshed [HERMES_API.md](../HERMES_API.md#appendix-c-verified-live-cluster-state-2026-06-06-updated-2026-06-07).

## 4. What "done" looks like

- [ ] An agent posts a finding to the board; a *different* agent reads it, disagrees, and posts a critique — without the daemon telling it to.
- [ ] Multiple agents work concurrently on one task; the board shows their contributions interleaving in real time.
- [ ] The loop halts via the Decider's accepted solution or the SolE majority vote, not when a fixed pipeline ends.
- [ ] Opening a running task shows live agent traces (reasoning steps, tool calls, token deltas), not just "Processing…".
- [ ] The Blackboard tab renders a live graph of entries and the agents acting on them, animating as entries land — including the Cleaner visibly pruning.
- [ ] Killing and restarting the daemon mid-task replays the board from the event log without corruption.
- [ ] A user attaches a PDF at submit; agents cite its content; a "build a codebase" task materializes files under the configured `{artifacts_dir}/{task}/`, browsable in the UI.
- [ ] The composer offers the variant dropdown (traditional enabled; PatchBoard/stigmergic visible but disabled), and a dummy variant UI adapter mounts with zero shell edits.

### Showcase / novelty "done" (the artifact goal)

Beyond the engineering bar above, the project is an artifact to *show* — see [doc 15](15-novelty-and-research-directions.md). "Done" for the showcase means:

- [ ] At least the core-only demos land: **live convergence**, the **cost/locality frontier**, and **counterfactual replay** ([doc 15 §5](15-novelty-and-research-directions.md#5-concrete-demos--experiments-to-build-ranked-by-wow-per-effort)) — these need V1 hooks that are now tracked as phase tasks.
- [ ] The **evaluation harness** ([doc 10 Phase E](10-migration-and-rollout.md#phase-e-cross-cutting--begin-at-phase-1--evaluation-ab--showcase-instrumentation)) produces a comparable metrics table (accuracy/$/tokens/latency/rounds), so novelty claims are *measured*, not asserted.
- [ ] V1 ships without foreclosing the variants: every PR passes the [seams checklist](03-target-architecture.md#6-the-variant-seam-one-engine-three-coordination-paradigms). The headline novel work — the **stigmergic regime + its robustness experiment** ([doc 16](16-variant-stigmergic.md)) — remains a clean drop-in, built separately.

> [!NOTE]
> Items that depend on Hermes-runtime capabilities (the Runs API, profiles, crons) are flagged throughout and consolidated in [§10 Open Questions](10-migration-and-rollout.md#3-open-questions-verify-before-building). Verify them on a live node before committing engineering time.
