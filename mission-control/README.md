# Mission Control — bMAS Dashboard

The real-time operations dashboard for the bMAS (Blackboard Multi-Agent System). Built with Next.js 16, React 19, and Zustand.

## Features

- **Task Submission** — Submit tasks to the swarm directly from the dashboard
- **Task DAG** — Visualize task decomposition and execution flow (React Flow)
- **Live Log Terminals** — Per-agent log streams via SSE + Redis Streams (xterm.js)
- **Blackboard Inspector** — View public consensus and private debate state
- **HITL Controls** — Pause/resume the swarm, inject operator hints
- **Cost & Tokens** — Live spend tracking across local and cloud models
- **Infrastructure Telemetry** — CPU/RAM gauges from Beszel Hub
- **Skills Explorer** — View learned agent skills per node

## Development

```bash
npm install
npm run dev -- -p 9321 --hostname 0.0.0.0
```

Dashboard runs at `http://192.168.4.240:9321`.

## Architecture

- **API Routes** (`src/app/api/`) — Server-side proxies to bMAS Daemon (:9000), Redis, and Beszel Hub
- **Components** (`src/components/`) — 7 feature panels + `ui/` design system primitives
- **State** (`src/hooks/useBlackboard.ts`) — Zustand store with 2s polling of daemon `/state`
- **Styling** — Vanilla CSS with design tokens defined in `globals.css` and `DESIGN.md`

## Documentation

- **[DESIGN.md](DESIGN.md)** — UI design system specification (colors, typography, components)
- **[CONTEXT.md](../docs/CONTEXT.md)** — Full system reference (hardware, network, services)
