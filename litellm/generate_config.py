#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
# LiteLLM Config Generator — produces config.yaml from bmas.yaml
# ─────────────────────────────────────────────────────────────────────
# Reads the bMAS configuration file and generates a LiteLLM-native
# config.yaml with model definitions, routing aliases, and settings.
#
# Usage:
#   python3 generate_config.py --config /etc/bmas/bmas.yaml --output /tmp/litellm_config.yaml
# ─────────────────────────────────────────────────────────────────────

import argparse
import os
import sys

import yaml


def parse_cloud_models(cfg: dict) -> list[dict]:
    """Parse cloud/external model definitions from bmas.yaml."""
    model_list = []
    models = cfg.get("models", {})

    for alias, model_cfg in models.items():
        provider = model_cfg["provider"]
        model_name = model_cfg["model"]
        api_key_env = model_cfg.get("api_key_env", "")
        max_tokens = model_cfg.get("max_tokens", 4096)

        api_base = model_cfg.get("api_base", "")
        entry = {
            "model_name": alias,
            "litellm_params": {
                "model": f"{provider}/{model_name}",
                "max_tokens": max_tokens,
            },
        }
        if api_key_env:
            entry["litellm_params"]["api_key"] = f"os.environ/{api_key_env}"
        if api_base:
            entry["litellm_params"]["api_base"] = api_base

        model_list.append(entry)

    return model_list


def parse_edge_nodes(cfg: dict) -> list[dict]:
    """Parse edge inference node definitions from bmas.yaml.

    Each node is registered under two LiteLLM model_name entries:
      1. "edge-node-{i}" — individual alias for direct targeting
      2. "edge-local"    — shared group for round-robin load balancing
    LiteLLM's router load-balances across all deployments that share
    the same model_name, so multiple "edge-local" entries enable
    automatic distribution of inference calls.
    """
    model_list = []
    nodes = cfg.get("nodes", [])

    for i, node in enumerate(nodes, 1):
        inference = node.get("inference")
        if not inference:
            continue

        inf_host = inference["host"]
        inf_port = inference.get("port", 8080)
        inf_model = inference.get("model", "local-model")

        litellm_params = {
            "model": f"openai/{inf_model}",
            "api_base": f"http://{inf_host}:{inf_port}/v1",
            "api_key": "not-needed",
            "max_tokens": inference.get("max_tokens", 65536),
        }
        model_info = {
            "description": f"Edge node {node.get('name', i)} ({node.get('role', 'unknown')})",
        }

        # Individual alias (for direct targeting / debugging)
        model_list.append({
            "model_name": f"edge-node-{i}",
            "litellm_params": dict(litellm_params),
            "model_info": dict(model_info),
        })

        # Shared group alias (for load-balanced routing)
        model_list.append({
            "model_name": "edge-local",
            "litellm_params": dict(litellm_params),
            "model_info": dict(model_info),
        })

    return model_list


def parse_triage_model(cfg: dict) -> list[dict]:
    """Parse the local triage (vLLM) model if enabled and using local backend.

    When backend is 'gemini', triage requests go through an existing
    cloud model alias — no special LiteLLM entry needed.
    """
    triage = cfg.get("triage", {})
    if not triage.get("enabled", True):
        return []

    backend = triage.get("backend", "gemini")
    if backend != "local":
        return []  # Gemini backend uses existing cloud model alias

    triage_model = triage.get("local_model", "Qwen/Qwen3-1.7B")

    return [{
        "model_name": "triage",
        "litellm_params": {
            "model": f"openai/{triage_model}",
            "api_base": "http://bmas-triage:8000/v1",
            "api_key": "not-needed",
            "max_tokens": 1024,
            "temperature": 0.1,
        },
    }]


def build_routing_aliases(cfg: dict) -> dict:
    """Map routing tier names to model group names."""
    routing = cfg.get("routing", {})
    aliases = {}

    for tier, target in routing.items():
        if target == "local":
            # Route to the shared edge-local group — LiteLLM will
            # round-robin across all nodes registered under this name.
            aliases[tier] = "edge-local"
        else:
            aliases[tier] = target

    return aliases


def generate_config(bmas_config_path: str) -> dict:
    """Generate a complete LiteLLM config dict from a bmas.yaml file."""
    with open(bmas_config_path) as f:
        cfg = yaml.safe_load(f)

    model_list = []
    model_list.extend(parse_cloud_models(cfg))
    model_list.extend(parse_edge_nodes(cfg))
    model_list.extend(parse_triage_model(cfg))

    model_group_alias = build_routing_aliases(cfg)

    return {
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


def main():
    parser = argparse.ArgumentParser(
        description="Generate LiteLLM config.yaml from bmas.yaml"
    )
    parser.add_argument(
        "--config",
        default="/etc/bmas/bmas.yaml",
        help="Path to bmas.yaml (default: /etc/bmas/bmas.yaml)",
    )
    parser.add_argument(
        "--output",
        default="/tmp/litellm_config.yaml",
        help="Output path for generated config (default: /tmp/litellm_config.yaml)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        print(f"FATAL: bmas.yaml not found at {args.config}", file=sys.stderr)
        sys.exit(1)

    litellm_config = generate_config(args.config)

    with open(args.output, "w") as f:
        yaml.dump(litellm_config, f, default_flow_style=False, sort_keys=False)

    model_count = len(litellm_config["model_list"])
    aliases = litellm_config["router_settings"]["model_group_alias"]
    print(f"Generated LiteLLM config at {args.output}")
    print(f"  Models: {model_count}")
    print(f"  Routing aliases: {aliases}")


if __name__ == "__main__":
    main()
