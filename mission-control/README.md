# Mission Control — Stigmergic Dashboard

The real-time operations dashboard for **Stigmergic**. A single-pane-of-glass for monitoring, debugging, and controlling the AI swarm. Built with Next.js 16, React 19, TypeScript, and vanilla CSS.

> Runs as Docker container `bmas-dashboard` on the control plane at port 9321.

## Features

| # | Feature | Technology | Data Source |
|:---|:---|:---|:---|
| 1 | **Task DAG Visualizer** | React Flow (`@xyflow/react`) | `GET /api/state` (2s polling) |
| 2 | **Live Log Terminals** | xterm.js (`@xterm/xterm`) | `GET /api/logs` (SSE, Redis Streams) |
| 3 | **HITL Controls** | ActionButton + Toast | `POST /api/hitl/{action}` |
| 4 | **Blackboard Inspector** | SplitView (Public vs Private) | `GET /api/state` + `GET /api/private` |
| 5 | **Cost Tracker** | Recharts + MetricCard | `GET /api/cost` (Redis metrics) |
| 6 | **Skills Explorer** | Tabbed Panel (per agent) | `GET /api/skills?node={role}` |
| 7 | **Hardware Telemetry** | MetricCard gauges | `GET /api/telemetry` (Beszel Hub) |

## Architecture

```
Browser (React 19)
    │
    ▼
┌────────────────────────────────────────────────────────────┐
│  Next.js 16 App Router                                     │
│                                                            │
│  ┌──────────────────────┐  ┌────────────────────────────┐  │
│  │ src/app/api/         │  │ src/components/            │  │
│  │                      │  │                            │  │
│  │  /api/state   ──────▶│  │  TopBar                   │  │
│  │  /api/logs    ──────▶│  │  DAGVisualizer            │  │
│  │  /api/hitl    ──────▶│  │  LogTerminal (×3)         │  │
│  │  /api/private ──────▶│  │  HITLControls             │  │
│  │  /api/cost    ──────▶│  │  BlackboardInspector      │  │
│  │  /api/skills  ──────▶│  │  CostTracker              │  │
│  │  /api/submit  ──────▶│  │  SkillsExplorer           │  │
│  │  /api/telemetry ────▶│  │  Telemetry                │  │
│  └──────────────────────┘  └────────────────────────────┘  │
│             │                                              │
└─────────────┼──────────────────────────────────────────────┘
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
│   │   ├── layout.tsx         # Root layout — fonts, metadata, AppShell
│   │   ├── page.tsx           # Dashboard grid — assembles all 7 features
│   │   ├── AppShell.tsx       # Sidebar + TopBar + main content container
│   │   ├── globals.css        # Design tokens (CSS custom properties)
│   │   └── api/               # 8 API route handlers (server-side proxies)
│   │       ├── state/         #   → Daemon :9000/state
│   │       ├── logs/          #   → Redis Streams (SSE singleton)
│   │       ├── hitl/          #   → Daemon :9000/hitl/*
│   │       ├── private/       #   → Redis SCAN bmas:private:*
│   │       ├── cost/          #   → Redis HGETALL bmas:metrics:*
│   │       ├── skills/        #   → Agent LXCs :8000/skills
│   │       ├── submit/        #   → Daemon :9000/submit
│   │       └── telemetry/     #   → Beszel Hub :8090
│   ├── components/
│   │   ├── ui/                # Design system primitives (10 components)
│   │   │   ├── Panel.tsx      #   Container with header/body/states
│   │   │   ├── StatusBadge.tsx#   Status pill (pending/running/success/error/paused)
│   │   │   ├── MetricCard.tsx #   Numeric display with trend indicator
│   │   │   ├── TerminalPane.tsx#  xterm.js wrapper with agent identity
│   │   │   ├── SplitView.tsx  #   Side-by-side panel layout
│   │   │   ├── ActionButton.tsx#  Primary/Secondary/Danger button variants
│   │   │   ├── Skeleton.tsx   #   Loading placeholder with shimmer
│   │   │   ├── EmptyState.tsx #   No-data placeholder with icon + message
│   │   │   ├── Toast.tsx      #   Notification system (bottom-right stack)
│   │   │   └── Sidebar.tsx    #   Collapsible navigation (240px → 48px)
│   │   ├── TopBar.tsx         # App header — swarm status + cost ticker
│   │   ├── DAGVisualizer.tsx  # Feature: task dependency graph
│   │   ├── LogTerminal.tsx    # Feature: per-agent log stream
│   │   ├── HITLControls.tsx   # Feature: pause/resume/inject hint
│   │   ├── BlackboardInspector.tsx # Feature: public + private state
│   │   ├── CostTracker.tsx    # Feature: spend + token charts
│   │   ├── SkillsExplorer.tsx # Feature: agent skills per node
│   │   └── Telemetry.tsx      # Feature: hardware health gauges
│   ├── hooks/
│   │   ├── useBlackboard.ts   # Zustand store — polls /api/state every 2s
│   │   └── useToast.ts        # Toast notification state
│   └── lib/
│       ├── redis.ts           # Redis client singleton (used by API routes)
│       └── design-tokens.ts   # Programmatic access to design tokens
├── DESIGN.md                  # UI design system specification
├── package.json
├── tsconfig.json
└── next.config.ts
```

