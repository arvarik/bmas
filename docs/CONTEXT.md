[🏠 Index](../README.md)

# System Context Document

> [!ABSTRACT] Purpose
> This document is the **comprehensive reference guide** for any human or AI agent working on the bMAS (Blackboard Multi-Agent System) project. It provides the full hardware inventory, network topology, service map, software architecture, and Mission Control UI context needed to understand, debug, or extend the system.
>
> Read this document first before working on any bMAS component.

---

## 1. Project Overview

### What Is bMAS?

bMAS (Blackboard Multi-Agent System) is a **distributed AI swarm** running on a home lab Proxmox cluster. It coordinates multiple LLM-powered agents to decompose, execute, and audit complex tasks through a structured debate-and-consensus workflow.

The system is designed around three principles:
1. **Cost optimization** — Route tasks by complexity. Simple tasks run on local 4B models at $0. Complex tasks escalate to cloud Gemini Pro only when necessary.
2. **Hardware acceleration** — Each edge node has an AMD Radeon iGPU running native Vulkan inference via `llama.cpp`. No Docker, no Ollama, no silent CPU fallbacks.
3. **Observable autonomy** — A real-time Mission Control dashboard provides full swarm visibility: task DAGs, live logs, cost tracking, and human-in-the-loop controls.

### System Architecture Summary

The system has two planes and a control node:

| Plane | Hardware | Purpose |
|:---|:---|:---|
| **Execution Plane** | 3× Lenovo ThinkCentre M75q Gen 5 | Edge inference (GPU) + Agent execution (CPU) |
| **Control Plane** | 1× HP OMEN 16L desktop | Orchestration, state management, routing, dashboard |
| **Storage** | TrueNAS (192.168.4.229) | NFS shares, Beszel Hub telemetry |

---

## 2. Hardware Inventory

### 2.1 Edge Nodes (Execution Plane)

Three identical Lenovo ThinkCentre M75q Gen 5 mini PCs running Proxmox VE 9.x.

| Spec | Value |
|:---|:---|
| **CPU** | AMD Ryzen 7 PRO 8700GE (8C/16T, Zen 4, 35W TDP) |
| **iGPU** | AMD Radeon 780M (12 CUs, RDNA 3, gfx1103) |
| **RAM** | 32 GB DDR5 (26 GB usable after 6 GB UMA reservation) |
| **Storage** | 1 TB NVMe SSD (LVM-Thin provisioned) |
| **Network** | 2.5 GbE (Data Plane: `192.168.4.0/24`) + 1 GbE (Cluster Plane: `10.0.0.0/24`) |

> **Note:** `vulkaninfo` reports "AMD Radeon 760M (RADV PHOENIX)" — this is a [Mesa RADV naming bug](02_execution_plane_provisioning.md). The hardware is confirmed 780M via CPU model (Ryzen 7 PRO 8700GE = 780M) and max GPU clock (2700 MHz).

Each node runs **two LXC containers** for bMAS:

| Container | Role | Resources | IP Pattern |
|:---|:---|:---|:---|
| **LXC A (Inference)** | `llama-server` with Vulkan GPU passthrough | 6 vCPUs, 6 GB RAM, 300 GB disk | `.102`, `.111`, `.121` |
| **LXC B (Agent)** | Hermes Agent framework (native Python venv) | 6 vCPUs, 12 GB RAM, 100 GB disk | `.103`, `.112`, `.122` |

### 2.2 Control Plane (HP OMEN 16L)

| Spec | Value |
|:---|:---|
| **CPU** | Intel Core Ultra 7 265F (20-core) |
| **GPU** | NVIDIA RTX 5060 Ti 16GB (dedicated — triage model + Trebek WhisperX) |
| **RAM** | 32 GB DDR5-5600 |
| **Storage** | 1 TB PCIe Gen4 NVMe M.2 |
| **Network** | 2.5GbE LAN, Wi-Fi 6, BT 5.4 |
| **OS** | Ubuntu 25.10 (bare metal, no Proxmox) |
| **IP** | `192.168.4.240` |
| **User** | `arvarik` |

This machine runs all Control Plane services (Docker + systemd) and will host the Mission Control web UI.

### 2.3 TrueNAS Storage Server (UGREEN DXP480T+)

