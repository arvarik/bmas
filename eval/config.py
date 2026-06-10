"""Eval-specific configuration — reads bmas.yaml for daemon URL and model pricing.

This is a lightweight reader that extracts only the fields the eval harness needs.
It does NOT import daemon.src.config (which has side-effects at import time and
requires the full daemon environment).
"""

import os
import sys
from pathlib import Path

import yaml


def _find_config() -> str:
    """Locate bmas.yaml by walking up from this file or using BMAS_CONFIG env."""
    env = os.getenv("BMAS_CONFIG")
    if env and os.path.isfile(env):
        return env

    # Walk up from eval/ to repo root
    candidate = Path(__file__).resolve().parent.parent / "bmas.yaml"
    if candidate.is_file():
        return str(candidate)

    # Docker / system path
    system_path = "/etc/bmas/bmas.yaml"
    if os.path.isfile(system_path):
        return system_path

    print(
        "❌ FATAL: Cannot locate bmas.yaml. "
        "Set BMAS_CONFIG or place bmas.yaml in the repo root.",
        file=sys.stderr,
    )
    sys.exit(1)


def load_eval_config(config_path: str | None = None) -> dict:
    """Load and return the eval-relevant subset of bmas.yaml.

    Returns a dict with keys:
      - daemon_url: str
      - coordination_variant: str
      - model_pricing: dict[str, dict]  (model_alias -> {input_cost_per_token, output_cost_per_token})
      - coordination: dict  (full coordination block snapshot)
      - nodes: list[dict]
    """
    path = config_path or _find_config()
    with open(path) as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        print(f"❌ FATAL: {path} is empty or not a YAML mapping", file=sys.stderr)
        sys.exit(1)

    # Daemon URL
    cp = cfg.get("control_plane", {})
    cp_host = cp.get("host", "127.0.0.1")
    cp_ports = cp.get("ports", {})
    daemon_port = int(cp_ports.get("daemon", 9000))
    daemon_url = f"http://{cp_host}:{daemon_port}"

    # Model pricing
    models = cfg.get("models", {})
    model_pricing: dict[str, dict] = {}
    for alias, model_cfg in models.items():
        pricing = model_cfg.get("pricing")
        if pricing:
            model_pricing[alias] = {
                "input_cost_per_token": float(pricing.get("input_cost_per_token", 0)),
                "output_cost_per_token": float(pricing.get("output_cost_per_token", 0)),
            }

    # Coordination block
    coordination = cfg.get("coordination", {})
    variant = coordination.get("variant", "legacy_pipeline")

    # Nodes
    nodes = cfg.get("nodes", [])

    return {
        "daemon_url": daemon_url,
        "coordination_variant": variant,
        "model_pricing": model_pricing,
        "coordination": coordination,
        "nodes": nodes,
    }
