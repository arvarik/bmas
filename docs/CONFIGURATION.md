# Configuration

Stigmergic uses two local configuration files.

- `bmas.yaml` contains non-secret runtime settings.
- `.env` contains secrets and host-level overrides.

Run `./scripts/bmas init` to create both files. Do not commit either file.

## Validate a configuration

Run these commands after each change.

```bash
python3 scripts/validate_configs.py
docker compose config --quiet
./scripts/bmas doctor
```

The first command validates all published examples. The daemon rejects unknown fields, invalid values, and invalid cross-references.

The generated [JSON Schema](reference/config.schema.json) supports editor completion. Run this command after a schema change:

```bash
python3 scripts/generate_config_schema.py
```

## Supported templates

| File | Use |
|:---|:---|
| [`bmas.example.yaml`](../bmas.example.yaml) | Single-host Gemini starter. |
| [`examples/starters/anthropic.yaml`](../examples/starters/anthropic.yaml) | Single-host Anthropic starter. |
| [`examples/starters/openai.yaml`](../examples/starters/openai.yaml) | Single-host OpenAI starter. |
| [`examples/classic-homelab.yaml`](../examples/classic-homelab.yaml) | Advanced Hermes and local-inference example. |

Use one starter template for a first installation. The homelab template requires external services at each configured address.

## Top-level structure

```yaml
project: {}
control_plane: {}
nodes:
  - name: execution-node
triage: {}
models: {}
model_pools: {}
routing: {}
coordination: {}
storage: {}
monitoring: {}
```

The `model_pools` and `monitoring` sections are optional.

## `project`

| Field | Type | Required | Purpose |
|:---|:---|:---|:---|
| `name` | string | Yes | Sets the Mission Control title. |
| `description` | string | No | Describes the deployment. |

## `control_plane`

This section defines published service addresses.

| Field | Type | Required | Purpose |
|:---|:---|:---|:---|
| `host` | string | Yes | Sets the public host label and non-Compose fallback host. |
| `ports.redis` | integer | Yes | Sets the Redis port. |
| `ports.litellm` | integer | Yes | Sets the LiteLLM port. |
| `ports.daemon` | integer | Yes | Sets the daemon port. |
| `ports.dashboard` | integer | No | Sets the Mission Control port. The default is `9321`. |
| `ports.triage` | integer | No | Sets the optional local triage port. The default is `8001`. |

The starter keeps `host: localhost`. Docker Compose supplies internal service URLs through environment variables.

Do not set `host: localhost` for an external Hermes node. That value would refer to the node itself.

## `nodes`

Each runtime requires at least one execution node.

The starter defines one Docker service:

```yaml
nodes:
  - name: starter-agent
    host: agent
    port: 8000
    role: starter
    color: "#5eead4"
```

| Field | Type | Required | Purpose |
|:---|:---|:---|:---|
| `name` | string | Yes | Gives the node a stable operator name. |
| `host` | string | Yes | Sets the agent API host. |
| `port` | integer | No | Sets the agent API port. The default is `8000`. |
| `role` | string | Yes | Gives the physical node a display role. |
| `color` | string | No | Sets the node color in Mission Control. |
| `dashboard_port` | integer | No | Sets an optional Hermes dashboard port. |
| `inference` | mapping | No | Registers a local OpenAI-compatible model server. |

An `inference` mapping accepts `host`, `port`, `model`, and optional `max_tokens` fields.

The starter agent performs tool-free model calls. Use [Node Setup](NODE_SETUP.md) when an agent needs tools, an isolated workspace, or more capacity.

## `triage`

Triage selects one routing tier for each task.

| Field | Type | Default | Purpose |
|:---|:---|:---|:---|
| `enabled` | boolean | `true` | Enables model-based classification. |
| `backend` | `cloud` or `local` | `cloud` | Selects the classification service. |
| `model` | string | `starter-model` | Selects a LiteLLM alias for cloud triage. |
| `local_model` | string | `Qwen/Qwen3-1.7B` | Selects the vLLM model for local triage. |
| `gpu_memory_utilization` | number | `0.35` | Limits the local vLLM GPU fraction. |
| `max_model_len` | integer | `8192` | Limits the local triage context. |
| `default_complexity` | tier | `medium` | Selects the tier when triage is disabled or fails. |

The cloud backend uses the alias from `triage.model`. The provider can be Gemini, Anthropic, OpenAI, or another LiteLLM provider.

The local backend requires the Compose `gpu` profile and an NVIDIA runtime.

## `models`

Each key under `models` creates one LiteLLM alias.