| Spec | Value |
|:---|:---|
| **CPU** | Intel Core i5-1235U (10-core) |
| **RAM** | 40 GB DDR5 (upgraded) |
| **Storage** | 4× 4TB NVMe M.2 SSDs |
| **Network** | 10GbE LAN, Wi-Fi, 2× Thunderbolt 4 |
| **OS** | TrueNAS 25.10 |
| **IP** | `192.168.4.229` |
| **Roles** | NFS file shares, Beszel Hub (`:8090`), Dockge, Plex, Immich, Vaultwarden |

---

## 3. Network Topology

### 3.1 Complete IP Map

```
192.168.4.0/24 — Data Plane (2.5 GbE)
├── .1           Gateway (router)
├── .100         infra-node1     (LXC — Caddy reverse proxy, *.lab.arvarik.com)
├── .201         pve-node1 (Proxmox host)
├── .202         pve-node2 (Proxmox host)
├── .203         pve-node3 (Proxmox host)
├── .102         inference-node1 (LXC A — llama-server)
├── .103         agent-node1     (LXC B — Hermes Planner)
├── .111         inference-node2 (LXC A — llama-server)
├── .112         agent-node2     (LXC B — Hermes Executor)
├── .121         inference-node3 (LXC A — llama-server)
├── .122         agent-node3     (LXC B — Hermes Auditor)
├── .229         TrueNAS (NFS + Beszel Hub)
└── .240         HP OMEN (Control Plane)

10.0.0.0/24 — Cluster Plane (1 GbE, Proxmox Corosync only)
├── .11          pve-node1
├── .12          pve-node2
└── .13          pve-node3
```

### 3.2 Port Map (HP OMEN — 192.168.4.240)

**bMAS Control Plane Services:**

| Port | Service | Technology | Purpose |
|:---|:---|:---|:---|
| `4000` | LiteLLM Gateway | Docker (`bmas-litellm`) | Unified OpenAI-compatible API router |
| `6379` | Redis Blackboard | Docker (`bmas-redis`) | State store, Streams, Redlock |
| `8001` | Triage Router | Docker (`bmas-triage`) | Qwen3-1.7B complexity classifier on RTX 5060 Ti |
| `9000` | bMAS Daemon | systemd (`bmas-daemon`) | Orchestrator FastAPI (task submission, state, HITL) |
| `9321` | Mission Control | systemd (`mission-control`) | bMAS operations dashboard (Next.js) |

**Infrastructure & Monitoring:**

| Port | Service | Technology | Purpose |
|:---|:---|:---|:---|
| `5001` | Dockge | Docker (`dockge-dockge-1`) | Docker Compose management UI |
| — | Beszel Agent | Docker (`beszel-agent`) | Reports to Beszel Hub on TrueNAS (`192.168.4.229:8090`) |
| `11434` | Ollama API | Native binary (currently **inactive**) | Local LLM inference (legacy, pre-bMAS) |

**Personal Hermes Agent (not part of bMAS):**

The HP OMEN also runs a separate, standalone Hermes agent with its own dashboard, gateway, and WhatsApp bridge. These are independent from the bMAS swarm agents on the edge nodes. All three are managed by systemd and start on boot.

> **Dependency chain:** `hermes-gateway` starts first → `hermes-dashboard` and `hermes-whatsapp` depend on (`After=`) the gateway.

| Port | Service | Technology | Purpose |
|:---|:---|:---|:---|
| — | Hermes Gateway | systemd (`hermes-gateway`) | Core agent runtime (no external port) |
| `9119` | Hermes Dashboard | systemd (`hermes-dashboard`) | Personal Hermes Agent management UI |
| `3000` | WhatsApp Bridge | systemd (`hermes-whatsapp`) | Hermes WhatsApp self-chat bridge (localhost only) |

**Other Personal Apps (not part of bMAS):**

| Port | Service | Technology | Purpose |
|:---|:---|:---|:---|
| `3001` | Euro Events | systemd (`euro-events`) | Euro events tracker dashboard (Next.js) — `~/.hermes/home/opt/data/euro-events` |

### 3.3 Port Map (Edge Nodes — per Inference LXC)

| Port | Service | Technology |
|:---|:---|:---|
| `8080` | `llama-server` | systemd — Gemma 4 E4B via Vulkan |

### 3.4 Port Map (Edge Nodes — per Agent LXC)

| Port | Service | Technology |
|:---|:---|:---|
| `8000` | Hermes API Server | `api_server.py` — task execution + skills endpoint |
| `9119` | Hermes Dashboard | `hermes dashboard` — per-agent management UI |

---

## 4. Service Inventory

### 4.0 Infrastructure Services (CT 100 — `192.168.4.100` on pve-node1)

