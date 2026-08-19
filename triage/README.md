# Triage

Triage classifies each task as `simple`, `light`, `medium`, or `complex`.

The daemon uses that tier to select one entry from the `routing` section.

## Runtime backends

| Backend | GPU | Behavior |
|:---|:---|:---|
| `cloud` | No | Sends a short classification request through a configured LiteLLM alias. |
| `local` | NVIDIA | Sends a constrained request to the optional vLLM service. |

Set the backend in `bmas.yaml`.

```yaml
triage:
  enabled: true
  backend: cloud
  model: starter-model
  default_complexity: medium
```

The cloud backend can use any provider alias from `models`. The name `cloud` does not require Gemini.

Disable triage to use one fixed tier.

```yaml
triage:
  enabled: false
  backend: cloud
  default_complexity: medium
```

## Local backend

The local backend uses the optional Compose `gpu` profile.

```yaml
triage:
  enabled: true
  backend: local
  local_model: Qwen/Qwen3-1.7B
  gpu_memory_utilization: 0.35
  max_model_len: 8192
  default_complexity: medium
```

Set `HF_TOKEN` in `.env` when the selected model requires it.

Start the profile:

```bash
docker compose --profile gpu up -d
./scripts/bmas doctor --wait 180
```

The local service appears at port `8001` by default. Docker keeps that port on the loopback address unless `BMAS_BIND_ADDRESS` changes it.

## Failure behavior

The classifier extracts the first valid tier from the model result. It uses `default_complexity` when classification fails.

The local backend uses constrained output when vLLM supports the configured request. The cloud backend validates free-form text.

## Evaluation files

The `eval` directory contains 117 labeled tasks and a standalone test client.

| File | Purpose |
|:---|:---|
| `eval/cases.py` | Defines the labeled task set. |
| `eval/run.py` | Runs the classifier against the task set. |
| `eval/report.py` | Calculates accuracy and per-tier measures. |
| `src/client.py` | Sends local or Gemini-specific evaluation requests. |

The evaluation client's `gemini` backend name applies only to that test client. The main `bmas.yaml` field uses `cloud`.

Run the local evaluation after the GPU service becomes ready.

```bash
cd triage
python3 -m eval.run --url http://127.0.0.1:8001
```

Check the command help before you change evaluation flags.

## Related configuration

Read [Configuration](../docs/CONFIGURATION.md) for all triage and routing fields.
