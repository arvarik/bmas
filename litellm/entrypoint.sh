#!/bin/sh
# ─────────────────────────────────────────────────────────────────────
# LiteLLM Entrypoint — generates config.yaml from bmas.yaml
# ─────────────────────────────────────────────────────────────────────
# This script reads the bMAS config file and dynamically generates
# a LiteLLM-native config.yaml, then starts the LiteLLM proxy.
# ─────────────────────────────────────────────────────────────────────

set -e

BMAS_CONFIG="${BMAS_CONFIG:-/etc/bmas/bmas.yaml}"
OUTPUT="/tmp/litellm_config.yaml"

if [ ! -f "$BMAS_CONFIG" ]; then
  echo "FATAL: bmas.yaml not found at $BMAS_CONFIG"
  exit 1
fi

# Use Python (available in the LiteLLM image) to parse YAML and generate config
python3 - "$BMAS_CONFIG" "$OUTPUT" << 'PYTHON_SCRIPT'
import sys
import yaml
import json

bmas_config_path = sys.argv[1]
output_path = sys.argv[2]

with open(bmas_config_path) as f:
    cfg = yaml.safe_load(f)

model_list = []
cp = cfg.get("control_plane", {})
cp_host = cp.get("host", "localhost")

# ── Cloud / External Models ──────────────────────────────────────────
models = cfg.get("models", {})
for alias, model_cfg in models.items():
    provider = model_cfg["provider"]
    model_name = model_cfg["model"]
    api_key_env = model_cfg.get("api_key_env", "")
    max_tokens = model_cfg.get("max_tokens", 4096)

    entry = {
        "model_name": alias,
        "litellm_params": {
            "model": f"{provider}/{model_name}",
            "max_tokens": max_tokens,
        },
    }
    if api_key_env:
        entry["litellm_params"]["api_key"] = f"os.environ/{api_key_env}"

    model_list.append(entry)

# ── Edge Inference Nodes ─────────────────────────────────────────────
nodes = cfg.get("nodes", [])
for i, node in enumerate(nodes, 1):
    inference = node.get("inference")
    if not inference:
        continue

    inf_host = inference["host"]
    inf_port = inference.get("port", 8080)
    inf_model = inference.get("model", "local-model")

    model_list.append({
        "model_name": f"edge-node-{i}",
        "litellm_params": {
            "model": f"openai/{inf_model}",
            "api_base": f"http://{inf_host}:{inf_port}/v1",
            "api_key": "not-needed",
            "max_tokens": 2048,
        },
        "model_info": {
            "description": f"Edge node {node.get('name', i)} ({node.get('role', 'unknown')})",
        },
    })

# ── Triage Model (local vLLM) ───────────────────────────────────────
triage = cfg.get("triage", {})
if triage.get("enabled", True):
    triage_port = cp.get("ports", {}).get("triage", 8001)
    triage_model = triage.get("model", "Qwen/Qwen3-1.7B")
    model_list.append({
        "model_name": "triage",
        "litellm_params": {
            "model": f"openai/{triage_model}",
            "api_base": f"http://bmas-triage:8000/v1",
            "api_key": "not-needed",
            "max_tokens": 1024,
            "temperature": 0.1,
        },
    })

# ── Routing Aliases ──────────────────────────────────────────────────
routing = cfg.get("routing", {})
model_group_alias = {}
for tier, target in routing.items():
    if target == "local":
        # Route to edge-node-1 (LiteLLM will load-balance if multiple exist)
        model_group_alias[tier] = "edge-node-1"
    else:
        model_group_alias[tier] = target

# ── Assemble Final Config ────────────────────────────────────────────
litellm_config = {
    "model_list": model_list,
    "router_settings": {
        "routing_strategy": "simple-shuffle",
        "num_retries": 2,
        "timeout": 120,
        "model_group_alias": model_group_alias,
    },
    "general_settings": {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
    },
    "litellm_settings": {
        "drop_params": True,
    },
}

with open(output_path, "w") as f:
    yaml.dump(litellm_config, f, default_flow_style=False, sort_keys=False)

print(f"Generated LiteLLM config at {output_path}")
print(f"  Models: {len(model_list)}")
print(f"  Routing aliases: {model_group_alias}")
PYTHON_SCRIPT

echo "Starting LiteLLM proxy..."
exec litellm --config "$OUTPUT" --port 4000
