#!/usr/bin/env python3
"""Validate every published bMAS YAML configuration."""

import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DAEMON_SRC = REPO_ROOT / "daemon" / "src"
LITELLM_GENERATOR = REPO_ROOT / "litellm" / "generate_config.py"
sys.path.insert(0, str(DAEMON_SRC))

from config_schema import validate_config_document  # noqa: E402


def config_files() -> list[Path]:
    """Return every public configuration example."""
    return [REPO_ROOT / "bmas.example.yaml", *sorted((REPO_ROOT / "examples").rglob("*.yaml"))]


def validate_semantics(path: Path, raw: dict) -> list[str]:
    """Return semantic errors that the typed schema cannot express."""
    errors: list[str] = []
    nodes = raw.get("nodes", [])
    node_hosts = {str(node.get("host")) for node in nodes}
    if not nodes:
        errors.append("classic requires at least one execution node")

    triage = raw.get("triage", {})
    models = raw.get("models", {})
    if triage.get("enabled", True) and triage.get("backend", "cloud") in {"cloud", "gemini"}:
        model = triage.get("model", "starter-model")
        if model not in models:
            errors.append(f"triage model {model!r} is not in models")

    inference_nodes = [node for node in nodes if node.get("inference")]
    for tier, target in raw.get("routing", {}).items():
        if target == "local" and not inference_nodes:
            errors.append(f"routing.{tier} uses local without an inference node")
        if target != "local" and target not in models:
            errors.append(f"routing.{tier} references unknown model {target!r}")

    for tier, aliases in raw.get("model_pools", {}).items():
        for alias in aliases:
            if alias not in models:
                errors.append(f"model_pools.{tier} references unknown model {alias!r}")

    roles = raw.get("coordination", {}).get("role_registry", {})
    for role, value in roles.items():
        preferred = value.get("preferred_host")
        if preferred and preferred not in node_hosts:
            errors.append(f"role {role!r} prefers unknown host {preferred!r}")
    return errors


def validate_loader(path: Path, raw: dict) -> list[str]:
    """Load a configuration through the daemon's real loader."""
    loader_config = deepcopy(raw)
    temporary_path: Path | None = None
    storage = loader_config.get("storage", {})
    if storage.get("enabled"):
        temporary_dir = tempfile.TemporaryDirectory(prefix="bmas-config-")
        storage["user_media_dir"] = str(Path(temporary_dir.name) / "uploads")
        storage["artifacts_dir"] = str(Path(temporary_dir.name) / "output")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as output:
            yaml.safe_dump(loader_config, output)
            temporary_path = Path(output.name)

    env = os.environ.copy()
    env.update({
        "BMAS_CONFIG": str(temporary_path or path),
        "REDIS_PASSWORD": "test-redis",
        "LITELLM_MASTER_KEY": "sk-test",
        "BMAS_NODE_KEY": "test-node-key",
        "PYTHONPATH": str(DAEMON_SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
            temporary_dir.cleanup()
    errors: list[str] = []
    if result.returncode != 0:
        errors.append(result.stderr.strip() or "daemon loader failed")
    if "WARNING" in result.stderr:
        errors.append("daemon loader emitted a warning: " + result.stderr.strip())
    return errors


def validate_litellm_output(path: Path) -> list[str]:
    """Generate and validate the LiteLLM configuration for one example."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as output:
        output_path = Path(output.name)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(LITELLM_GENERATOR),
                "--config",
                str(path),
                "--output",
                str(output_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return [result.stderr.strip() or "LiteLLM generator failed"]
        generated = yaml.safe_load(output_path.read_text())
    finally:
        output_path.unlink(missing_ok=True)

    errors: list[str] = []
    model_list = generated.get("model_list", []) if isinstance(generated, dict) else []
    if not model_list:
        errors.append("LiteLLM generator produced no models")
    required_costs = {
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_creation_input_token_cost",
        "cache_read_input_token_cost",
    }
    for model in model_list:
        missing = required_costs - set(model.get("model_info", {}))
        if missing:
            errors.append(
                f"LiteLLM model {model.get('model_name')!r} lacks cost fields: "
                + ", ".join(sorted(missing))
            )
    return errors


def main() -> int:
    failures: list[str] = []
    for path in config_files():
        relative = path.relative_to(REPO_ROOT)
        try:
            raw = yaml.safe_load(path.read_text())
            validate_config_document(raw)
            errors = [
                *validate_semantics(path, raw),
                *validate_loader(path, raw),
                *validate_litellm_output(path),
            ]
        except Exception as exc:
            errors = [str(exc)]
        if errors:
            failures.extend(f"{relative}: {error}" for error in errors)
        else:
            print(f"PASS: {relative}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"PASS: {len(config_files())} configuration files are valid and warning-free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
