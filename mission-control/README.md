# Mission Control — bMAS Dashboard

The real-time operations dashboard for **bMAS**. A task-scoped interface for monitoring, debugging, and controlling the AI swarm, with live SSE streaming, execution graphs, structured log viewers, and a blackboard command center. Built with Next.js 16, React 19, TypeScript, and vanilla CSS.

> Runs as Docker container `bmas-dashboard` on the control plane at port 9321.

## Features

| # | Feature | Technology | Data Source |
|:---|:---|:---|:---|
| 1 | **Task Overview** | React + Markdown renderer | REST `/api/tasks/{id}` + SSE |
| 2 | **Execution Graph (DAG)** | React Flow (`@xyflow/react`) | SSE `turn`, `narration` events |
| 3 | **Distributed Log Stream** | TanStack Virtual + detail drawer | SSE `log` events + REST fallback |
| 4 | **Agent Trace Inspector** | TanStack Virtual + structured viewer | REST `/api/tasks/{id}/trace` |
| 5 | **Blackboard Board** | Timeline / Threads / Graph views | SSE `board_*` events + REST `/api/tasks/{id}/board` |
| 6 | **Mission Cockpit** | 4-panel live layout | Composite: blackboard graph + agent minds + firehose + convergence |
| 7 | **Operator Controls (HITL)** | ActionButton + directive injection | POST `/api/hitl/{action}` |
| 8 | **Artifact Browser** | File tree + download links | REST `/api/tasks/{id}/artifacts` |
| 9 | **Cost Tracking** | Budget gauge + per-model breakdown | SSE `cost` events |
| 10 | **Skills Explorer** | Tabbed panel (per agent) | GET `/api/skills` |
| 11 | **Hardware Telemetry** | Multi-node cards + gauges | GET `/api/telemetry` (Beszel Hub) |

## Architecture

```
Browser (React 19)
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  Next.js 16 App Router                                    │
│                                                          │
│  ┌────────────────────┐  ┌──────────────────────────┐    │
│  │ src/app/api/       │  │ src/app/task/[taskId]/   │    │
│  │                    │  │                          │    │
│  │  /api/stream/*   ──┤  │  Overview (process+result)│   │
│  │  /api/tasks/*    ──┤  │  DAG (TurnGraph)         │    │
│  │  /api/hitl       ──┤  │  Logs (DistributedLog)   │    │
│  │  /api/submit     ──┤  │  Blackboard (Board)      │    │
│  │  /api/skills     ──┤  │  Mission (Cockpit)       │    │
│  │  /api/telemetry  ──┤  │  Artifacts (Browser)     │    │
│  └────────────────────┘  └──────────────────────────┘    │
│             │                                            │
└─────────────┼────────────────────────────────────────────┘
              │
    ┌─────────┼──────────────────┐
    │         │                  │
    ▼         ▼                  ▼
 Daemon    Redis            Beszel Hub
 :9000     :6379            :8090
```

## Project Structure