CT 100 (`infra-node1`) runs the lab-wide reverse proxy and monitoring stack via Docker Compose at `/opt/infra/`.

| Container | Port | Config Location | Purpose |
|:---|:---|:---|:---|
| `caddy` | `80`, `443` | `/opt/infra/docker-compose.yml` | Reverse proxy for `*.lab.arvarik.com` — routes HTTPS to all services across Proxmox and TrueNAS |
| `homepage` | `3000` | `/opt/infra/data/homepage/config/` | Infrastructure dashboard |
| `uptime-kuma` | `3001` | `/opt/infra/data/uptime-kuma/` | 22+ HTTPS monitors |
| `dozzle` | `8080` | `/opt/infra/docker-compose.yml` | Central Docker log aggregator (UI) |
| `watchtower` | — | `/opt/infra/docker-compose.yml` | Auto-updates containers |

**Key Paths:**
- **Caddyfile:** `/opt/infra/Caddyfile` — bind-mounted into the Caddy container at `/etc/caddy/Caddyfile`
- **Docker Compose:** `/opt/infra/docker-compose.yml` — defines all CT 100 services
- **Caddy Dockerfile:** `/opt/infra/caddy/Dockerfile` — custom build with Cloudflare DNS plugin

> **Note:** Caddy has `watchtower.enable=false` — a broken Caddy update takes down HTTPS access to every proxied service. Update Caddy manually with deliberate testing.

### 4.1 Docker Services (HP OMEN)

All managed via Docker Compose with `restart: unless-stopped` or `restart: always`.

| Container | Image | Config Location | Health Check |
|:---|:---|:---|:---|
| `bmas-redis` | `redis:7-alpine` | `/opt/bmas/redis/docker-compose.yml` | `redis-cli ping` |
| `bmas-litellm` | `ghcr.io/berriai/litellm:main-stable` | `/opt/bmas/litellm/docker-compose.yml` | Built-in `/health` |
| `bmas-triage` | `vllm/vllm-openai:latest` | `/opt/bmas/triage/docker-compose.yml` | N/A (manual) |
| `beszel-agent` | `henrygd/beszel-agent-nvidia` | Standalone | N/A |
| `dockge-dockge-1` | `louislam/dockge:1` | Standalone (port `5001`) | Built-in `/` |

### 4.2 Systemd Services (HP OMEN)

**bMAS:**

| Service | ExecStart | WorkingDirectory | User | Dependencies |
|:---|:---|:---|:---|:---|
| `bmas-daemon` | `.venv/bin/uvicorn main:app --port 9000` | `/opt/bmas/daemon/` | root | Redis, LiteLLM |
| `mission-control` | `node .next/standalone/server.js` (port 9321) | `/opt/bmas/mission-control/` | root | `bmas-daemon` |

**Personal Hermes Agent (separate from bMAS):**

| Service | ExecStart | User | Dependencies |
|:---|:---|:---|:---|
| `hermes-gateway` | `python -m hermes_cli.main gateway run` | arvarik | Network |
| `hermes-dashboard` | `hermes dashboard --port 9119` | arvarik | `hermes-gateway` |
| `hermes-whatsapp` | `node whatsapp-bridge/bridge.js --port 3000` | arvarik | `hermes-gateway` |

**Other Personal Apps:**

| Service | ExecStart | User | Dependencies |
|:---|:---|:---|:---|
| `euro-events` | `npx next dev -H 0.0.0.0 -p 3001` | arvarik | Network |

### 4.3 Systemd Services (Per Inference LXC)

| Service | ExecStart |
|:---|:---|
| `llama-server` | `/opt/llama.cpp/build/bin/llama-server` with Vulkan, `-ngl 99`, `--ctx-size 49152`, `--parallel 2` |

### 4.4 Services (Per Agent LXC)

| Process | Port | Path | Description |
|:---|:---|:---|:---|
| Hermes Agent | — | `/root/.hermes/` | Native Python venv — runs the bMAS role persona |
| `api_server.py` | `:8000` | `/opt/bmas/api_server.py` | FastAPI — accepts task execution requests + `/skills` endpoint |
| Hermes Dashboard | `:9119` | — | Per-agent management UI for skills, memory, and configuration |

---

## 5. Software Architecture

### 5.1 The bMAS Workflow

