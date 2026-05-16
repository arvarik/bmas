# LiteLLM — Unified Model Gateway

The centralized AI model router for bMAS. Provides a single OpenAI-compatible API endpoint that abstracts all model backends — local edge nodes, the local triage model, and cloud Gemini APIs — behind unified routing, cost tracking, and retry logic.

> Runs as Docker container `bmas-litellm` on the control plane at port 4000.

## Architecture

```
bMAS Daemon (:9000)
       │
       │  model="gemini-pro"
       ▼
 ┌───────────┐
 │  LiteLLM  │──── model_group_alias ────▶ gemini-pro       ──▶ Gemini 3.1 Pro (cloud)
 │   :4000   │──── model_group_alias ────▶ gemini-flash     ──▶ Gemini 3 Flash (cloud)
 │           │──── model_group_alias ────▶ gemini-flash-lite ▶ Gemini 3.1 Flash Lite (cloud)
 │           │──── direct ───────────────▶ edge-node-*      ──▶ Local inference (edge nodes)
 └───────────┘
```

## Model Routing

The daemon uses model names from `bmas.yaml`'s `routing` section; LiteLLM resolves them to actual backends:

| Daemon Calls | LiteLLM Routes To | Backend | Cost |
|:---|:---|:---|:---|
| `gemini-pro` | `gemini-pro` | Gemini 3.1 Pro (cloud API) | $$$ |
| `gemini-flash` | `gemini-flash` | Gemini 3 Flash (cloud API) | $$ |
| `gemini-flash-lite` | `gemini-flash-lite` | Gemini 3.1 Flash Lite (cloud API) | $ |
| `edge-node-*` | Direct | Local inference via llama-server on edge nodes | Free |

> **Note:** Model names and routing are fully configurable in `bmas.yaml`. The table above shows an example deployment. Triage classification uses the vLLM container directly (port 8001), not LiteLLM.

## Files

| File | Purpose |
|:---|:---|
| `Dockerfile` | Extends the official LiteLLM image with the bMAS config generator |
| `generate_config.py` | Generates LiteLLM `config.yaml` from `bmas.yaml` at container startup |
| `entrypoint.sh` | Shell entrypoint — runs the generator then starts the LiteLLM proxy |

## Configuration Details

### Router Settings

- **Strategy**: `simple-shuffle` — round-robin across available backends in a model group
- **Retries**: 2 automatic retries per request with 5s backoff
- **Timeout**: 120s per request (accounts for cold starts on edge nodes)
- **`drop_params: true`**: Silently drops unsupported parameters instead of erroring (e.g., `guided_choice` sent to Gemini)

### Resource Limits

- **Memory**: 1 GB
- **CPUs**: 2 cores
- **Network**: Bound to control plane host (LAN only)

## Deployment

```bash
# Start the container (part of root docker-compose.yml)
docker compose up -d litellm

# Check health
curl http://localhost:4000/health/readiness

# View logs
docker compose logs -f litellm

# Test a model route
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "light", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Environment Variables

| Variable | Description |
|:---|:---|
| `GEMINI_API_KEY` | Google AI API key for cloud Gemini models |
| `LITELLM_MASTER_KEY` | Master API key for authenticating requests to LiteLLM |