```
mission-control/
├── src/
│   ├── app/
│   │   ├── layout.tsx             # Root layout — fonts, metadata
│   │   ├── page.tsx               # Landing page — task list + submit
│   │   ├── ClientShell.tsx        # Client-side app shell wrapper
│   │   ├── LandingPageClient.tsx  # Landing page client component
│   │   ├── globals.css            # Design tokens (CSS custom properties)
│   │   ├── views.css              # View-specific styles
│   │   ├── task/[taskId]/         # Task-scoped pages (tabbed navigation)
│   │   │   ├── layout.tsx         #   Shared task layout with tab bar
│   │   │   ├── TaskStreamContext.tsx #   SSE stream provider for all tabs
│   │   │   ├── page.tsx           #   Overview — process summary + result
│   │   │   ├── dag/page.tsx       #   Execution Graph — TurnGraph swimlane
│   │   │   ├── logs/page.tsx      #   Distributed logs + agent trace
│   │   │   ├── blackboard/page.tsx #  Blackboard Board command center
│   │   │   ├── mission/page.tsx   #   Mission Cockpit (4-panel live)
│   │   │   └── artifacts/page.tsx #   Artifact browser + downloads
│   │   ├── agents/                #   Agent management pages
│   │   ├── skills/                #   Skills explorer page
│   │   ├── infra/                 #   Infrastructure telemetry page
│   │   └── api/                   # 20+ API route handlers
│   │       ├── stream/
│   │       │   ├── system/route.ts    # System-wide SSE (task lifecycle)
│   │       │   └── task/[taskId]/route.ts  # Task-scoped SSE (18 event types)
│   │       ├── tasks/
│   │       │   ├── route.ts           # GET /api/tasks (list)
│   │       │   └── [taskId]/
│   │       │       ├── route.ts       # GET /api/tasks/{id} (detail)
│   │       │       ├── board/route.ts # GET /api/tasks/{id}/board
│   │       │       ├── cost/route.ts  # GET /api/tasks/{id}/cost
│   │       │       ├── debate/route.ts# GET /api/tasks/{id}/debate
│   │       │       ├── logs/route.ts  # GET /api/tasks/{id}/logs
│   │       │       ├── trace/route.ts # GET /api/tasks/{id}/trace
│   │       │       ├── turns/route.ts # GET /api/tasks/{id}/turns
│   │       │       ├── files/         # File upload/download proxy
│   │       │       └── artifacts/     # Artifact retrieval proxy
│   │       ├── submit/route.ts        # POST /api/submit
│   │       ├── hitl/route.ts          # POST /api/hitl (pause/resume/directive)
│   │       ├── skills/route.ts        # GET/POST /api/skills
│   │       ├── capabilities/route.ts  # GET /api/capabilities
│   │       ├── profiles/route.ts      # GET /api/profiles
│   │       └── telemetry/route.ts     # GET /api/telemetry (Beszel proxy)
│   ├── components/
│   │   ├── features/              # 18 feature components
│   │   │   ├── TurnGraph.tsx          # Swimlane execution graph (rounds × agents)
│   │   │   ├── DistributedLogStream.tsx # Unified chronological log viewer
│   │   │   ├── BlackboardBoard.tsx    # Board command center (Timeline/Threads/Graph)
│   │   │   ├── AgentTrace.tsx         # Structured turn trace viewer (TanStack Virtual)
│   │   │   ├── AgentMindCard.tsx      # Per-agent live status card
│   │   │   ├── BlackboardGraph.tsx    # Force-directed board entry graph
│   │   │   ├── GlobalFirehose.tsx     # Unfiltered live event feed
│   │   │   ├── ConvergenceStrip.tsx   # Convergence progress indicator
│   │   │   ├── ConsensusMeter.tsx     # Consensus scoring visualization
│   │   │   ├── BudgetGauge.tsx        # Budget usage gauge
│   │   │   ├── ArtifactBrowser.tsx    # Grouped file tree with downloads
│   │   │   ├── AttachmentRail.tsx     # File preview slide-over
│   │   │   ├── SkillsExplorer.tsx     # Agent skills (per node)
│   │   │   ├── Telemetry.tsx          # Hardware health gauges
│   │   │   ├── TurnInspector.tsx      # Turn detail side panel
│   │   │   ├── ToolCallCard.tsx       # Tool call visualization
│   │   │   ├── VariantSelect.tsx      # Variant selector dropdown
│   │   │   ├── ReplayScrubber.tsx     # Timeline replay controls
│   │   │   ├── WorkerLane.tsx         # Per-agent swimlane in TurnGraph
│   │   │   └── board/                 # Board sub-components
│   │   │       ├── boardModel.ts      #   Merge-never-delete data model
│   │   │       ├── BoardEntryCard.tsx  #   Individual board entry card
│   │   │       ├── BoardEntryDetail.tsx#   Entry detail drawer
│   │   │       └── DebateThread.tsx    #   Debate thread visualization
│   │   ├── ui/                    # Design system primitives (9 components)
│   │   │   ├── Panel.tsx              # Container with header/body/states
│   │   │   ├── StatusBadge.tsx        # Status pill
│   │   │   ├── MetricCard.tsx         # Numeric display with trend
│   │   │   ├── ActionButton.tsx       # Button variants (Primary/Secondary/Danger)
│   │   │   ├── Skeleton.tsx           # Loading placeholder with shimmer
│   │   │   ├── EmptyState.tsx         # No-data placeholder
│   │   │   ├── Toast.tsx              # Notification system
│   │   │   ├── TaskSidebar.tsx        # Task navigation sidebar
│   │   │   └── TerminalPane.tsx       # Terminal-style output pane
│   │   └── layout/
│   │       └── TopBar.tsx             # App header bar
│   ├── hooks/
│   │   ├── useTaskStream.ts       # Task-scoped SSE — 18 event types, rAF batching, REST fallback
│   │   ├── useSystemStream.ts     # System-level SSE — task lifecycle events
│   │   ├── useTaskHistory.ts      # REST-based task list fetching
│   │   └── useToast.ts            # Toast notification state
│   └── lib/
│       ├── config.ts              # Server-side config loader (bmas.yaml)
│       ├── redis.ts               # Redis client singleton (API routes)
│       ├── mappers.ts             # SSE → component data transforms
│       ├── variants.ts            # Coordination variant metadata
│       ├── design-tokens.ts       # Programmatic design token access
│       └── dummy-adapter.tsx      # Development stub adapter
├── package.json
├── tsconfig.json
└── next.config.ts
```