```
User submits task
       │
       ▼
┌─────────────┐
│ bMAS Daemon │  ← Orchestrator on HP OMEN (:9000)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Triage    │  ← Qwen3-1.7B classifies: SIMPLE / LIGHT / MEDIUM / COMPLEX
│   Router    │     (runs on RTX 5060 Ti via vLLM, $0 cost)
└──────┬──────┘
       │
       ├─── SIMPLE ───→ Edge model (Gemma 4 E4B, local, $0)
       ├─── LIGHT ────→ Gemini Flash Lite (cloud, cheapest)
       ├─── MEDIUM ───→ Gemini Flash (cloud, balanced)
       └─── COMPLEX ──→ Gemini Pro (cloud, highest quality)
       │
       ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Planner   │     │  Executor   │     │   Auditor   │
│  (Node 1)   │     │  (Node 2)   │     │  (Node 3)   │
│ Decomposes  │     │ Implements  │     │  Reviews    │
│ into plan   │     │ sub-tasks   │     │  & resolves │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────┬───────┘                   │
                   ▼                           │
            ┌────────────┐                     │
            │  Private   │  ← Debate namespace │
            │ Blackboard │    (agents disagree) │
            └─────┬──────┘                     │
                  │                            │
                  └────────────────────────────┘
                           │
                           ▼
                   ┌──────────────┐
                   │    Public    │  ← Consensus written by Auditor
                   │  Blackboard  │
                   └──────────────┘
```

### 5.2 Redis Blackboard Namespaces

| Namespace | Key Pattern | Purpose |
|:---|:---|:---|
| **Public** | `bmas:public:*` | Consensus state — tasks, results, swarm status |
| **Private** | `bmas:private:{session}:*` | Debate workspace — wiped after consensus |
| **Locks** | `bmas:locks:{resource}` | Redlock distributed locks |
| **Logs** | `bmas:logs:{node_id}` | Redis Streams (durable agent output for SSE) |
| **Metrics** | `bmas:metrics:cost`, `bmas:metrics:tokens` | Per-model cost and token counters |
| **HITL** | `bmas:public:hints:{task_id}` | Operator hints injected during pause |

### 5.3 LiteLLM Model Routing

| LiteLLM Model Name | Backend | Cost | Use Case |
|:---|:---|:---|:---|
| `triage` | Qwen3-1.7B via vLLM (`:8001`) | $0 (local GPU) | Complexity classification |
| `edge-node-1/2/3` | Gemma 4 E4B via llama-server (`:8080`) | $0 (local iGPU) | SIMPLE sub-tasks |
| `light` | Gemini Flash Lite (cloud API) | ~$0.01/1K tokens | LIGHT tasks |
| `medium` | Gemini Flash (cloud API) | ~$0.03/1K tokens | MEDIUM tasks |
| `heavy` | Gemini Pro (cloud API) | ~$0.10/1K tokens | COMPLEX tasks |

### 5.4 Three Inference Pathways

1. **LiteLLM Gateway** (primary) — All agents route through `192.168.4.240:4000`. Centralized cost tracking, retry logic, and model routing.
2. **Edge Models** (local, $0) — `llama-server` on Inference LXCs. 48K context, Gemma 4 E4B, Vulkan-accelerated. Used for SIMPLE-classified sub-tasks.
3. **Gemini CLI** (optional, $0) — Agents invoke `gemini -p` via shell tool. Authenticated via Google AI Ultra OAuth (2,000 req/day). Zero-cost frontier model access.

### 5.5 HITL (Human-in-the-Loop)

The bMAS Daemon supports operator intervention:

1. **Pause**: `POST /hitl/pause` → sets `bmas:public:state.pause = true`
2. **Resume**: `POST /hitl/resume` → clears the pause flag
3. **Inject Hint**: `POST /hitl/inject` → pushes a hint to `bmas:public:hints:{task_id}`
4. **Daemon Behavior**: `_check_pause()` coroutine gates each dispatch step (Planner → Executor → Auditor). When paused, the daemon polls every 500ms. On resume, it pops any injected hints and appends them to the task context.

---

## 6. Mission Control Web UI

### 6.1 What It Is

Mission Control is a **Next.js 15 web application** (App Router, TypeScript) that provides real-time visibility into the bMAS swarm. It runs on the HP OMEN at `http://192.168.4.240:9321`.

It is the operator's single-pane-of-glass for monitoring, debugging, and controlling the AI swarm.

### 6.2 Seven Features