```yaml
models:
  starter-model:
    provider: gemini
    model: gemini-3.5-flash
    api_key_env: GEMINI_API_KEY
    max_tokens: 65536
    pricing:
      input_cost_per_token: 1.5e-7
      output_cost_per_token: 6.0e-7
```

| Field | Type | Required | Purpose |
|:---|:---|:---|:---|
| `provider` | string | Yes | Sets the LiteLLM provider prefix. |
| `model` | string | Yes | Sets the provider model identifier. |
| `api_key_env` | string | Yes | Names the environment variable that contains the provider key. |
| `api_base` | string | No | Sets a custom OpenAI-compatible API base. |
| `max_tokens` | integer | No | Limits output tokens. The default is `4096`. |
| `pricing.input_cost_per_token` | number | No | Sets static input pricing in US dollars. |
| `pricing.output_cost_per_token` | number | No | Sets static output pricing in US dollars. |
| `pricing.source` | string | No | Records the pricing source label. |

Static pricing supports daemon cost estimates. Runtime provider cost data can fill missing pricing.

## `routing`

Routing maps each complexity tier to one model alias.

```yaml
routing:
  simple: starter-model
  light: starter-model
  medium: starter-model
  complex: starter-model
```

Each value must match a key under `models`. The special value `local` requires at least one node with an `inference` mapping.

## `model_pools`

This optional section gives generated experts more than one model choice.

```yaml
model_pools:
  medium: [fast-model, careful-model]
  complex: [careful-model, second-opinion-model]
```

Each key must be `simple`, `light`, `medium`, or `complex`. Each list entry must match a key under `models`.

The runtime uses `routing.<tier>` when a tier has no model pool.

## `coordination`

Set `variant: classic` for the deployment default. Mission Control can select another registered runtime for one task or test arm.

| Field | Type | Default | Purpose |
|:---|:---|:---|:---|
| `variant` | string | `classic` | Selects the deployment default runtime. The file schema keeps Classic as the supported default. |
| `view_budget_tokens` | integer | `12000` | Limits the blackboard text supplied to a model. |
| `round_execution` | value | `concurrent` | Selects `concurrent` or `sequential` role execution. |

### `coordination.classic`

| Field | Default | Purpose |
|:---|:---|:---|
| `max_rounds` | `4` | Limits control-unit rounds. |
| `max_duration_s` | `1800` | Limits the complete task duration. |
| `budget_ceiling_usd` | `0.50` | Stops a task at the cost ceiling. |
| `max_concurrent_activations` | `3` | Limits active role calls in one round. |
| `experts_per_tier` | tier mapping | Sets the generated expert count. |
| `cleaner_entry_threshold` | `12` | Starts cleanup after this board entry count. |
| `cleaner_token_threshold` | `8000` | Starts cleanup after this estimated token count. |
| `cleaner_retention_weights` | weight mapping | Scores entries during cleanup. |
| `stall_rounds` | `2` | Stops repeated rounds that add no progress. |
| `max_replans` | `2` | Limits control-unit replans. |
| `cu_mode` | `llm` | Selects `llm` or `heuristic_first`. |
| `coordinator_narration` | `false` | Adds control-unit reasons to the event stream. |
| `sole_similarity` | `auto` | Selects `auto`, `exact`, `embedding`, or `judge`. |

Use lower limits during initial provider tests. Increase one limit at a time and run `./scripts/bmas smoke` after each change.

### `coordination.role_registry`

Each key names one classic role. The starter includes `planner`, `expert`, `critic`, `conflict_resolver`, `cleaner`, and `decider`.

| Field | Default | Purpose |
|:---|:---|:---|
| `enabled` | `true` | Enables the role. |
| `preferred_host` | `null` | Pins the role to one node host. `null` enables load balancing. |
| `profile` | Required | Selects the Hermes profile name or starter prompt profile. |
| `dispatch_port` | `8000` | Selects the agent API port. |

Every non-null `preferred_host` must match a `nodes[].host` value.

### `coordination.board`

| Field | Default | Purpose |
|:---|:---|:---|
| `max_entry_chars` | `8000` | Limits one board entry body. |
| `max_title_len` | `200` | Limits one board entry title. |
| `salience_weights` | weight mapping | Scores entries by confidence, recency, references, and penalties. |

## `storage`

| Field | Default | Purpose |
|:---|:---|:---|
| `enabled` | `false` | Enables uploads and task artifacts. |
| `user_media_dir` | `/data/uploads` | Sets the upload directory inside the daemon. |
| `artifacts_dir` | `/data/output` | Sets the artifact directory inside the daemon. |
| `max_upload_mb` | `50` | Limits one upload. |
| `max_task_output_mb` | `500` | Limits all artifacts for one task. |
| `allowed_upload_types` | extension list | Allows upload filename extensions. |
| `pdf_extraction` | `pymupdf` | Selects `pymupdf`, `pypdf`, or `off`. |
| `extraction_max_chars` | `60000` | Limits extracted document text. |