## Real-Time Data Flow

Mission Control uses **Server-Sent Events (SSE)** for all real-time data, not WebSockets or polling:

```
Daemon events (Redis Pub/Sub)
        │
        ▼
  /api/stream/task/{id}    ← Task-scoped SSE (18 event types)
  /api/stream/system       ← System-wide SSE (task lifecycle)
        │
        ▼
  useTaskStream.ts          ← Client hook: rAF batching, REST hydration
        │
        ├──▶ turns[]          → TurnGraph, TurnInspector
        ├──▶ boardEntries[]   → BlackboardBoard, boardModel
        ├──▶ logs[]           → DistributedLogStream
        ├──▶ costEvents[]     → BudgetGauge, CostTracker
        ├──▶ traces[]         → AgentTrace
        ├──▶ narrations[]     → TurnGraph coordinator spine
        ├──▶ files[]          → ArtifactBrowser, AttachmentRail
        └──▶ status           → StatusBadge, TopBar
```

### SSE Event Types (18)

`status`, `phase`, `log`, `cost`, `turn`, `board_entry`, `board_event`, `narration`, `trace`, `debate`, `error`, `hitl`, `file`, `artifact`, `convergence`, `steer`, `approval`, `complete`

## Design System

The UI follows a dark-mode-first design system defined in [DESIGN.md](../docs/design/DESIGN.md):

- **HSL-based surface elevation** — 5 layers, no borders
- **Design tokens** — All colors, spacing, typography, and radii as CSS custom properties in `globals.css`
- **Primitive-first** — All features compose from shared `ui/` primitives
- **Five-state coverage** — Every component: empty, loading, active, error, disabled
- **Agent identity colors** — Dynamically assigned per agent role

> See [docs/design/DESIGN.md](../docs/design/DESIGN.md) for the complete specification.

## Tech Stack

| Category | Technology | Version |
|:---|:---|:---|
| Framework | Next.js (App Router) | 16.2.x |
| React | React | 19.2.x |
| Language | TypeScript | 5.x |
| State | Zustand | 5.x |
| Execution Graph | React Flow (`@xyflow/react`) | 12.x |
| Virtualization | TanStack Virtual (`@tanstack/react-virtual`) | 3.x |
| Markdown | react-markdown + remark-gfm | 10.x |
| Charts | Recharts | 3.x |
| Icons | Lucide React | 1.x |
| Redis Client | redis (Node.js) | 5.x |
| Styling | Vanilla CSS (design tokens) | — |

## Development

```bash
# Install dependencies
npm install

# Start dev server
npm run dev -- -p 9321 --hostname 0.0.0.0

# Lint
npm run lint

# Type check
npx tsc --noEmit

# Build for production
npm run build

# Start production server
npm start
```

Dashboard runs at `http://localhost:9321` (or your control plane IP).

## Documentation

- **[../docs/design/DESIGN.md](../docs/design/DESIGN.md)** — Complete UI design system specification
- **[../docs/architecture/README.md](../docs/architecture/README.md)** — System architecture deep-dive
- **[../examples/stigmergic/CONTEXT.md](../examples/stigmergic/CONTEXT.md)** — Example deployment reference
