# Triage — Semantic Complexity Classifier

The cost-optimization gatekeeper for Stigmergic. Before any paid API call is made, the triage router classifies each task's complexity and routes it to the cheapest model capable of handling it.

> Runs as Docker container `bmas-triage` on the HP OMEN's RTX 5060 Ti at `192.168.4.240:8001`, using vLLM with Qwen3-1.7B.

## How It Works

```
User Task
    │
    ▼
┌──────────────────┐
│  Qwen3-1.7B      │  ← bfloat16 on RTX 5060 Ti (16 GB VRAM)
│  + guided_choice │  ← vLLM constrains output to valid tier labels
│  + /no_think     │  ← Disables Qwen3's thinking mode for speed
└────────┬─────────┘
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

## Files

| File | Purpose |
|:---|:---|
| `docker-compose.yml` | vLLM container definition with NVIDIA GPU reservation, model config, and resource limits |
| `client.py` | Standalone HTTP client for the vLLM server. Handles request construction, retry logic, `<think>` tag stripping, and response parsing. Zero external dependencies (stdlib only). |
| `test_triage.py` | Evaluation suite runner. Classifies all test cases, prints a rich CLI table, computes accuracy/confusion matrix/per-tier F1, and optionally exports JSON results. |
| `test_cases.py` | 117 curated test cases across all 4 tiers with ground truth labels. Covers 9 complexity dimensions including distractor susceptibility and input length variation. |
| `report.py` | Metrics computation and CLI formatting. Confusion matrix, per-tier precision/recall/F1, latency percentiles, and ANSI-colored output. |
| `.env` | Hugging Face token (`HF_TOKEN`) for model downloads — **not committed to git** |

## Evaluation Suite

The test suite validates classification accuracy against 117 hand-labeled cases:

```bash
# Run evaluation (default endpoint)
python3 test_triage.py

# Custom endpoint
python3 test_triage.py --url http://192.168.4.240:8001

# Skip warmup (CUDA graph compilation)
python3 test_triage.py --no-warmup

# Export results to JSON
python3 test_triage.py --export
```

### Test Case Distribution

| Tier | Count | Coverage |
|:---|:---|:---|
| SIMPLE | 33 | Factual lookups, arithmetic, string ops, distractor-laden prompts |
| LIGHT | 25 | Extraction, summaries, patterns, translations, long input + light task |
| MEDIUM | 31 | Coding tasks, technical explanations, debugging, DevOps artifacts |
| COMPLEX | 28 | System architecture, full applications, research synthesis, cross-domain |

### Output

The suite produces:
- Per-case pass/fail table with latency and throughput
- Confusion matrix (expected vs. predicted)
- Per-tier precision, recall, and F1 scores
- Weighted F1 across all tiers
- Latency percentiles (p50, p90, p95, p99)
- Misclassification details

## Key Design Decisions

| Decision | Rationale |
|:---|:---|
| **Qwen3-1.7B** | Best-in-class instruction following for sub-2B models. Small enough to share the GPU with other workloads. |
| **`guided_choice`** | vLLM's constrained decoding guarantees the output is a valid tier label — no post-processing failures |
| **`/no_think`** | Qwen3's thinking mode adds latency without improving classification accuracy |
| **MEDIUM fallback** | If label extraction fails, default to MEDIUM — never under-routes to cheaper models |
| **`gpu-memory-utilization 0.35`** | Leaves 65% of RTX 5060 Ti VRAM for other workloads (Trebek WhisperX, future models) |

## Deployment

```bash
# Start the container
docker compose up -d

# Check if model is loaded
curl http://192.168.4.240:8001/v1/models

# Test a classification
curl http://192.168.4.240:8001/v1/chat/completions \
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

# View logs
docker compose logs -f vllm-triage
```

## Resource Limits

- **Memory**: 12 GB
- **CPUs**: 4 cores
- **GPU**: 1× NVIDIA RTX 5060 Ti (35% VRAM utilization ≈ 5.6 GB)
- **Model**: Qwen3-1.7B bfloat16, 8192 max context length
