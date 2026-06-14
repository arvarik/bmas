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
| `role` | string | ✅ | — | Primary role: `planner`, `executor`, `auditor`, or any custom role. |
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
| `pricing.input_cost_per_token` | float | ❌ | Cost per input token (USD). Enables daemon-side cost tracking. |
| `pricing.output_cost_per_token` | float | ❌ | Cost per output token (USD). |

```yaml
models:
  gemini-pro:
    provider: gemini
    model: "gemini-3.1-pro-preview"
    api_key_env: GEMINI_API_KEY
    max_tokens: 8192
    pricing:
      input_cost_per_token: 1.25e-6
      output_cost_per_token: 5.0e-6

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
routing:
  complex: gemini-pro
  medium: gemini-flash
  light: gemini-flash-lite
  simple: local
```

---

### `coordination` *(required)*

Controls the coordination paradigm used for multi-agent tasks.

| Field | Type | Required | Default | Description |
|:---|:---|:---|:---|:---|
| `variant` | string | ✅ | — | Active variant: `traditional`, `patchboard`, or `stigmergic`. |
| `blackboard_v2` | bool | ❌ | `true` | Enables the v2 board substrate (entries + events). |
| `view_budget_tokens` | int | ❌ | `12000` | Full-board token budget; budgeted view mode above this. |
| `round_execution` | string | ❌ | `concurrent` | `concurrent` (parallel agents) or `sequential` (paper-exact). |

#### `coordination.traditional` *(when variant = traditional)*

| Field | Type | Default | Description |
|:---|:---|:---|:---|
| `max_rounds` | int | `4` | Maximum CU rounds before forced convergence. |
| `max_duration_s` | int | `1800` | Maximum task duration in seconds. |
| `budget_ceiling_usd` | float | `0.50` | Maximum LLM spend per task (USD). Halts execution when exceeded. |
| `max_concurrent_activations` | int | `3` | Max agents activated per round. |
| `experts_per_tier` | map | see below | Number of expert agents per complexity tier. |
| `cleaner_entry_threshold` | int | `12` | Board entry count that triggers Cleaner activation. |
| `stall_rounds` | int | `2` | Consecutive rounds without progress before forced convergence. |
| `cu_mode` | string | `llm` | Control Unit mode: `llm` (LLM-driven) or `heuristic_first` (rule-based). |
| `coordinator_narration` | bool | `false` | Emit CU narration events (routing rationale visible in TurnGraph). |
| `sole_similarity` | string | `auto` | Similarity check mode: `auto`, `exact`, `embedding`, or `judge`. |

```yaml
coordination:
  variant: traditional
  view_budget_tokens: 12000
  round_execution: concurrent
  traditional:
    max_rounds: 8
    max_duration_s: 1800
    budget_ceiling_usd: 1.00
    max_concurrent_activations: 3
    experts_per_tier: { simple: 0, light: 1, medium: 2, complex: 3 }
    cleaner_entry_threshold: 12
    stall_rounds: 2
    cu_mode: llm
    coordinator_narration: true
    sole_similarity: auto
```

#### `coordination.role_registry` *(optional)*

Maps blackboard roles to Hermes profiles and preferred hosts for dispatch.

| Field | Type | Description |
|:---|:---|:---|
| `preferred_host` | string or null | IP for preferred dispatch. `null` = load-balanced across all nodes. |
| `profile` | string | Hermes profile name (in `~/.hermes/profiles/<profile>/`). |
| `dispatch_port` | int | Port of the `api_server.py` bridge on the target node. |

```yaml
  role_registry:
    planner:
      preferred_host: "192.168.1.101"
      profile: planner
      dispatch_port: 8000
    expert:
      preferred_host: null              # load-balanced
      profile: expert
      dispatch_port: 8000
    critic:
      preferred_host: "192.168.1.101"
      profile: critic
      dispatch_port: 8000
    decider:
      preferred_host: "192.168.1.111"
      profile: decider
      dispatch_port: 8000
```

#### `coordination.board` *(optional)*

Board entry constraints and salience scoring weights.

| Field | Type | Default | Description |
|:---|:---|:---|:---|
| `max_entry_chars` | int | `8000` | Max body length; larger content becomes artifacts. |
| `max_title_len` | int | `200` | Max title length for indexing. |
| `salience_weights.confidence` | float | `0.4` | Weight for entry confidence score. |
| `salience_weights.recency` | float | `0.2` | Weight for entry recency. |
| `salience_weights.refs_in` | float | `0.3` | Weight for inbound references. |
| `salience_weights.penalty` | float | `0.3` | Weight for superseded/deprecated penalty. |

---

### `storage` *(optional)*

File upload and artifact storage settings.

| Field | Type | Default | Description |
|:---|:---|:---|:---|
| `enabled` | bool | `false` | Enable file upload and artifact storage. |
| `user_media_dir` | string | `/opt/bmas-data/uploads` | Directory for user-uploaded files. |
| `artifacts_dir` | string | `/opt/output` | Directory for agent-produced artifacts. |
| `max_upload_mb` | int | `50` | Maximum file upload size (MB). |
| `max_task_output_mb` | int | `500` | Maximum total artifact size per task (MB). |
| `allowed_upload_types` | list | `[pdf, txt, md, ...]` | Allowed file extensions for upload. |
| `pdf_extraction` | string | `pymupdf` | PDF text extraction backend: `pymupdf`, `pypdf`, or `off`. |
| `extraction_max_chars` | int | `60000` | Maximum characters extracted from a document. |

```yaml
storage:
  enabled: true
  user_media_dir: "/opt/bmas-data/uploads"
  artifacts_dir: "/opt/output"
  max_upload_mb: 50
  allowed_upload_types: [pdf, txt, md, csv, json, png, jpg, docx]
  pdf_extraction: pymupdf
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
| `BMAS_NODE_KEY` | ✅ | Bearer token for agent ingest auth (traces + logs). |
| `GEMINI_API_KEY` | ❌ | Google Gemini API key. |
| `ANTHROPIC_API_KEY` | ❌ | Anthropic (Claude) API key. |
| `OPENAI_API_KEY` | ❌ | OpenAI API key. |
| `HF_TOKEN` | ❌ | Hugging Face token for gated models. |
| `BESZEL_EMAIL` | ❌ | Beszel Hub login email (for telemetry). |
| `BESZEL_PASSWORD` | ❌ | Beszel Hub login password (for telemetry). |

At least one cloud API key is required unless all routing goes to `local`.

**`BMAS_NODE_KEY`:** This token authenticates agent nodes when they ship traces and logs back to the daemon. Set the same value on the daemon (via `.env`) and on each agent node (in its systemd service Environment).

**Beszel credentials:** Required if `monitoring.beszel_hub` is set in `bmas.yaml`. These are the email/password you use to log into the Beszel web UI.

---

## Example Configurations

See the `examples/` directory:
- **[stigmergic.yaml](../examples/stigmergic/stigmergic.yaml)** — Full 3-node deployment with Gemini
- **[minimal-cloud.yaml](../examples/minimal-cloud.yaml)** — No edge nodes, single cloud provider
- **[multi-provider.yaml](../examples/multi-provider.yaml)** — Mix of Gemini + Claude + local

See also **[bmas.example.yaml](../bmas.example.yaml)** for a fully commented reference with all options.