The Compose stack mounts named volumes at `/data/uploads` and `/data/output`. Keep these container paths for the starter.

The published starter files set `storage.enabled` to `true`. Set it to `false` only when the deployment must disable uploads and artifacts.

## `monitoring`

Set `monitoring.beszel_hub` to a Beszel URL when Mission Control must read host telemetry.

Leave the complete section out when you do not use Beszel.

## Required `.env` values

The starter command generates the first six values.

| Variable | Purpose |
|:---|:---|
| `REDIS_PASSWORD` | Authenticates Redis clients. |
| `LITELLM_MASTER_KEY` | Authenticates model gateway clients. |
| `BMAS_NODE_KEY` | Authenticates agent logs and traces. |
| `BMAS_EXECUTE_KEY` | Authenticates daemon and Mission Control requests to agents. |
| `BMAS_API_KEY` | Authenticates task and control mutations. |
| `BMAS_DASHBOARD_KEY` | Protects Mission Control pages and proxy routes. |

Also set each provider variable named by `models.*.api_key_env`.

Do not reuse one value for different keys. Do not put a secret in `bmas.yaml`.

## Host and port overrides

| Variable | Default | Purpose |
|:---|:---|:---|
| `BMAS_BIND_ADDRESS` | `127.0.0.1` | Selects the host address for every published port. |
| `REDIS_PORT` | `6379` | Changes the host Redis port. |
| `LITELLM_PORT` | `4000` | Changes the host LiteLLM port. |
| `AGENT_PORT` | `8000` | Changes the host starter-agent port. |
| `DAEMON_PORT` | `9000` | Changes the host daemon port. |
| `DASHBOARD_PORT` | `9321` | Changes the host Mission Control port. |
| `TRIAGE_PORT` | `8001` | Changes the optional local triage port. |
| `BMAS_STARTER_MODEL` | `starter-model` | Selects the starter-agent LiteLLM alias. |

Keep `BMAS_STARTER_MODEL` equal to a configured model alias.

## Runtime limit overrides

The `.env.example` file lists every supported runtime limit. The main groups control task admission, agent endpoint capacity, circuit recovery, event delivery, and projection caching.

Change these limits only after you capture a baseline with the [Classic Harness](CLASSIC_HARNESS.md).

### Benchmark scheduler limits

| Variable | Default | Purpose |
|:---|:---|:---|
| `BMAS_BENCHMARK_MAX_ACTIVE` | `4` | Limits active benchmark attempts across scheduler workers. |
| `BMAS_BENCHMARK_LEASE_SECONDS` | `30` | Sets a renewable attempt lease from 10 through 300 seconds. |
| `BMAS_BENCHMARK_RUNTIME_LIMITS` | empty object | Maps runtime identifiers to active-attempt limits. |
| `BMAS_BENCHMARK_MODEL_LIMITS` | empty object | Maps model aliases to active-attempt limits. |
| `BMAS_BENCHMARK_PROVIDER_LIMITS` | empty object | Maps provider names to active-attempt limits. |
| `BMAS_BENCHMARK_MODEL_PROVIDERS` | empty object | Maps model aliases to provider names for provider limits. |

The four mapping values use JSON objects. This example limits Patchboard, one model, and its provider.

```env
BMAS_BENCHMARK_RUNTIME_LIMITS={"patchboard":2}
BMAS_BENCHMARK_MODEL_LIMITS={"starter-model":3}
BMAS_BENCHMARK_PROVIDER_LIMITS={"gemini":3}
BMAS_BENCHMARK_MODEL_PROVIDERS={"starter-model":"gemini"}
```

The scheduler claims every model alias in an arm snapshot until the task reports its actual model. This rule prevents early over-admission.

## Internal service URL overrides

Docker Compose sets these values inside containers:

- `BMAS_REDIS_URL`
- `BMAS_LITELLM_URL`
- `BMAS_TRIAGE_URL`
- `BMAS_DAEMON_URL`

Do not add these values to a normal starter `.env`. Set them only when you run services outside the default Compose network.

## Compatibility fields

The loader accepts `traditional` as an old alias for `classic`. It also accepts `triage.backend: gemini` as an old alias for `cloud`.

Do not use these aliases in a new configuration. The loader reports a warning and the public examples stay warning-free.

The old `blackboard_v2` field has no effect. The classic runtime always uses the durable board.
