<p align="center">
  <img src="mission-control/public/ant-head.png" alt="Stigmergic Swarm Logo" width="128" height="128" />
</p>

<h1 align="center">Stigmergic</h1>

<p align="center">
  <strong>Biomimetic Multi-Agent Swarm (bMAS) Orchestration System</strong>
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0" /></a>
  <a href="https://github.com/arvarik/bmas/actions/workflows/ci.yml"><img src="https://github.com/arvarik/bmas/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white" alt="Python 3.13+" /></a>
  <a href="https://typescriptlang.org"><img src="https://img.shields.io/badge/TypeScript-6.x-3178C6?logo=typescript&logoColor=white" alt="TypeScript 6.x" /></a>
  <a href="https://nextjs.org"><img src="https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white" alt="Next.js 16" /></a>
  <a href="docker-compose.yml"><img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker" /></a>
  <a href="redis/"><img src="https://img.shields.io/badge/Redis-7--alpine-DC382D?logo=redis&logoColor=white" alt="Redis" /></a>
  <a href="litellm/"><img src="https://img.shields.io/badge/LiteLLM-Gateway-orange" alt="LiteLLM" /></a>
  <a href="triage/"><img src="https://img.shields.io/badge/vLLM-Triage-purple" alt="vLLM" /></a>
</p>

**A distributed AI swarm built on the Blackboard Multi-Agent System (bMAS) architecture.** Stigmergic coordinates multiple LLM-powered agents through a shared blackboard with an LLM-driven Control Unit that dynamically selects agents per round — achieving structured, multi-round debate and consensus.

