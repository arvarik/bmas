# Triage — Semantic Complexity Classifier

The cost-optimization gatekeeper for bMAS. Before any paid API call is made, the triage router classifies each task's complexity and routes it to the cheapest model capable of handling it.

## Backends

Triage supports two classification backends, configured via `triage.backend` in `bmas.yaml`:

| Backend | Default | GPU Required | Description |
|:---|:---|:---|:---|
| **`gemini`** | ✅ Yes | No | Routes classification through LiteLLM → Gemini Flash Lite API. Zero infrastructure overhead — just needs a `GEMINI_API_KEY`. |
| **`local`** | No | Yes (NVIDIA) | Runs Qwen3-1.7B on a local vLLM server with constrained decoding. Free after setup, ~50ms latency, but requires GPU + Docker `--profile gpu`. |

## How It Works

```
User Task
    │
    ▼
┌───────────────────────────────────────┐
│  Backend: gemini (default)            │
│  Gemini 3.1 Flash Lite via LiteLLM   │
│  ─── OR ───                           │
│  Backend: local                       │
│  Qwen3-1.7B + guided_choice (vLLM)   │
└────────────┬──────────────────────────┘
             │
        ┌────┴────┐
        │  TIER   │
        └────┬────┘
             │
        ┌────▼──────────────────────────────────────┐
        │ SIMPLE  → edge-node (Gemma 4B, $0)        │
        │ LIGHT   → Gemini Flash Lite (cheap cloud)  │
        │ MEDIUM  → Gemini Flash ($$)                │
        │ COMPLEX → Gemini Pro ($$$)                 │
        └───────────────────────────────────────────┘
```

## Tier Definitions

| Tier | Description | Model Target | Cost |
|:---|:---|:---|:---|
| **SIMPLE** | Factual lookups, arithmetic, formatting, single-step operations | Gemma 4 E4B (edge, local) | $0 |
| **LIGHT** | Extraction, short summaries, pattern generation, translations | Gemini 3.1 Flash Lite | ~$0.01/1K tokens |
| **MEDIUM** | Single-function coding, technical explanations, document drafting | Gemini 3 Flash | ~$0.03/1K tokens |
| **COMPLEX** | System architecture, full applications, research synthesis | Gemini 3.1 Pro | ~$0.10/1K tokens |

## Setup

### Gemini Backend (Default)

No additional setup needed beyond the standard bMAS deployment. The Gemini backend uses the existing LiteLLM proxy and `GEMINI_API_KEY` (already required for model routing).

```yaml
# bmas.yaml
triage:
  enabled: true
  backend: gemini                    # Cloud API, no GPU needed
  model: "gemini-flash-lite"         # LiteLLM model alias
  default_complexity: medium
```

```bash
# Start without GPU profile — triage uses Gemini API via LiteLLM
docker compose up -d
```

### Local Backend (Qwen3-1.7B on vLLM)

The local backend runs a Qwen3-1.7B model on a local GPU via vLLM. This eliminates per-classification API costs and provides ~50ms latency, but requires:

- **NVIDIA GPU** with ≥6 GB VRAM (tested on RTX 5060 Ti 16 GB)
- **NVIDIA Container Toolkit** installed
- **HF_TOKEN** in `.env` (for downloading the gated Qwen3 model from Hugging Face)

#### Configuration

```yaml
# bmas.yaml
triage:
  enabled: true
  backend: local                     # Local vLLM server, requires GPU
  model: "gemini-flash-lite"         # Ignored when backend: local
  default_complexity: medium
  # ── Local backend options ──
  local_model: "Qwen/Qwen3-1.7B"
  gpu_memory_utilization: 0.35       # Fraction of GPU VRAM to use (0.35 ≈ 5.6 GB on 16 GB)
  max_model_len: 8192                # Max context length
```

```bash
# .env — add your Hugging Face token
HF_TOKEN=hf_your_token_here
```

#### Starting

```bash
# Start with GPU profile to enable the vLLM triage container
docker compose --profile gpu up -d

# Verify the model is loaded
curl http://<CONTROL_PLANE_HOST>:8001/v1/models

# Test a classification
curl http://<CONTROL_PLANE_HOST>:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-1.7B",
    "messages": [
      {"role": "system", "content": "Classify task complexity. Respond with SIMPLE, LIGHT, MEDIUM, or COMPLEX. /no_think"},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "max_tokens": 10,
    "guided_choice": ["SIMPLE", "LIGHT", "MEDIUM", "COMPLEX"]
  }'
```

#### Resource Limits (Local Backend)

- **Memory**: 12 GB
- **CPUs**: 4 cores
- **GPU**: 1× NVIDIA GPU (35% VRAM utilization by default)
- **Model**: Qwen3-1.7B bfloat16, 8192 max context length

## Files

| File | Purpose |
|:---|:---|
| `docker-compose.yml` | Triage container defined in the root `docker-compose.yml` under the `gpu` profile (local backend only) |
| `src/client.py` | Standalone HTTP client for testing. Supports both `local` and `gemini` backends. Zero external dependencies (stdlib only). |
| `eval/run.py` | Evaluation suite runner. Classifies all test cases, prints a rich CLI table, computes accuracy/confusion matrix/per-tier F1, and optionally exports JSON results. |
| `eval/cases.py` | 117 curated test cases across all 4 tiers with ground truth labels. Covers 9 complexity dimensions including distractor susceptibility and input length variation. |
| `eval/report.py` | Metrics computation and CLI formatting. Confusion matrix, per-tier precision/recall/F1, latency percentiles, and ANSI-colored output. |
| `.env` | Hugging Face token (`HF_TOKEN`) for local backend model downloads — **not committed to git** |

## Evaluation Suite

The test suite validates classification accuracy against 117 hand-labeled cases:

```bash
# Run evaluation against local vLLM (default)
python3 -m eval.run

# Run against local vLLM on custom endpoint
python3 -m eval.run --url http://<CONTROL_PLANE_HOST>:8001

# Skip warmup (CUDA graph compilation)
python3 -m eval.run --no-warmup

# Export results to JSON
python3 -m eval.run --export
```

### Test Case Distribution

| Tier | Count | Coverage |
|:---|:---|:---|
| SIMPLE | 33 | Factual lookups, arithmetic, string ops, distractor-laden prompts |
| LIGHT | 25 | Extraction, summaries, patterns, translations, long input + light task |
| MEDIUM | 31 | Coding tasks, technical explanations, debugging, DevOps artifacts |
| COMPLEX | 28 | System architecture, full applications, research synthesis, cross-domain |

## Key Design Decisions

| Decision | Rationale |
|:---|:---|
| **Gemini default** | Eliminates GPU requirement for the default deployment. Most users already have a `GEMINI_API_KEY` configured for model routing. |
| **Qwen3-1.7B (local)** | Best-in-class instruction following for sub-2B models. Small enough to share the GPU with other workloads. |
| **`guided_choice` (local only)** | vLLM's constrained decoding guarantees the output is a valid tier label — no post-processing failures |
| **`/no_think`** | Qwen3's thinking mode adds latency without improving classification accuracy |
| **MEDIUM fallback** | If label extraction fails, default to MEDIUM — never under-routes to cheaper models |
| **Shared system prompt** | Both backends use the identical prompt and label extraction, ensuring consistent classification behavior |