| # | Feature | Data Source | Technology |
|:---|:---|:---|:---|
| 1 | **DAG Visualizer** | `GET /api/state` (polls every 2s) | React Flow (`@xyflow/react`) |
| 2 | **Live Log Streaming** | `GET /api/logs` (SSE, Redis Streams) | xterm.js (`@xterm/xterm`) |
| 3 | **HITL Controls** | `POST /api/hitl/{action}` | ActionButton + Toast notifications |
| 4 | **Blackboard Inspector** | `GET /api/state` + `GET /api/private` | SplitView (Public vs Private) |
| 5 | **Cost Tracker** | `GET /api/cost` (Redis metrics) | Recharts + MetricCard |
| 6 | **Skills Explorer** | `GET /api/skills?node={role}` | Tabbed Panel (per agent) |
| 7 | **Hardware Telemetry** | `GET /api/telemetry` (Beszel Hub) | MetricCard gauges |

### 6.3 Design System

The UI follows an enterprise-grade design system defined in [DESIGN.md](DESIGN.md), inspired by **Notion** (clean hierarchy), **Airbnb** (typography + whitespace), **Linear** (real-time ops), and **Vercel** (dark-mode polish).

Key design principles:
1. **Dark-mode-first** — HSL-based surface elevation hierarchy (5 layers, no borders)
2. **Design token system** — All colors, spacing, typography, and radii are CSS custom properties. Zero hardcoded values.
3. **Primitive-first composition** — All feature components compose from shared `ui/` primitives (Panel, StatusBadge, MetricCard, ActionButton, etc.)
4. **Five-state coverage** — Every component implements: empty, loading, active, error, and disabled states
5. **Agent identity colors** — Planner (purple), Executor (teal), Auditor (amber) — consistent across DAG nodes, terminal headers, log prefixes, and chart segments

### 6.4 API Routes (Next.js Backend)

All 7 Next.js API routes act as proxies to backend services, avoiding CORS issues:

| Route | Method | Backend Target | Purpose |
|:---|:---|:---|:---|
| `/api/state` | GET | bMAS Daemon `:9000/state` | Blackboard public state |
| `/api/logs` | GET | Redis Streams (direct) | SSE endpoint — tails 3 log streams |
| `/api/hitl` | POST/GET | bMAS Daemon `:9000/hitl/*` | Pause/Resume/Inject/Status |
| `/api/private` | GET | Redis (direct SCAN) | Private namespace data |
| `/api/cost` | GET | Redis `HGETALL` | Per-model cost + token counters |
| `/api/skills` | GET/DELETE | Agent LXCs `:8000/skills` | Hermes skills proxy |
| `/api/submit` | POST | bMAS Daemon `:9000/submit` | Task submission |

### 6.5 Log Streaming Architecture

Logs use **Server-Sent Events (SSE)** — not WebSockets. This is a deliberate architectural decision:

- SSE is unidirectional (server → client), which matches the log streaming pattern
- The browser's `EventSource` API handles automatic reconnection natively
- `Last-Event-ID` enables catch-up after disconnects (Redis Stream IDs)
- No custom WebSocket server needed — works natively in Next.js App Router

The `/api/logs` endpoint uses a **module-level singleton Redis subscriber** to prevent connection pool exhaustion. All connected browser tabs share the same subscriber, which fans out messages to each SSE response stream.

### 6.6 Layout Architecture

```
┌──────────────────────────────────────────────────┐
│  Top Bar (48px)                                  │
│  [Mission Control]  [Swarm Status]  [Cost Ticker]│
├─────────┬────────────────────────────────────────┤
│ Sidebar │  Main Content Area                     │
│ (240px) │  ┌─────────────┬──────────────────┐    │
│         │  │  DAG Panel  │  HITL + Status   │    │
│ • DAG   │  │  (60%)      │  (40%)           │    │
│ • Logs  │  ├─────────────┴──────────────────┤    │
│ • Board │  │  Log Terminals (3 columns)     │    │
│ • Cost  │  │  [Planner]  [Executor] [Auditor]│   │
│ • Nodes │  ├─────────────┬──────────────────┤    │
│ • Skills│  │  Blackboard │  Cost + Telemetry│    │
│         │  │  Inspector  │  Metrics         │    │
│         │  └─────────────┴──────────────────┘    │
└─────────┴────────────────────────────────────────┘
```

- **Sidebar**: Collapsible (240px → 48px icon-only). Three sections: Operations, Intelligence, Infrastructure.
- **Focused view**: Clicking a nav item expands that panel to fill the main area. `Esc` returns to dashboard.
- **Responsive**: Sidebar auto-collapses at <1440px. Single-column stacked layout at <1024px.