> *Named after [stigmergy](https://en.wikipedia.org/wiki/Stigmergy) — the mechanism by which individual agents coordinate through shared environmental signals (the blackboard) rather than direct communication.*

## Features

- **Cyclic blackboard orchestration** — Control Unit selects agents per round; agents read/write a shared board; rounds repeat until convergence (not a fixed pipeline)
- **Dynamic agent roles** — Planner, Critic, Conflict Resolver, Cleaner, Decider, and domain-specific Experts — all discovered and activated dynamically
- **Multi-provider model routing** — Route tasks to Gemini, Claude, OpenAI, or local models based on complexity tiers
- **Intelligent triage** — Local Qwen3-1.7B classifier automatically routes tasks to the cheapest capable model
- **Execution graph** — Swimlane visualization of agent turns grouped by round, with coordinator routing rationale
- **Structured distributed logging** — Per-agent log streams with full structured payloads, level/agent filtering, and detail drawers
- **Blackboard command center** — Timeline, thread, and graph views of board entries with salience heat, debate threading, and type/author grouping
- **Mission cockpit** — Live 4-panel command center: blackboard graph, agent mind cards, global firehose, convergence strip
- **Human-in-the-loop** — Pause/resume at round boundaries, inject directives, abort tasks, approve/reject agent actions
- **Real-time cost tracking** — Per-model token usage and USD cost with budget ceilings and live gauges
- **Single config file** — Define your entire deployment in `bmas.yaml`
- **Docker-first** — `docker compose up` and you're running

## Architecture

```
┌─────────────────── Control Plane (Docker Compose) ───────────────────┐
│                                                                       │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌────────┐  ┌───────────┐ │
│  │  Redis   │  │ LiteLLM  │  │ Triage  │  │ Daemon │  │ Mission   │ │
│  │ Board    │  │ Gateway  │  │ (GPU)   │  │ Orch.  │  │ Control   │ │
│  │ :6379    │  │ :4000    │  │ :8001   │  │ :9000  │  │ :9321     │ │
│  └─────────┘  └──────────┘  └─────────┘  └────────┘  └───────────┘ │
└───────────────────────────────────────────────────────────────────────┘
         │              │                        │
         │       ┌──────┴──────┐          ┌──────┴──────┐
         │       │ Cloud APIs  │          │ Agent Nodes │
         │       │ Gemini/etc  │          │ Hermes+SSE  │
         │       └─────────────┘          └─────────────┘

               ┌─────── Per Round ─────────┐
               │                            │
               │   Control Unit selects     │
               │   agents from board state  │
               │         │                  │
               │    ┌────┴────┐             │
               │    │ Agents  │ read/write  │
               │    │ execute │ the board   │
               │    └────┬────┘             │
               │         │                  │
               │   Board updated;           │
               │   loop until convergence   │
               └────────────────────────────┘
```

## Quick Start

```bash
git clone https://github.com/arvarik/bmas.git
cd bmas

# Configure
cp bmas.example.yaml bmas.yaml    # edit with your IPs and settings
cp .env.example .env              # fill in secrets (API keys, passwords)

# Start
docker compose up -d              # without GPU
docker compose --profile gpu up -d  # with GPU (enables triage)

# Open Mission Control
open http://localhost:9321
```

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for the full guide.

## Configuration

Everything is configured through a single `bmas.yaml` file:

```yaml
project:
  name: "My Swarm"

control_plane:
  host: "localhost"
  ports: { redis: 6379, litellm: 4000, daemon: 9000, dashboard: 9321 }

coordination:
  variant: traditional              # traditional | stigmergic
  traditional:
    max_rounds: 8
    budget_ceiling_usd: 1.00
    cu_mode: llm                    # llm | heuristic
    coordinator_narration: true

nodes:
  - name: "node-1"
    host: "192.168.1.101"
    port: 8000
    role: planner
    inference: { host: "192.168.1.102", port: 8080, model: "gemma-4-e4b" }

models:
  gemini-pro: { provider: gemini, model: "gemini-3.1-pro-preview", api_key_env: GEMINI_API_KEY }

routing:
  complex: gemini-pro
  medium: gemini-pro
  light: gemini-pro
  simple: local
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference and [examples/](examples/) for sample configs.

## Repository Structure

```
bmas/
├── bmas.example.yaml      # Reference configuration
├── .env.example           # Secrets template
├── docker-compose.yml     # Unified control plane (5 services)
├── docker-compose.dev.yml # Dev overrides (hot-reload, volume mounts)
│
├── agent/                 # Edge node agent API (deployed to LXCs)
│   ├── api_server.py      #   Hermes ↔ Daemon bridge (Runs API + CLI fallback)
│   ├── profiles/          #   Per-role Hermes profiles (planner, critic, expert, ...)
│   └── tests/             #   SSE parser + event translation tests
│
├── daemon/                # Python FastAPI orchestrator
│   └── src/
│       ├── app.py         #   API entry point (:9000)
│       ├── config.py      #   Loads bmas.yaml at startup
│       ├── database.py    #   SQLite persistence (v2 schema, 12 tables)
│       ├── auth.py        #   Bearer token authentication for node ingest
│       ├── core/
│       │   ├── orchestrator.py   # Task lifecycle + multi-round dispatch
│       │   ├── blackboard.py     # Redis state + durable board snapshots
│       │   ├── board_store.py    # Event-sourced board (entries + events)
│       │   ├── gateway.py        # Capability-gated agent dispatch
│       │   ├── entry.py          # Board entry schema + validation
│       │   ├── protocol.py       # Agent ↔ Daemon message protocol
│       │   ├── salience.py       # Entry salience scoring
│       │   ├── event_emitter.py  # SSE event abstraction
│       │   ├── log_levels.py     # Level normalization (INF→info, etc.)
│       │   ├── triage.py         # Complexity classification client
│       │   └── variants/
│       │       └── traditional.py  # Cyclic CU → agents → board loop
│       └── routes/
│           ├── submit.py         # POST /submit
│           ├── tasks.py          # GET /tasks, /tasks/{id}/*
│           ├── events.py         # SSE endpoints (task + system)
│           ├── ingest.py         # POST /ingest/traces, /ingest/logs
│           ├── artifacts.py      # GET /tasks/{id}/artifacts
│           ├── files.py          # GET /tasks/{id}/files
│           ├── hitl.py           # POST /tasks/{id}/pause|resume|directive
│           └── health.py         # GET /health
│
├── mission-control/       # Next.js 16 dashboard (:9321)
│   └── src/
│       ├── app/
│       │   ├── page.tsx           # Landing page — task list + submit
│       │   ├── task/[taskId]/     # Task-scoped pages (7 tabs)
│       │   │   ├── page.tsx       #   Overview — process summary + result
│       │   │   ├── dag/           #   Execution Graph — TurnGraph (swimlane)
│       │   │   ├── logs/          #   Distributed Log Stream + Agent Trace
│       │   │   ├── blackboard/    #   Blackboard Board command center
│       │   │   ├── mission/       #   Mission Cockpit (4-panel live view)
│       │   │   └── artifacts/     #   Artifact browser + downloads
│       │   └── api/               # 20+ API route handlers (server-side proxies)
│       ├── components/
│       │   ├── features/          # 18 feature components
│       │   │   ├── TurnGraph.tsx       # Swimlane execution graph (React Flow)
│       │   │   ├── DistributedLogStream.tsx  # Unified chronological logs
│       │   │   ├── BlackboardBoard.tsx # Board command center
│       │   │   ├── AgentTrace.tsx      # Structured turn trace viewer
│       │   │   └── ...                 # + 14 more feature components
│       │   ├── ui/                # Design system primitives (9 components)
│       │   └── layout/            # App shell (TopBar)
│       ├── hooks/
│       │   ├── useTaskStream.ts   # SSE hook — 18 event types, rAF batching
│       │   ├── useSystemStream.ts # System-level SSE (task lifecycle)
│       │   └── useTaskHistory.ts  # REST task list fetching
│       └── lib/
│           ├── config.ts          # Server-side config loader
│           ├── mappers.ts         # SSE → component data transforms
│           └── design-tokens.ts   # Programmatic design token access
│
├── litellm/               # LiteLLM model gateway (:4000)
├── redis/                 # Redis blackboard (:6379)
├── triage/                # Complexity classifier (:8001)
│
├── docs/                  # Documentation
│   ├── QUICKSTART.md      #   Get started in 5 minutes
│   ├── CONFIGURATION.md   #   Full config reference
│   ├── NODE_SETUP.md      #   Edge node provisioning guide
│   ├── architecture/      #   System architecture deep-dive
│   └── design/            #   Mission Control UI specification
│
├── scripts/               # Operational utilities
│   ├── check-ci.sh        #   Local CI mirror (ruff + mypy + pytest + eslint + tsc + build)
│   ├── deploy_profiles.sh #   Deploy Hermes profiles to agent nodes
│   └── healthcheck.sh     #   Post-deploy service health check
│
└── examples/              # Example configurations
    ├── stigmergic/        #   Reference deployment (3-node homelab)
    ├── minimal-cloud.yaml #   Cloud-only, no GPU required
    └── multi-provider.yaml#   Gemini + Claude + OpenAI routing
```

## Documentation

| Document | Description |
|:---|:---|
| [Architecture](docs/architecture/README.md) | System architecture & component deep-dive |
| [Quick Start](docs/QUICKSTART.md) | Get running in 5 minutes |
| [Configuration](docs/CONFIGURATION.md) | Full `bmas.yaml` reference |
| [Node Setup](docs/NODE_SETUP.md) | Provisioning edge nodes |
| [Design System](docs/design/DESIGN.md) | Mission Control UI specification |

### Component READMEs

| Component | README |
|:---|:---|
| Agent | [agent/README.md](agent/README.md) |
| Daemon | [daemon/README.md](daemon/README.md) |
| Mission Control | [mission-control/README.md](mission-control/README.md) |
| LiteLLM | [litellm/README.md](litellm/README.md) |
| Redis | [redis/README.md](redis/README.md) |
| Triage | [triage/README.md](triage/README.md) |

## Paper

This project is an implementation of the Blackboard Multi-Agent System (bMAS) architecture proposed in:

> **Han, B. & Zhang, S. (2025).** *Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture.*
> [arXiv:2507.01701](https://arxiv.org/abs/2507.01701)

The paper introduces a framework where LLM agents coordinate through a shared blackboard with an LLM-driven control unit that dynamically selects agents per round — achieving competitive performance with state-of-the-art multi-agent systems while consuming fewer tokens. Stigmergic implements this architecture with the **traditional** coordination variant.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).
