# Configuration Reference

Complete reference for `bmas.yaml` — the central configuration file for your bMAS deployment.

## File Location

By default, all services look for the config at `/etc/bmas/bmas.yaml` (mounted via Docker Compose).
Override with the `BMAS_CONFIG` environment variable.

## Schema

---

### `project` *(required)*

| Field | Type | Required | Default | Description |
|:---|:---|:---|:---|:---|
| `name` | string | ✅ | — | Your deployment name. Shown in the dashboard title. |
| `description` | string | ❌ | — | Description for SEO/metadata. |

```yaml
project:
  name: "My bMAS Swarm"
  description: "AI agent swarm for automated research"
```

---

### `control_plane` *(required)*

Defines the machine running the Docker Compose stack.

| Field | Type | Required | Default | Description |
|:---|:---|:---|:---|:---|
| `host` | string | ✅ | — | IP or hostname of the control plane machine. |
| `ports.redis` | int | ✅ | — | Redis port. |
| `ports.litellm` | int | ✅ | — | LiteLLM proxy port. |
| `ports.triage` | int | ❌ | `8001` | Triage vLLM port. |
| `ports.daemon` | int | ✅ | — | Daemon API port. |
| `ports.dashboard` | int | ✅ | — | Mission Control dashboard port. |

```yaml
control_plane:
  host: "192.168.1.100"
  ports:
    redis: 6379
    litellm: 4000
    triage: 8001
    daemon: 9000
    dashboard: 9321
```

---

### `nodes` *(optional, default: `[]`)*

List of agent + inference nodes in your cluster. Each node runs a Hermes agent and optionally a local inference server.

| Field | Type | Required | Default | Description |
|:---|:---|:---|:---|:---|
| `name` | string | ✅ | — | Friendly name for the node. |
| `host` | string | ✅ | — | IP or hostname of the agent. |
| `port` | int | ❌ | `8000` | Agent API port. |
| `role` | string | ✅ | — | One of: `planner`, `executor`, `auditor`. |
| `color` | string | ❌ | per-role default | Hex color for the dashboard UI. |
| `inference.host` | string | ❌ | — | IP of the local inference server. |
| `inference.port` | int | ❌ | `8080` | Inference server port. |
| `inference.model` | string | ❌ | — | Model identifier (e.g., `gemma-4-e4b`). |

```yaml
nodes:
  - name: "node-1"
    host: "192.168.1.101"
    port: 8000
    role: planner
    color: "#a78bfa"
    inference:
      host: "192.168.1.102"
      port: 8080
      model: "gemma-4-e4b"
```

**No nodes?** Set `nodes: []` and all routing goes to cloud models.

---

### `triage` *(optional)*

Controls the local complexity classifier that runs on the control plane GPU.

| Field | Type | Required | Default | Description |
|:---|:---|:---|:---|:---|
| `enabled` | bool | ❌ | `true` | Set `false` to skip classification. |
| `model` | string | ❌ | `Qwen/Qwen3-1.7B` | vLLM model for classification. |
| `gpu_memory_utilization` | float | ❌ | `0.35` | GPU memory fraction for vLLM. |
| `max_model_len` | int | ❌ | `8192` | Maximum context length. |
| `default_complexity` | string | ❌ | `medium` | Fallback tier when triage is disabled or unreachable. |

```yaml
# With GPU
triage:
  enabled: true
  model: "Qwen/Qwen3-1.7B"

# Without GPU
triage:
  enabled: false
  default_complexity: medium
```

---

### `models` *(required)*

Define the LLM models available for task execution. Each key becomes a LiteLLM model alias.

| Field | Type | Required | Description |
|:---|:---|:---|:---|
| `provider` | string | ✅ | LiteLLM provider: `gemini`, `anthropic`, `openai`, etc. |
| `model` | string | ✅ | Model identifier (e.g., `gemini-3.1-pro-preview`). |
| `api_key_env` | string | ✅ | Environment variable name holding the API key. |
| `max_tokens` | int | ❌ | Maximum tokens for generation. |

```yaml
models:
  gemini-pro:
    provider: gemini
    model: "gemini-3.1-pro-preview"
    api_key_env: GEMINI_API_KEY
    max_tokens: 8192

  claude-sonnet:
    provider: anthropic
    model: "claude-sonnet-4-20250514"
    api_key_env: ANTHROPIC_API_KEY

  gpt-4o:
    provider: openai
    model: "gpt-4o"
    api_key_env: OPENAI_API_KEY
```

---

### `routing` *(required)*

Maps complexity tiers to models defined in the `models` section.

| Tier | Description |
|:---|:---|
| `complex` | Most demanding tasks (reasoning, multi-step analysis) |
| `medium` | Moderate tasks (summarization, code review) |
| `light` | Simple tasks (formatting, basic Q&A) |
| `simple` | Trivial tasks (routing to free local models) |

The special value `"local"` routes to edge inference nodes (round-robin).

```yaml
# Single provider
routing:
  complex: gemini-pro
  medium: gemini-pro
  light: gemini-flash-lite
  simple: local

# Multi-provider (mix and match!)
routing:
  complex: gemini-pro         # Hardest tasks → Gemini Pro
  medium: claude-sonnet       # Medium → Claude
  light: gpt-4o               # Light → GPT-4o
  simple: local               # Free → edge nodes
```

---

### `monitoring` *(optional)*

| Field | Type | Required | Description |
|:---|:---|:---|:---|
| `beszel_hub` | string | ❌ | URL of your Beszel Hub for system monitoring. |

```yaml
monitoring:
  beszel_hub: "http://192.168.1.200:8090"
```

---

## Environment Variables (`.env`)

Secrets are **never** stored in `bmas.yaml`. They go in `.env`:

| Variable | Required | Description |
|:---|:---|:---|
| `REDIS_PASSWORD` | ✅ | Redis authentication password. |
| `LITELLM_MASTER_KEY` | ✅ | LiteLLM proxy authentication key. |
| `GEMINI_API_KEY` | ❌ | Google Gemini API key. |
| `ANTHROPIC_API_KEY` | ❌ | Anthropic (Claude) API key. |
| `OPENAI_API_KEY` | ❌ | OpenAI API key. |
| `HF_TOKEN` | ❌ | Hugging Face token for gated models. |

At least one cloud API key is required unless all routing goes to `local`.

---

## Example Configurations

See the `examples/` directory:
- **[stigmergic.yaml](../examples/stigmergic.yaml)** — Full 3-node deployment with Gemini
- **[minimal-cloud.yaml](../examples/minimal-cloud.yaml)** — No edge nodes, single cloud provider
- **[multi-provider.yaml](../examples/multi-provider.yaml)** — Mix of Gemini + Claude + local
