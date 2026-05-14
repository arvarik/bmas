# bMAS — Blackboard Multi-Agent System

A **distributed AI swarm** running on a home lab Proxmox cluster. bMAS coordinates multiple LLM-powered agents to decompose, execute, and audit complex tasks through a structured debate-and-consensus workflow.

## Architecture

![bMAS AI Strategy Topology](docs/ai-topology/ai-strategy-topology.png)

## Repository Structure

```
bmas/
├── daemon/            # Python FastAPI orchestrator
│   ├── main.py        #   API entry point (:9000)
│   ├── orchestrator.py#   Task lifecycle & agent dispatch
│   ├── blackboard.py  #   Redis state management
│   ├── triage_router.py#  Complexity classification
│   └── personas.py    #   Agent role definitions
├── mission-control/   # Next.js 16 dashboard (:9321)
│   ├── src/app/       #   App Router pages & API routes
│   ├── src/components/#   React components (7 panels)
│   └── src/hooks/     #   Zustand stores & custom hooks
├── litellm/           # LiteLLM Gateway config (Docker, :4000)
├── redis/             # Redis Blackboard config (Docker, :6379)
├── triage/            # Triage Router config & tests (Docker, :8001)
└── docs/              # System documentation
    └── CONTEXT.md     #   Full hardware/network/service reference
```

## Quick Start

All services are managed by systemd or Docker and start on boot. To check status:

```bash
# bMAS services
systemctl is-active bmas-daemon          # Orchestrator (:9000)
docker ps --filter name=bmas             # Redis, LiteLLM, Triage

# Mission Control (dev server)
# Currently runs as dev server, not a systemd service
cd /opt/bmas/mission-control && npm run dev -- -p 9321 --hostname 0.0.0.0

# Agent health (on edge LXCs)
for ip in 192.168.4.103 192.168.4.112 192.168.4.122; do
  curl -s http://$ip:8000/health
done
```

## Port Map (HP OMEN — 192.168.4.240)

| Port | Service | Purpose |
|------|---------|---------|
| `4000` | LiteLLM | OpenAI-compatible API router |
| `6379` | Redis | Blackboard state store |
| `8001` | Triage | Complexity classifier |
| `9000` | bMAS Daemon | Orchestrator API |
| `9321` | Mission Control | Dashboard UI |

> See [docs/CONTEXT.md](docs/CONTEXT.md) for the full hardware inventory, network topology, and service reference.

## Documentation

- **[CONTEXT.md](docs/CONTEXT.md)** — Comprehensive system reference (hardware, network, services, architecture)
- **[DESIGN.md](mission-control/DESIGN.md)** — Mission Control UI design system specification
