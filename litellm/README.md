# LiteLLM — Unified Model Gateway

The centralized AI model router for Stigmergic. Provides a single OpenAI-compatible API endpoint that abstracts all model backends — local edge nodes, the local triage model, and cloud Gemini APIs — behind unified routing, cost tracking, and retry logic.

> Runs as Docker container `bmas-litellm` on the HP OMEN at `192.168.4.240:4000`.

## Architecture

```
bMAS Daemon (:9000)
       │
       │  model="heavy"
       ▼
 ┌───────────┐
 │  LiteLLM  │──── model_group_alias ────▶ gemini-pro   ──▶ Gemini 3.1 Pro (cloud)
 │   :4000   │──── model_group_alias ────▶ gemini-flash ──▶ Gemini 3 Flash (cloud)
 │           │──── model_group_alias ────▶ gemini-flash-lite ▶ Gemini 3.1 Flash Lite (cloud)
 │           │──── model_group_alias ────▶ triage       ──▶ Qwen3-1.7B (local vLLM :8001)
 │           │──── direct ───────────────▶ edge-node-*  ──▶ Gemma 4 E4B (local :8080)
 └───────────┘
```

## Model Routing

The daemon uses simple alias names; LiteLLM resolves them to actual model backends:

| Daemon Calls | LiteLLM Routes To | Backend | Cost |
|:---|:---|:---|:---|
| `heavy` | `gemini-pro` | Gemini 3.1 Pro (cloud API) | $$$ |
| `medium` | `gemini-flash` | Gemini 3 Flash (cloud API) | $$ |
| `light` | `gemini-flash-lite` | Gemini 3.1 Flash Lite (cloud API) | $ |
| `triage` | `triage` | Qwen3-1.7B via vLLM (`:8001`) | Free |
| `edge-node-1` | Direct | Gemma 4 E4B via llama-server (`.102:8080`) | Free |
| `edge-node-2` | Direct | Gemma 4 E4B via llama-server (`.111:8080`) | Free |
| `edge-node-3` | Direct | Gemma 4 E4B via llama-server (`.121:8080`) | Free |

## Files

| File | Purpose |
|:---|:---|
| `config.yaml` | Full LiteLLM configuration — model list, router settings, model group aliases, spend tracking |
| `docker-compose.yml` | Container definition with health check, resource limits, and bind-mounted config |
| `.env` | Gemini API key (`GEMINI_API_KEY`) — **not committed to git** |

## Configuration Details

### Router Settings

- **Strategy**: Cost-based routing — prefers cheaper models when multiple can handle the request
- **Retries**: 2 automatic retries per request with 5s backoff
- **Timeout**: 120s per request (accounts for cold starts on edge nodes)
- **`drop_params: true`**: Silently drops unsupported parameters instead of erroring (e.g., `guided_choice` sent to Gemini)

### Resource Limits

- **Memory**: 1 GB
- **CPUs**: 2 cores
- **Network**: Bound to `192.168.4.240:4000` (LAN only, not exposed externally)

## Deployment

```bash
# Start the container
docker compose up -d

# Check health
curl http://192.168.4.240:4000/health/readiness

# View logs
docker compose logs -f litellm

# Test a model route
curl http://192.168.4.240:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-bmas-master-2026" \
  -H "Content-Type: application/json" \
  -d '{"model": "light", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Environment Variables

| Variable | Description |
|:---|:---|
| `GEMINI_API_KEY` | Google AI API key for cloud Gemini models |
| `LITELLM_MASTER_KEY` | Master API key for authenticating requests to LiteLLM |
