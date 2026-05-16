# Hermes Dashboard API Reference

> **Version**: Hermes Agent v0.13.0 — "The Tenacity Release" (2026.5.7)  
> **Primary Source**: [Official Web Dashboard Docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)  
> **Secondary Source**: Audited from `hermes_cli/web_server.py` on agent nodes  
> **Dashboard Port**: `9119` (default) — FastAPI + Uvicorn backend, React 19 SPA frontend  
> **Gateway Port**: `8642` (default) — OpenAI-compatible API server (separate process)  
> **bMAS Context**: Each edge LXC runs a Hermes Dashboard accessible at `http://<node_ip>:9119`  
> **Machine-readable docs**: [`/llms.txt`](https://hermes-agent.nousresearch.com/docs/assets/files/llms-bcf65f79b33e57e6c0cce5b9627945d4.txt) (~17 KB index) · [`/llms-full.txt`](https://hermes-agent.nousresearch.com/docs/assets/files/llms-full-bab654a7749469685f2837d994ab04e9.txt) (~1.8 MB full)

> [!IMPORTANT]
> **Two APIs, two ports.** This document covers **both** the Dashboard API (`:9119`, management UI backend) and the Gateway API Server (`:8642`, OpenAI-compatible inference). They use different auth mechanisms and serve different purposes. Don't confuse them.

---

## Authentication

The Hermes Dashboard uses **ephemeral session tokens** for all non-public API endpoints. The token is generated once per process start using `secrets.token_urlsafe(32)` and is embedded in the dashboard HTML.

### How to Obtain a Token

The token is injected into the root HTML page as a JavaScript global:

```html
<script>window.__HERMES_SESSION_TOKEN__="<token>";...</script>
```

**Programmatic extraction:**

```bash
TOKEN=$(curl -s http://<node_ip>:9119/ \
  | grep -oP '__HERMES_SESSION_TOKEN__="[^"]+' \
  | cut -d'"' -f2)
```

### How to Authenticate

Send the token via the **`X-Hermes-Session-Token`** header (preferred) or the legacy `Authorization: Bearer <token>` header:

```bash
curl -H "X-Hermes-Session-Token: $TOKEN" http://<node_ip>:9119/api/skills
```

### Public Endpoints (No Token Required)

These read-only endpoints skip authentication:

| Endpoint | Description |
|:--|:--|
| `GET /api/status` | Agent version, home dir, gateway status, platform states, active session count |
| `GET /api/config/defaults` | Default configuration values |
| `GET /api/config/schema` | Config field schema, types, categories, and select options per field |
| `GET /api/model/info` | Current model, provider, context length, capabilities |
| `GET /api/dashboard/themes` | Available dashboard themes |
| `GET /api/dashboard/plugins` | Installed dashboard plugins |

> [!WARNING]
> `/api/status` may block for several seconds if it's checking gateway health. Use a connect timeout.

### CORS Restrictions

The dashboard restricts CORS to localhost origins only:
- `http://localhost:9119` / `http://127.0.0.1:9119` (production)
- `http://localhost:3000` / `http://127.0.0.1:3000` (Next.js dev)
- `http://localhost:5173` / `http://127.0.0.1:5173` (Vite dev)

Custom port origins are added automatically. **bMAS bypasses CORS entirely** because Mission Control makes server-side requests from Next.js API routes, not browser-side requests.

### bMAS Integration Pattern

In Mission Control, we scrape the token from the HTML page on each request. Since the token is per-process and the dashboard rarely restarts, this is effectively a one-time auth handshake per session. See [`mission-control/src/app/api/skills/route.ts`](../mission-control/src/app/api/skills/route.ts) for the reference implementation.

---

## Endpoint Reference

### Skills

Skills are Hermes's procedural memory — Markdown files with YAML frontmatter stored in `~/.hermes/skills/`. Each skill teaches the agent a reusable procedure.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/skills` | ✅ | List all installed skills |
| `PUT` | `/api/skills/toggle` | ✅ | Enable/disable a skill |

**`GET /api/skills`** — Response:
```json
[
  {
    "name": "architecture-diagram",
    "description": "Dark-themed SVG architecture/cloud/infra diagrams as HTML.",
    "category": "creative",
    "enabled": true
  }
]
```

**`PUT /api/skills/toggle`** — Body: `{ "name": "skill-name", "enabled": false }`

**bMAS usage**: Already integrated into the Skills Explorer page. Mission Control proxies through `/api/skills?node=<role>`.

---

### Sessions

Hermes persists all chat sessions in a local SQLite database with FTS5 full-text search.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/sessions?limit=N` | ✅ | List recent sessions |
| `GET` | `/api/sessions/search?q=&limit=N` | ✅ | Full-text search across all session messages |
| `GET` | `/api/sessions/{id}` | ✅ | Get session metadata |
| `GET` | `/api/sessions/{id}/messages` | ✅ | Get full message history for a session |
| `GET` | `/api/sessions/{id}/latest-descendant` | ✅ | Follow fork chain to the latest active session |
| `DELETE` | `/api/sessions/{id}` | ✅ | Delete a session |

**`GET /api/sessions/search?q=redis+timeout`** — Response:
```json
{
  "results": [
    {
      "session_id": "abc123",
      "snippet": "...ETIMEDOUT: connect ETIMEDOUT 192.168.4.240:6379...",
      "role": "user",
      "model": "medium",
      "session_started": "2026-05-14T03:22:00Z"
    }
  ]
}
```

> [!TIP]
> **bMAS opportunity**: A "Session History" view in Mission Control could let operators search what each agent node has been doing outside of bMAS-dispatched tasks — useful for debugging and auditing autonomous agent behavior.

---

### Analytics & Usage

Token and cost tracking aggregated from the session database.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/analytics/usage?days=N` | ✅ | Daily token/cost breakdown |
| `GET` | `/api/analytics/models?days=N` | ✅ | Per-model usage statistics |

**`GET /api/analytics/usage?days=30`** — Response:
```json
{
  "daily": [
    {
      "day": "2026-05-14",
      "input_tokens": 48173,
      "output_tokens": 7067,
      "cache_read_tokens": 8106,
      "reasoning_tokens": 0,
      "estimated_cost": 0.0,
      "actual_cost": 0,
      "sessions": 3,
      "api_calls": 4
    }
  ]
}
```

**`GET /api/analytics/models?days=30`** — Response:
```json
{
  "models": [
    {
      "model": "medium",
      "provider": "custom",
      "input_tokens": 62242,
      "output_tokens": 7784,
      "cache_read_tokens": 8106,
      "reasoning_tokens": 0,
      "estimated_cost": 0.0,
      "actual_cost": 0,
      "sessions": 4,
      "api_calls": 5,
      "tool_calls": 12,
      "avg_tokens_per_session": 17506.5,
      "capabilities": {}
    }
  ]
}
```

> [!TIP]
> **bMAS opportunity**: Aggregate per-node analytics into Mission Control's Cost tab to show Hermes-level token usage alongside bMAS-level task costs. This gives a complete picture of resource consumption per agent.

---

### Logs

Tail the agent's structured log files with optional level/component filtering and free-text search.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/logs?file=agent&lines=100` | ✅ | Tail log files |

**Query parameters:**
- `file` — Log file name: `agent` (default), `errors`, or `gateway`
- `lines` — Number of lines to return (default 100, max 500)
- `level` — Minimum level filter: `DEBUG`, `INFO`, `WARNING`, `ERROR`
- `component` — Filter by component prefix: `all`, `gateway`, `agent`, `tools`, `cli`, `cron`
- `search` — Free-text substring search (case-insensitive)

**Response:**
```json
{
  "file": "agent",
  "lines": [
    "2026-05-15T18:22:01 [INFO] agent.tools | web_search: 3 results",
    "2026-05-15T18:22:03 [INFO] agent.memory | flushed 2 memories"
  ]
}
```

> [!TIP]
> **bMAS opportunity**: Feed Hermes agent logs into Mission Control's Log Terminal alongside the bMAS daemon logs for a unified operations view. Could supplement the existing Redis stream-based log tailing.

---

### Memory

Read and manage the agent's persistent memory files (`MEMORY.md` and `USER.md`).

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/memory` | ✅ | Read memory and user files with metadata |

**`GET /api/memory`** — Response:
```json
{
  "memory": "## Environment\n- OS: Proxmox LXC (Debian)...",
  "user": "## Communication Style\n- Direct and technical...",
  "memory_path": "/root/.hermes/memories/MEMORY.md",
  "user_path": "/root/.hermes/memories/USER.md",
  "memory_mtime": "2026-05-15T18:22:00Z",
  "user_mtime": "2026-05-14T10:00:00Z"
}
```

> [!NOTE]
> `MEMORY.md` is capped at ~2,200 chars (~800 tokens) and injected into the system prompt at session start. The agent manages it via its internal memory tool (add/replace/remove entries). `USER.md` tracks user preferences and communication style.

> [!TIP]
> **bMAS opportunity**: Display each agent's memory contents in Mission Control to understand what each node has learned. Useful for debugging when an agent behaves unexpectedly — its memory may contain stale or incorrect entries.

---

### Model Configuration

Information about the currently configured model and available providers.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/model/info` | ❌ | Current model, provider, context length, capabilities |
| `GET` | `/api/model/options` | ✅ | Authenticated providers + curated model lists |
| `GET` | `/api/model/auxiliary` | ✅ | Auxiliary model slots (vision, compression, etc.) |
| `POST` | `/api/model/set` | ✅ | Change the active model |

**`GET /api/model/info`** (public) — Response:
```json
{
  "model": "medium",
  "provider": "custom",
  "auto_context_length": 256000,
  "config_context_length": 0,
  "effective_context_length": 256000,
  "capabilities": {
    "supports_tools": true,
    "supports_vision": false,
    "supports_reasoning": false,
    "context_window": 256000,
    "max_output_tokens": 8192,
    "model_family": "gemini"
  }
}
```

> [!TIP]
> **bMAS opportunity**: Display per-node model info in the Infrastructure page to show what model each agent is actually using, its context window, and capabilities. Useful for verifying that LiteLLM routing is working correctly.

---

### Cron Jobs

Hermes has a built-in scheduler for autonomous, recurring tasks. Full CRUD API.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/cron/jobs` | ✅ | List all cron jobs (including disabled) |
| `GET` | `/api/cron/jobs/{id}` | ✅ | Get a specific job |
| `POST` | `/api/cron/jobs` | ✅ | Create a new cron job |
| `PUT` | `/api/cron/jobs/{id}` | ✅ | Update a job |
| `POST` | `/api/cron/jobs/{id}/pause` | ✅ | Pause a job |
| `POST` | `/api/cron/jobs/{id}/resume` | ✅ | Resume a paused job |
| `POST` | `/api/cron/jobs/{id}/trigger` | ✅ | Trigger immediate execution |
| `DELETE` | `/api/cron/jobs/{id}` | ✅ | Delete a job |

**`POST /api/cron/jobs`** — Body:
```json
{
  "prompt": "Check disk usage and alert if any partition is above 90%",
  "schedule": "0 */6 * * *",
  "name": "disk-check",
  "deliver": "local"
}
```

> [!IMPORTANT]
> **bMAS opportunity**: This is a powerful primitive for autonomous swarm maintenance. The daemon could schedule health checks, memory consolidation, or skill pruning on agent nodes via their cron APIs. A "Scheduled Tasks" panel in Mission Control could manage these across all nodes.

---

### Tools & Toolsets

Query which tool capabilities are enabled on each agent.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/tools/toolsets` | ✅ | List all toolsets with enabled/configured status |

**Response:**
```json
[
  {
    "name": "web",
    "label": "🔍 Web Search & Scraping",
    "description": "web_search, web_extract",
    "enabled": true,
    "available": true,
    "configured": true,
    "tools": ["web_extract", "web_search"]
  },
  {
    "name": "browser",
    "label": "🌐 Browser Automation",
    "description": "navigate, click, type, scroll",
    "enabled": true,
    "available": true,
    "configured": true,
    "tools": ["browser_back", "browser_click", "browser_navigate", "..."]
  }
]
```

> [!TIP]
> **bMAS opportunity**: Show per-node tool availability in the Infrastructure page. Helps operators understand what each agent can do — e.g., if the executor has browser tools but the auditor doesn't.

---

### Profiles & Soul

Hermes supports multiple personality profiles, each with its own `SOUL.md` identity file.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/profiles` | ✅ | List all profiles with model, provider, skill count |
| `POST` | `/api/profiles` | ✅ | Create a new profile |
| `PATCH` | `/api/profiles/{name}` | ✅ | Rename/update a profile |
| `DELETE` | `/api/profiles/{name}` | ✅ | Delete a profile |
| `GET` | `/api/profiles/{name}/soul` | ✅ | Read the SOUL.md content |
| `PUT` | `/api/profiles/{name}/soul` | ✅ | Update the SOUL.md content |

**`GET /api/profiles`** — Response:
```json
{
  "profiles": [
    {
      "name": "default",
      "path": "/root/.hermes",
      "is_default": true,
      "model": "medium",
      "provider": "custom",
      "has_env": true,
      "skill_count": 91
    }
  ]
}
```

> [!TIP]
> **bMAS opportunity**: The SOUL.md is effectively the agent's personality. Mission Control could read/display each node's soul to show operators how each agent is configured to behave. Could also enable remote soul editing for role-specific tuning.

---

### Configuration

Read and write the agent's `config.yaml` settings.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/config` | ✅ | Current config (normalized for web). Returns all 150+ fields as JSON |
| `GET` | `/api/config/defaults` | ❌ | Default config values |
| `GET` | `/api/config/schema` | ❌ | Config field schema — type, description, category, and select options per field |
| `PUT` | `/api/config` | ✅ | Update config fields. Body: `{ "config": { ... } }` |
| `GET` | `/api/config/raw` | ✅ | Raw `config.yaml` text |
| `PUT` | `/api/config/raw` | ✅ | Replace `config.yaml` text |

> [!WARNING]
> Writing to config endpoints mutates the agent's `config.yaml` on disk. Changes take effect on the next agent session or gateway restart. The dashboard edits the same file as `hermes config set`.

---

### Environment Variables

Manage the agent's `.env` file (API keys, secrets).

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/env` | ✅ | List env vars (values masked). Returns set/unset status, redacted values, descriptions, and categories |
| `PUT` | `/api/env` | ✅ | Set env vars. Body: `{ "key": "VAR_NAME", "value": "secret" }` |
| `DELETE` | `/api/env` | ✅ | Remove env vars. Body: `{ "key": "VAR_NAME" }` |
| `POST` | `/api/env/reveal` | ✅ | Reveal masked values (sensitive!) |

> [!CAUTION]
> These endpoints expose and modify API keys (OpenRouter, Anthropic, Telegram tokens, etc.). **Never proxy these through Mission Control** without additional access controls. The dashboard has no built-in user authentication — it relies on localhost binding for security.

---

### Gateway & Updates

Control the Hermes gateway process (for OpenAI-compatible API at `:8642`) and agent updates.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `POST` | `/api/gateway/restart` | ✅ | Restart the gateway in background |
| `POST` | `/api/hermes/update` | ✅ | Run `hermes update` in background |
| `GET` | `/api/actions/{name}/status` | ✅ | Tail action logs and check process status |

The `{name}` for action status is either `gateway-restart` or `hermes-update`.

---

### WebSocket Endpoints

Real-time channels for interactive features.

| Path | Auth | Description |
|:--|:--|:--|
| `WS /api/pty` | ✅ (query param `?token=`) | Interactive terminal (PTY) for embedded chat TUI |
| `WS /api/ws` | ✅ (query param `?token=`) | Bidirectional chat messages |
| `WS /api/pub` | ✅ (query param `?token=`) | Publish events to a channel |
| `WS /api/events` | ✅ (query param `?token=`) | Subscribe to real-time agent events |

> [!NOTE]
> WebSockets authenticate via the `?token=<session_token>` query parameter since browsers can't set custom headers on WebSocket upgrades.

---

### Dashboard UI

Manage the dashboard's own themes and plugins.

| Method | Path | Auth | Description |
|:--|:--|:--|:--|
| `GET` | `/api/dashboard/themes` | ❌ | Available themes (built-in: default, midnight, ember, mono, cyberpunk, rose) |
| `PUT` | `/api/dashboard/theme` | ✅ | Set active theme. Persists to `config.yaml` under `dashboard.theme` |
| `GET` | `/api/dashboard/plugins` | ❌ | Installed plugins |
| `GET` | `/api/dashboard/plugins/hub` | ✅ | Browse plugin hub |
| `POST` | `/api/dashboard/agent-plugins/install` | ✅ | Install a plugin |
| `POST` | `/api/dashboard/plugins/{name}/visibility` | ✅ | Toggle plugin enable/disable |
| `DELETE` | `/api/dashboard/agent-plugins/{name}` | ✅ | Uninstall a plugin |

> [!NOTE]
> **Plugin backend routes**: Plugins can expose custom FastAPI routes mounted under `/api/plugins/<name>/`. These are defined by a `plugin_api.py` file in the plugin directory. The frontend Plugin SDK is available at `window.__HERMES_PLUGIN_SDK__`.

---

## Integration Priorities for bMAS

Based on the available API surface, here are the highest-value integrations for Mission Control, ranked by impact:

### 🔴 High Priority

| Feature | Endpoint(s) | Why |
|:--|:--|:--|
| **Skills Explorer** | `/api/skills`, `/api/skills/toggle` | ✅ **Already integrated.** Real-time view of agent procedural memory. |
| **Per-Node Analytics** | `/api/analytics/usage`, `/api/analytics/models` | Shows token consumption per agent, per model, per day. Critical for cost visibility. |
| **Model Info** | `/api/model/info` | Verify which model each agent is actually using — catches misconfigs fast. |
| **Agent Memory** | `/api/memory` | See what each agent has learned. Debug unexpected behavior from stale memory entries. |

### 🟡 Medium Priority

| Feature | Endpoint(s) | Why |
|:--|:--|:--|
| **Agent Logs** | `/api/logs` | Unified log view across all agents. Supplements Redis stream logs. |
| **Toolset Inventory** | `/api/tools/toolsets` | Know what each agent can do. Useful for task routing decisions. |
| **Session History** | `/api/sessions`, `/api/sessions/search` | Search what agents have been doing outside bMAS tasks. |
| **Cron Management** | `/api/cron/jobs` | Schedule autonomous maintenance tasks across the swarm. |
| **Gateway Chat** | `POST /v1/chat/completions` (port 8642) | Programmatically send prompts to agents. Enables daemon-to-agent communication. |

### 🟢 Low Priority (Future)

| Feature | Endpoint(s) | Why |
|:--|:--|:--|
| **Soul/Profile Viewer** | `/api/profiles`, `/api/profiles/{name}/soul` | Display agent personalities and allow remote tuning. |
| **Config Management** | `/api/config`, `/api/config/raw` | Remote config editing (risky — needs access controls). |
| **Real-time Events** | `WS /api/events` | Stream agent activity into Mission Control in real-time. |

---

## Quick Start: Adding a New Hermes Proxy Route

To add a new Hermes Dashboard endpoint to Mission Control:

### 1. Add the Next.js API route

Create a new file in `mission-control/src/app/api/<feature>/route.ts`:

```typescript
import { NextResponse } from "next/server";
import { AGENT_DASHBOARD_HOSTS } from "@/lib/config";

/** Fetch the Hermes session token from the dashboard HTML. */
async function fetchSessionToken(baseUrl: string): Promise<string | null> {
  try {
    const res = await fetch(baseUrl, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) return null;
    const html = await res.text();
    const match = html.match(/__HERMES_SESSION_TOKEN__="([^"]+)"/);
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  const node = searchParams.get("node");

  if (!node || !(node in AGENT_DASHBOARD_HOSTS)) {
    return NextResponse.json({ error: "Invalid node" }, { status: 400 });
  }

  const baseUrl = AGENT_DASHBOARD_HOSTS[node];
  const token = await fetchSessionToken(baseUrl);
  if (!token) {
    return NextResponse.json({ error: "Dashboard unreachable" }, { status: 503 });
  }

  const res = await fetch(`${baseUrl}/api/<hermes-endpoint>`, {
    cache: "no-store",
    signal: AbortSignal.timeout(5_000),
    headers: { "X-Hermes-Session-Token": token },
  });

  const data = await res.json();
  return NextResponse.json(data);
}
```

### 2. Wire the frontend component

Fetch from your new route: `fetch("/api/<feature>?node=planner")`.

### 3. Token caching (optional optimization)

The current pattern scrapes the token on every request. Since the token is stable for the lifetime of the dashboard process, you could cache it per node:

```typescript
const tokenCache = new Map<string, { token: string; ts: number }>();
const TOKEN_TTL_MS = 5 * 60 * 1000; // 5 minutes

async function getToken(baseUrl: string): Promise<string | null> {
  const cached = tokenCache.get(baseUrl);
  if (cached && Date.now() - cached.ts < TOKEN_TTL_MS) {
    return cached.token;
  }
  const token = await fetchSessionToken(baseUrl);
  if (token) tokenCache.set(baseUrl, { token, ts: Date.now() });
  return token;
}
```

If a cached token returns `401`, invalidate and retry.

---

## Reference: Running the Dashboard

The dashboard is started on each edge node via systemd:

```ini
# /etc/systemd/system/hermes-dashboard.service
[Unit]
Description=Hermes Agent Dashboard
After=hermes-agent.service

[Service]
Type=simple
User=root
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/local/bin/hermes dashboard --host 0.0.0.0 --port 9119 --insecure
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-party.target
```

> [!WARNING]
> The `--insecure` flag allows binding to `0.0.0.0` (required for network access from the control plane). Without it, the dashboard only listens on `127.0.0.1`. The ephemeral session token provides the auth layer.

### Key Paths on Agent Nodes

| Path | Description |
|:--|:--|
| `~/.hermes/config.yaml` | Agent configuration (150+ fields) |
| `~/.hermes/.env` | API keys and secrets |
| `~/.hermes/skills/` | Installed skills directory (~90 bundled) |
| `~/.hermes/plugins/` | User-installed plugins |
| `~/.hermes/logs/` | Agent log files (`agent.log`, `errors.log`, `gateway.log`) |
| `~/.hermes/SOUL.md` | Default profile personality |
| `~/.hermes/memories/MEMORY.md` | Agent's persistent memory (~2,200 char cap) |
| `~/.hermes/memories/USER.md` | User profile/preferences |
| `~/.hermes/kanban.db` | Default Kanban board (SQLite) |
| `~/.hermes/kanban/boards/` | Additional Kanban boards |
| `~/.hermes/profiles/` | Named profiles (each with own config, env, skills) |

---

## Appendix A: Gateway API Server (Port 8642)

The Gateway API Server is a **separate process** from the Dashboard. It exposes Hermes as an **OpenAI-compatible HTTP endpoint** for programmatic interaction. Start with `hermes gateway`.

### Authentication

Uses **Bearer token** auth via the `Authorization` header, configured by `API_SERVER_KEY` in `~/.hermes/.env`:

```bash
curl http://<node_ip>:8642/v1/chat/completions \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "hermes-agent", "messages": [{"role": "user", "content": "Hello"}]}'
```

### Configuration

| Env Var | Default | Description |
|:--|:--|:--|
| `API_SERVER_ENABLED` | `false` | Must be `true` to start the gateway |
| `API_SERVER_KEY` | — | Bearer token (required when binding to `0.0.0.0`) |
| `API_SERVER_HOST` | `127.0.0.1` | Bind address |
| `API_SERVER_PORT` | `8642` | Listen port |
| `API_SERVER_CORS_ORIGINS` | — | Comma-separated CORS allowlist for browser access |
| `API_SERVER_MODEL_NAME` | `hermes-agent` | Model name advertised by `/v1/models` |

### Endpoints

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/v1/chat/completions` | Standard OpenAI Chat Completions (stateless) |
| `POST` | `/v1/responses` | OpenAI Responses API (stateful via `previous_response_id`) |
| `GET` | `/v1/responses/{id}` | Retrieve a stored response |
| `DELETE` | `/v1/responses/{id}` | Delete a stored response |
| `GET` | `/v1/models` | List available models (returns the agent's profile name) |
| `GET` | `/v1/capabilities` | Machine-readable feature discovery for UIs and orchestrators |
| `GET` | `/health` | Health check (`{"status": "ok"}`). Also at `/v1/health` |
| `GET` | `/health/detailed` | Extended health: active sessions, running agents, resource usage |

**Runs API** (long-form sessions with SSE progress):

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/v1/runs` | Create a new agent run, returns `run_id` |
| `GET` | `/v1/runs/{run_id}` | Poll run state (status, output, usage) |
| `GET` | `/v1/runs/{run_id}/events` | SSE stream of tool progress, token deltas, lifecycle events |
| `POST` | `/v1/runs/{run_id}/stop` | Interrupt a running agent turn |

**Jobs API** (scheduled/background work via gateway):

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/jobs` | List all scheduled jobs |
| `POST` | `/api/jobs` | Create a job (same shape as `hermes cron`) |
| `GET` | `/api/jobs/{job_id}` | Get job definition and last-run state |
| `PATCH` | `/api/jobs/{job_id}` | Partial update of job fields |
| `DELETE` | `/api/jobs/{job_id}` | Delete a job (cancels in-flight runs) |
| `POST` | `/api/jobs/{job_id}/pause` | Pause without deleting |
| `POST` | `/api/jobs/{job_id}/resume` | Resume a paused job |
| `POST` | `/api/jobs/{job_id}/run` | Trigger immediate execution |

**`GET /v1/capabilities`** — Response:
```json
{
  "object": "hermes.api_server.capabilities",
  "platform": "hermes-agent",
  "model": "hermes-agent",
  "auth": { "type": "bearer", "required": true },
  "features": {
    "chat_completions": true,
    "responses_api": true,
    "run_submission": true,
    "run_status": true,
    "run_events_sse": true,
    "run_stop": true
  }
}
```

> [!TIP]
> **bMAS opportunity**: The Runs API is the most promising integration point for daemon-to-agent communication. The bMAS orchestrator could submit tasks via `POST /v1/runs`, subscribe to progress via SSE at `/v1/runs/{id}/events`, and cancel via `/v1/runs/{id}/stop` — replacing the current Redis-based task dispatch for Hermes-native agents.

> [!WARNING]
> The Gateway API gives **full access** to the agent's toolset including terminal commands. When binding to `0.0.0.0`, `API_SERVER_KEY` is mandatory. CORS is disabled by default.

---

## Appendix B: Gotchas & Implementation Notes

1. **Token invalidation**: Dashboard session tokens are invalidated when `hermes dashboard` restarts. Your token cache must handle `401` responses by re-scraping the token.

2. **Blocking `/api/status`**: This endpoint checks gateway health synchronously. Always use a connect timeout (5s recommended) to avoid hanging.

3. **Two auth mechanisms**: Dashboard (`:9119`) uses ephemeral session tokens scraped from HTML. Gateway (`:8642`) uses `API_SERVER_KEY` as a Bearer token. Never mix them.

4. **Config vs. Env**: `config.yaml` controls agent behavior (model, toolsets, memory). `.env` controls secrets and feature flags (`API_SERVER_ENABLED`, API keys). Config changes require session/gateway restart; env changes can be hot-reloaded via `/reload`.

5. **Memory path change**: In newer Hermes versions, memory files moved from `~/.hermes/MEMORY.md` to `~/.hermes/memories/MEMORY.md`. The `/api/memory` response includes the actual paths.

6. **Dashboard prerequisites**: The dashboard requires `pip install 'hermes-agent[web,pty]'`. The `web` extra provides FastAPI/Uvicorn; the `pty` extra enables the Chat tab's embedded TUI.

7. **Kanban is single-host**: The Kanban board uses a local SQLite database. Cross-node Kanban orchestration isn't supported natively — each node has its own board. For multi-node coordination, use bMAS's own Redis-based orchestration.