---

## 7. Build & Implementation Phases

The project is documented as a **6-phase deployment guide** with a **7-phase AI prompt sequence** for constructing Mission Control:

### Deployment Phases

| Phase | Document | Builds |
|:---|:---|:---|
| 1 | [Hardware & Hypervisor Prep](01_hardware_and_hypervisor_prep.md) | BIOS UMA, 6 LXCs, base packages |
| 2 | [Execution Plane Provisioning](02_execution_plane_provisioning.md) | GPU passthrough, Hermes native install |
| 3 | [Edge Model Hosting](03_edge_model_hosting.md) | `llama.cpp` Vulkan build, Gemma 4 E4B, systemd |
| 4 | [Control Plane & Triage Router](04_control_plane_and_triage_router.md) | Redis, LiteLLM, Qwen3-1.7B triage, semantic router |
| 5 | [bMAS Python Daemon](05_bmas_python_daemon.md) | Orchestrator, Blackboard client, HITL, personas |
| 6 | [Mission Control Web UI](06_mission_control_web_ui.md) | Next.js dashboard, SSE, React Flow, design system |

### Mission Control Prompt Phases (§6.4)

| Prompt | Focus | Output |
|:---|:---|:---|
| **Phase 0** | System context + constitutional constraints | Context lock |
| **Phase 1** | Backend API routes (7 files) | Redis client, SSE, proxies |
| **Phase 2** | State management + DAG visualizer (2 files) | Zustand store, React Flow |
| **Phase 3** | Terminal logging (1 file) | xterm.js + SSE consumer |
| **Phase 4** | Feature components (6 files) | HITL, Cost, Skills, Telemetry, Blackboard, page assembly |
| **Phase 5** | Design system foundation (12 files) | Tokens, primitives, layout shell, toast system |
| **Phase 6** | Visual polish & interaction states (9 files) | Refactored components, animations, responsive, keyboard shortcuts |

---

## 8. Key Design Decisions

| Decision | Rationale |
|:---|:---|
| **Native `llama.cpp` over Ollama** | Eliminates Docker-in-LXC overhead and silent CPU fallback. Direct Vulkan gives verified GPU utilization. |
| **Hermes Agent over CrewAI** | Persistent Skills memory, self-improvement loop, model-agnostic, no Docker dependency. |
| **LiteLLM as unified gateway** | Single OpenAI-compatible interface for all models. Centralized cost tracking, retry logic, routing. |
| **Redis Blackboard over filesystem** | Atomic Redlock operations, Redis Streams for durable logs, namespace separation for debate. |
| **SSE over WebSockets** | Unidirectional log streaming matches SSE perfectly. Native `EventSource` reconnection + `Last-Event-ID`. No custom server needed. |
| **Design token system** | CSS custom properties ensure visual consistency across all components. One design change propagates everywhere. |
| **6 GB UMA allocation** | Maximum available in Lenovo BIOS (no 8G option). Fits Gemma 4 E4B Q4_K_M (~2.8 GB weights + ~1.7 GB KV cache + driver overhead). |
| **Triage at $0** | Qwen3-1.7B runs on the local RTX 5060 Ti via vLLM. Classifies task complexity before any paid API call is made. |

---

## 9. Quick Reference: Health Check Commands

### From your MacBook (external)

```bash
# Check all HP OMEN services
ssh arvarik@192.168.4.240 'echo "=== Systemd ===" && \
  systemctl is-active hermes-gateway hermes-dashboard hermes-whatsapp 2>/dev/null | paste - - - -d" " && \
  echo "" && echo "=== Docker ===" && \
  docker ps --format "{{.Names}}: {{.Status}}" && \
  echo "" && echo "=== Ports ===" && \
  ss -tlnp | grep -E ":(4000|6379|8001|9000|9119|9321|3000|11434)\b"'
```

### Check edge nodes

```bash
# Verify llama-server on all 3 Inference LXCs
for ip in 192.168.4.102 192.168.4.111 192.168.4.121; do
  echo "=== $ip ==="
  curl -s --connect-timeout 2 http://$ip:8080/health 2>/dev/null || echo "UNREACHABLE"
done

# Verify Hermes agents on all 3 Agent LXCs
for ip in 192.168.4.103 192.168.4.112 192.168.4.122; do
  echo "=== $ip ==="
  curl -s --connect-timeout 2 http://$ip:8000/health 2>/dev/null || echo "UNREACHABLE"
done
```

---