## Design System

The UI follows an enterprise-grade design system defined in [DESIGN.md](DESIGN.md):

- **Dark-mode-first** — HSL-based surface elevation hierarchy (5 layers, no borders)
- **Design tokens** — All colors, spacing, typography, and radii are CSS custom properties in `globals.css`
- **Primitive-first** — All features compose from shared `ui/` primitives
- **Five-state coverage** — Every component: empty, loading, active, error, disabled
- **Agent identity colors** — Planner (purple), Executor (teal), Auditor (amber)

> See [DESIGN.md](DESIGN.md) for the complete specification.

## API Routes

All API routes are server-side proxies that forward requests to backend services, avoiding CORS issues:

| Route | Method | Backend | Purpose |
|:---|:---|:---|:---|
| `/api/state` | GET | Daemon `:9000/state` | Blackboard public state + agent health |
| `/api/logs` | GET | Redis Streams (direct) | SSE endpoint — tails 3 log streams |
| `/api/hitl` | POST/GET | Daemon `:9000/hitl/*` | Pause/Resume/Inject/Status |
| `/api/private` | GET | Redis (`SCAN`) | Private namespace debate data |
| `/api/cost` | GET | Redis (`HGETALL`) | Per-model cost + token counters |
| `/api/skills` | GET/DELETE | Agent LXCs `:8000/skills` | Hermes skills proxy |
| `/api/submit` | POST | Daemon `:9000/submit` | Task submission |
| `/api/telemetry` | GET | Beszel Hub `:8090` | Hardware metrics |

### Log Streaming (SSE)

Logs use **Server-Sent Events**, not WebSockets. The `/api/logs` endpoint uses a module-level singleton Redis subscriber to prevent connection pool exhaustion. All connected browser tabs share the same subscriber.

## Tech Stack

| Category | Technology | Version |
|:---|:---|:---|
| Framework | Next.js (App Router) | 16.2.x |
| React | React | 19.2.x |
| Language | TypeScript | 5.x |
| State | Zustand | 5.x |
| DAG Visualization | React Flow (`@xyflow/react`) | 12.x |
| Terminal | xterm.js (`@xterm/xterm`) | 6.x |
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

# Build for production
npm run build

# Start production server
npm start
```

Dashboard runs at `http://localhost:9321` (or your control plane IP).

## Documentation

- **[DESIGN.md](DESIGN.md)** — Complete UI design system specification
- **[../docs/CONTEXT.md](../docs/CONTEXT.md)** — Full system reference (hardware, network, services)
