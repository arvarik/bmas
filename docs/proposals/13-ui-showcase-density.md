[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Hermes & Node Topology](12-hermes-and-node-topology.md) | [➡️ Implementing with Antigravity Agents](14-implementing-with-antigravity-agents.md) | [🎨 Design System](../design/DESIGN.md)

# 13 — UI Philosophy for a Showcase: Information-Dense, Still Intuitive

> [!ABSTRACT]
> This project is an **artifact to demonstrate a novel system**. The governing UI goal is therefore not minimalism — it is **legible maximalism**: surface as much of the blackboard's state and every agent's thought process as possible, simultaneously, while keeping it scannable and snappy. This document amends the design philosophy for the visualization surfaces, defines the density patterns to use, and names the observability tools to draw inspiration from. It **supersedes the "quiet, clarity-over-density" framing** in docs [08](08-ui-blackboard-visualization.md) and [09](09-ui-agent-trace-inspector.md) for those surfaces — but it does **not** relax the token system.

---

## 1. Amending the design contract (deliberately, not casually)

[DESIGN.md §1](../design/DESIGN.md#1-design-principles) ranks *"clarity over density"* and *"quiet until important"* first. Those are right for an **operations** tool. This is also a **demonstration** tool. We resolve the tension explicitly rather than silently violating the contract:

> [!IMPORTANT] Proposed amendment to DESIGN.md
> Add a **"Showcase surfaces"** clause: *On the Blackboard, Trace, and Mission-Overview surfaces, density is a feature. Show all available signal at once; use hierarchy, motion, and progressive disclosure (not omission) to keep it legible. The token system, type scale, spacing scale, and accessibility rules remain fully binding.*

What stays sacred (non-negotiable, from [DESIGN.md](../design/DESIGN.md)):
- **Tokens only.** No new shades/spacing/radii invented inline ([§2](../design/DESIGN.md#2-color-system), [§4](../design/DESIGN.md#4-spacing--layout)). New agent-role colors go through [doc 08 §8](08-ui-blackboard-visualization.md#8-token--primitive-additions-do-this-first).
- **Primitive-first composition.** Build from `ui/` primitives ([§11](../design/DESIGN.md#11-file-organization)).
- **Snappy.** 60fps, no jank. Density must not cost responsiveness — see [§5 performance](#5-keeping-it-snappy-density-without-jank).
- **Accessibility.** Contrast, focus rings, `prefers-reduced-motion`, `aria-live` ([§10](../design/DESIGN.md#10-accessibility-requirements)).

What changes for showcase surfaces:
- "Empty by default, reveal on demand" → **"Rich by default, focus on demand."**
- "Quiet until important" → **"Everything visible; importance is encoded by size/color/motion, not by hiding."**

## 2. Inspiration: agent-observability tools that nail dense + legible

Borrow concrete patterns from the best LLM/agent trace UIs (study these, don't copy chrome):

| Tool | Pattern worth stealing |
|:--|:--|
| **LangSmith / LangGraph Studio** | Tree-of-runs with a synced graph view; click a graph node → its trace; latency/token chips on every span |
| **Langfuse** | Nested span timeline with token/cost per span; session→trace→observation drill-down |
| **Arize Phoenix** | Trace waterfall + embedding/cluster views; "what's anomalous" surfacing |
| **AgentOps** | Multi-agent session replay; per-agent timelines on parallel swimlanes |
| **W&B Weave** | Side-by-side trace compare; inline tool I/O expanders |
| **Vercel AI SDK / v0 traces** | Streaming token shimmer + tool-call cards that fill in live |
| **Datadog APM flame graphs** | Dense flame/lane layout that stays readable at a glance via color + hierarchy |
| **Perfetto / Chrome tracing** | Zoomable, scrubable multi-track timeline for *many* concurrent actors |

The throughline: **a graph/overview for spatial understanding + a synchronized timeline/tree for temporal detail, with cheap per-span chips (tokens, cost, latency, status) everywhere.** That is exactly the bMAS need: the blackboard graph (spatial) + agent traces (temporal), cross-linked.

## 3. The "Mission" layout: a multi-panel command center

Add a new top-level **Mission view** for a running task (route `/task/[taskId]/mission`, or make it the default tab for live tasks) — a dense, single-screen cockpit that shows the whole system at once. This is the screenshot you show people.

```
┌ TopBar: task · phase pill · consensus ▓▓▓▓░ 0.62 · $0.041/$0.50 · ⏱ 2m13s · round 3/4 ─────────────┐
├──────────────────────────────┬───────────────────────────────┬─────────────────────────────────────┤
│  BLACKBOARD GRAPH (center)    │  AGENT MINDS (right rail)      │  GLOBAL FIREHOSE (far right, optional)│
│  ┌──────────────────────────┐ │  ┌───────────────────────────┐ │  live, interleaved trace lines from   │
│  │ force/dag graph of entries│ │  │ ● Critic   node-1  ▸search │ │  ALL agents, color-coded by author,   │
│  │ + pressure HEATMAP glow   │ │  │   "DCF rate seems low…"    │ │  auto-scroll, filterable:             │
│  │ + live patch animations   │ │  │   642 tok · $0.001         │ │  19:14:02 critic    reasoning …       │
│  │ + consensus convergence   │ │  ├───────────────────────────┤ │  19:14:02 expert.v  tool web_search   │
│  │                           │ │  │ ● Expert  node-2  ▸reason  │ │  19:14:03 expert.v  result 3 hits     │
│  └──────────────────────────┘ │  │   live token shimmer…      │ │  19:14:06 critic    patch→e-12        │
│  [timeline scrubber ◀━━━●━━▶] │  │ ○ Cleaner idle             │ │  …                                    │
├──────────────────────────────┴───────────────────────────────┴─────────────────────────────────────┤
│  PRESSURE / CONSENSUS STRIP: sparkline of max-pressure ↓ and consensus ↑ over rounds                  │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Four synchronized regions, all live, all from the existing SSE stream:

1. **Blackboard graph** (center) — entries as nodes, `refs` as edges, **pressure heatmap** overlay ([doc 11 §3](11-extensibility-and-variants.md#3-the-pressure-field-generalizes-salience)), patches animating in. The spatial anchor.
2. **Agent Minds** (right rail) — one live card per agent showing its *current thought* (latest `reasoning` line streaming), current tool, token meter, cost. This is the "thought process of each agent" the request asks for. Idle agents shown muted (state-awareness).
3. **Global Firehose** (optional far rail / toggle) — every trace event from every agent, interleaved and color-coded — the "I can see everything happening" wow factor. Filter by author/type. Borrowed from APM live tails.
4. **Pressure/Consensus strip** (bottom) — twin sparklines: max pressure falling, consensus rising. Narrates *why* the system is converging.
5. **Coordinator lane** (optional, atop the Agent Minds rail) — when the [Coordinator narration agent](05-control-unit.md#11-the-coordinator-narration-agent-optional-showcase-flourish) is enabled, a distinct card shows the control unit *thinking about what to think about*: its rationale for the next role selection on ambiguous turns. Visually set apart (it's meta — it reasons about the agents, not the task). Only lights up on escalation turns; muted otherwise.

> [!NOTE] Responsive degradation
> The cockpit is designed for large monitors (≥1440px). On smaller viewports (laptops, ≤1366px), panels collapse in priority order: **Firehose** hides first (toggle to reveal), then **Agent Minds** collapses to a horizontal strip of status pills above the graph. The **Blackboard graph** and **Pressure/Consensus strip** always remain visible — they are the spatial and temporal anchors. Use the existing DESIGN.md breakpoint tokens (`--bp-md`, `--bp-lg`) for thresholds; do not invent new breakpoints.

Everything cross-links: click a graph node → highlights the agent + scrolls the firehose; click an agent card → opens its [Turn Inspector](09-ui-agent-trace-inspector.md#4-turn-inspector-slide-over); hover a firehose line → flashes the graph node it touched.

## 4. Density patterns (the toolkit)

Use these to pack signal without clutter:

- **Per-span chips everywhere.** Every node, card, and trace line carries compact chips: tokens (`↑1.2k`), cost (`$0.001`), latency (`3.4s`), status dot, confidence bar. `--text-xs`, `tabular-nums`. Information at a glance, no drill-down required.
- **Encode importance, don't hide it.** Salience → node size/opacity; pressure → heat glow; recency → subtle fade. The eye finds the hotspots without the UI removing anything.
- **Progressive disclosure, not omission.** Tool-call cards collapse to one line but are always present; "Show more" on long reasoning; the firehose can collapse to a height-N strip. Detail is one interaction away, never deleted.
- **Synchronized multi-view.** Graph ⇄ minds ⇄ firehose ⇄ timeline all driven by one selection/hover state (a small Zustand slice). Selecting in one highlights in all — the LangSmith/Phoenix pattern.
- **Swimlanes for parallelism.** When ≥2 agents act concurrently, the trace timeline ([doc 09](09-ui-agent-trace-inspector.md)) renders **parallel lanes** (one row per agent), so concurrency is *visible* — the core blackboard claim, made literal. Borrowed from AgentOps/Perfetto.
- **Replay scrubber.** Because the board is event-sourced ([doc 04 §2](04-blackboard-protocol.md#2-the-board-as-an-event-log)), the whole mission can be scrubbed start→finish. Indispensable for a demo: "let me replay how the agents reached consensus."
- **Motion as narration.** Patch lands → node fades in; critique posted → dashed red edge draws; conflict → amber pulse; consensus reached → single green bloom. Motion explains the system to a first-time viewer. All gated by `prefers-reduced-motion`.

## 5. Keeping it snappy (density without jank)

Density is only acceptable if it stays at 60fps. Hard requirements:

- **Coalesce SSE → state.** Buffer high-frequency `trace`/`token_delta` events and flush on `requestAnimationFrame`; never `setState` per token ([doc 09 §8](09-ui-agent-trace-inspector.md#8-files)). One render per frame, max.
- **Virtualize long lists.** The firehose and trace timeline use windowed rendering (e.g. `@tanstack/react-virtual`) — render only visible rows even with thousands of events.
- **Cap live, page history.** Keep the last N events in memory for the live view; fetch older from `agent_traces` on scroll ([doc 07](07-data-model.md)).
- **`onlyRenderVisibleElements` for the graph.** Already used in `DAGVisualizer.tsx`; keep it for the board graph. Throttle layout recomputation; animate via CSS transforms, not layout.
- **Decouple panels.** Each region subscribes to the slice of stream state it needs (selector-based), so a token storm in the firehose doesn't re-render the graph.
- **Web Worker for layout** (if force-directed): run graph layout off the main thread when entry counts grow.

## 6. Showing each agent's "mind" (the headline feature)

The request's core: *show the thought process of each agent and how they work.* Concretely, per agent we surface:

| Signal | Source | Where |
|:--|:--|:--|
| Live reasoning stream | `trace` type `reasoning` | Agent Mind card (latest), Trace timeline (full) |
| Current tool + args + result | `hermes.tool.progress` + `tool_result` ([doc 12](12-hermes-and-node-topology.md)) | tool-call card |
| Token meter (live) | `token_delta` | Mind card chip, sparkline |
| What it's about to write | `patch_proposed` | "proposes critique→e-12" chip |
| What got accepted/rejected | `board_patch` / `patch_rejected` | graph + "Resulted in" footer |
| Its identity & boundaries | profile `SOUL.md` ([doc 12 §3](12-hermes-and-node-topology.md#3-soulmd-per-role-replace-the-single-generic-soul)) | hover/expand on the Mind card |
| Its learned skills & memory | `/api/skills`, `/api/memory` ([doc 12 §5](12-hermes-and-node-topology.md#5-hermes-feature--bmas-need-leverage-the-whole-api)) | agent detail / Infra page |
| Pending approval | `approval_request` ([doc 12 §5.1](12-hermes-and-node-topology.md#51-native-hitl-via-run-approvals)) | inline Approve/Deny in the Mind card |

This is only possible because [doc 06](06-agent-traces.md) fixes trace collection first. **No traces, no minds.**

## 7. Component & token additions

| Component | Built from | Note |
|:--|:--|:--|
| `MissionView` | layout + the four regions | new default for live tasks |
| `AgentMindCard` | `MetricCard` + live reasoning + chips | the "thought process" surface |
| `GlobalFirehose` | virtualized list + filter bar | color by author |
| `PressureHeatmap` | overlay on `BlackboardGraph` | uses pressure ZSet ([11 §3](11-extensibility-and-variants.md#3-the-pressure-field-generalizes-salience)) |
| `ConsensusPressureStrip` | Recharts sparklines | twin trend |
| `ParallelTraceLanes` | extends `AgentTrace` ([09](09-ui-agent-trace-inspector.md)) | swimlanes |

Token additions: a **heat ramp** (low→high pressure) expressed via existing status hues (blue→amber→red, reusing `--status-*`); a neutral **author-color generator** (deterministic hue from author string) so roleless/dynamic authors render without a fixed enum ([doc 11 §6](11-extensibility-and-variants.md#6-the-seams-checklist-enforce-in-v1)). Add both to `DESIGN.md` + `design-tokens.ts` before use.

## 8. State matrix (showcase surfaces)

| Component | Empty | Loading | Active | Error | Disabled |
|:--|:--|:--|:--|:--|:--|
| MissionView | "Submit a task to watch the swarm think." + illustration | skeletal four-panel frame | all panels live | per-panel error islands (one failing panel never blanks the cockpit) | N/A |
| AgentMindCard | "Idle — awaiting activation" (muted) | "Awakening…" shimmer | live reasoning stream | "Trace lost — reconnecting" | idle = muted, not hidden |
| GlobalFirehose | "No activity yet" | shimmer rows | virtualized live tail | "Stream dropped" banner + retry | filter-empty = "No events match filter" |

> [!TIP] The demo script this enables
> Submit a task → the graph seeds an objective node → expert/planner Mind cards light up and stream reasoning → findings pop onto the graph → a critique draws a red edge, the region glows with pressure → the firehose shows it all interleaving → consensus sparkline climbs → green bloom → scrub back to replay the whole emergence. That is the artifact worth showing.

➡️ Continue to [14 — Implementing with Antigravity Agents](14-implementing-with-antigravity-agents.md).
