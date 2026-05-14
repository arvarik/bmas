# /opt/bmas/daemon/config.py
"""
bMAS Daemon configuration — loaded from bmas.yaml at import time.

This module reads the central bmas.yaml config file and exposes all
derived values (URLs, endpoints, routing tables) as module-level
constants. If the config file is missing or malformed, the daemon
crashes immediately with a clear error message.

Secrets (passwords, API keys) are always read from environment variables,
never from the YAML file.

NOTE: We use print() + sys.exit() instead of logging.critical() because
this module executes at import time, before logging.basicConfig() runs
in main.py. All FATAL messages go to stderr for container log capture.
"""

import os
import sys
from typing import Any
import yaml


# ── Helpers ──────────────────────────────────────────────────────────

def _fatal(msg: str, hint: str | None = None) -> None:
    """Print a FATAL error to stderr and exit with code 1."""
    print(f"\n❌ FATAL: {msg}", file=sys.stderr)
    if hint:
        print(f"   ↳ {hint}", file=sys.stderr)
    print("", file=sys.stderr)
    sys.exit(1)


def _warn(msg: str) -> None:
    """Print a WARNING to stderr."""
    print(f"⚠️  WARNING: {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    """Print a success check to stderr."""
    print(f"  ✓ {msg}", file=sys.stderr)


def _require(path: str) -> Any:
    """Walk the config dict by dot-separated path. Crash if missing."""
    parts = path.split(".")
    val: Any = _cfg
    for part in parts:
        if not isinstance(val, dict) or part not in val:
            _fatal(
                f"Missing required config key: '{path}'",
                f"Add '{path}' to {CONFIG_PATH}. See bmas.example.yaml for reference.",
            )
        val = val[part]
    return val


def _require_env(name: str, description: str) -> str:
    """Read a required environment variable. Crash with helpful message if missing."""
    val = os.environ.get(name)
    if not val:
        _fatal(
            f"Missing required environment variable: {name}",
            f"{description}. Set it in .env or your shell environment.",
        )
    return val  # type: ignore[return-value]


# ── Load Config ──────────────────────────────────────────────────────

CONFIG_PATH = os.getenv("BMAS_CONFIG", "/etc/bmas/bmas.yaml")

print("", file=sys.stderr)
print("┌─────────────────────────────────────────────┐", file=sys.stderr)
print("│       bMAS Daemon — Configuration Loader    │", file=sys.stderr)
print("└─────────────────────────────────────────────┘", file=sys.stderr)
print(f"  Config: {CONFIG_PATH}", file=sys.stderr)
print("", file=sys.stderr)

try:
    with open(CONFIG_PATH) as f:
        _cfg = yaml.safe_load(f)
    _ok(f"Config file loaded: {CONFIG_PATH}")
except FileNotFoundError:
    _fatal(
        f"Config file not found: {CONFIG_PATH}",
        "Copy bmas.example.yaml → bmas.yaml and configure your deployment.",
    )
except yaml.YAMLError as e:
    _fatal(f"Invalid YAML in {CONFIG_PATH}: {e}")

# yaml.safe_load returns None for empty files / bare "---" documents
if not isinstance(_cfg, dict):
    _fatal(
        f"{CONFIG_PATH} is empty or not a YAML mapping",
        "The file must contain a top-level YAML mapping (key: value pairs).",
    )


# ── Project ──────────────────────────────────────────────────────────

PROJECT_NAME: str = _require("project.name")
_ok(f"Project: {PROJECT_NAME}")

# ── Control Plane ────────────────────────────────────────────────────

CP_HOST: str = _require("control_plane.host")
CP_PORTS: dict = _require("control_plane.ports")

# Validate required port keys exist and are integers
for _required_port in ("redis", "litellm", "daemon"):
    if _required_port not in CP_PORTS:
        _fatal(
            f"Missing required port: 'control_plane.ports.{_required_port}'",
            f"Add 'ports.{_required_port}' under 'control_plane' in {CONFIG_PATH}.",
        )
    try:
        CP_PORTS[_required_port] = int(CP_PORTS[_required_port])
    except (ValueError, TypeError):
        _fatal(
            f"Invalid port value for 'control_plane.ports.{_required_port}': {CP_PORTS[_required_port]}",
            "Port must be an integer (e.g. 6379).",
        )

_ok(f"Control plane: {CP_HOST} (redis:{CP_PORTS['redis']}, litellm:{CP_PORTS['litellm']}, daemon:{CP_PORTS['daemon']})")

# ── Secrets from Environment ─────────────────────────────────────────

print("", file=sys.stderr)
print("  Checking environment variables...", file=sys.stderr)

REDIS_PASSWORD = _require_env("REDIS_PASSWORD", "Password for Redis authentication")
_ok("REDIS_PASSWORD is set")

LITELLM_KEY = _require_env("LITELLM_MASTER_KEY", "Master key for LiteLLM proxy authentication")
_ok("LITELLM_MASTER_KEY is set")

# ── Derived URLs ─────────────────────────────────────────────────────

REDIS_URL = f"redis://:{REDIS_PASSWORD}@{CP_HOST}:{CP_PORTS['redis']}/0"
LITELLM_URL = f"http://{CP_HOST}:{CP_PORTS['litellm']}/v1"
TRIAGE_URL = f"http://{CP_HOST}:{int(CP_PORTS.get('triage', 8001))}/v1"

# ── Nodes & Agent Endpoints ──────────────────────────────────────────

print("", file=sys.stderr)
print("  Validating agent nodes...", file=sys.stderr)

_nodes = _cfg.get("nodes", [])

# Role-to-agent-URL mapping (for orchestrator dispatch)
AGENT_ENDPOINTS: dict[str, str] = {}
# Role-to-node mapping (for UI colors, inference, etc.)
NODES_BY_ROLE: dict[str, dict] = {}
# Default role colors
_DEFAULT_COLORS = {
    "planner": "#a78bfa",
    "executor": "#5eead4",
    "auditor": "#fbbf24",
}

for node in _nodes:
    role = node.get("role")
    host = node.get("host")
    if not role or not host:
        _fatal(
            f"Each node must have 'role' and 'host'. Got: {node}",
            "Check the 'nodes' section in bmas.yaml. See bmas.example.yaml for format.",
        )
    try:
        port = int(node.get("port", 8000))
    except (ValueError, TypeError):
        _fatal(
            f"Invalid port for node '{role}': {node.get('port')}",
            "Port must be an integer (e.g. 8000).",
        )
    AGENT_ENDPOINTS[role] = f"http://{host}:{port}"
    node.setdefault("color", _DEFAULT_COLORS.get(role, "#94a3b8"))
    NODES_BY_ROLE[role] = node
    has_inference = "+ inference" if node.get("inference") else ""
    _ok(f"Node '{role}' → {host}:{port} {has_inference}")

if not AGENT_ENDPOINTS:
    _warn("No agent nodes configured in bmas.yaml. The daemon cannot dispatch tasks.")

# ── Triage Configuration ────────────────────────────────────────────

print("", file=sys.stderr)
print("  Validating triage configuration...", file=sys.stderr)

_triage = _cfg.get("triage", {})
TRIAGE_ENABLED: bool = _triage.get("enabled", True)
TRIAGE_MODEL: str = _triage.get("model", "Qwen/Qwen3-1.7B")
TRIAGE_GPU_MEMORY: float = _triage.get("gpu_memory_utilization", 0.35)
TRIAGE_MAX_MODEL_LEN: int = _triage.get("max_model_len", 8192)

_VALID_COMPLEXITIES = {"simple", "light", "medium", "complex"}
TRIAGE_DEFAULT_COMPLEXITY: str = _triage.get("default_complexity", "medium")
if TRIAGE_DEFAULT_COMPLEXITY not in _VALID_COMPLEXITIES:
    _fatal(
        f"Invalid triage.default_complexity: '{TRIAGE_DEFAULT_COMPLEXITY}'",
        f"Must be one of: {', '.join(sorted(_VALID_COMPLEXITIES))}.",
    )

if TRIAGE_ENABLED:
    _ok(f"Triage: enabled (model={TRIAGE_MODEL}, fallback={TRIAGE_DEFAULT_COMPLEXITY})")
else:
    _ok(f"Triage: disabled (all tasks route to '{TRIAGE_DEFAULT_COMPLEXITY}' tier)")

# ── Model Routing ────────────────────────────────────────────────────

print("", file=sys.stderr)
print("  Validating model routing...", file=sys.stderr)

_models = _cfg.get("models", {})
_routing = _cfg.get("routing", {})

# The routing table maps complexity enum values to LiteLLM model aliases.
# "local" is a special value that maps to edge inference nodes.
MODEL_ROUTING: dict[str, str] = {}
_inference_nodes = [n for n in _nodes if n.get("inference")]
for tier in ["simple", "light", "medium", "complex"]:
    target = _routing.get(tier)
    if not target:
        _fatal(
            f"Missing routing entry for complexity tier: '{tier}'",
            f"Add 'routing.{tier}' to {CONFIG_PATH}. Value should be a model name from the 'models' section or 'local'.",
        )
    if target == "local":
        if not _inference_nodes:
            _fatal(
                f"routing.{tier} is 'local' but no nodes have an 'inference' block",
                "Add inference settings to at least one node, or route to a cloud model.",
            )
        # Route to the first edge node with inference configured.
        # LiteLLM will load-balance across all edge-node-N aliases.
        MODEL_ROUTING[tier] = "edge-node-1"
        _ok(f"  {tier:>8} → local (edge inference, {len(_inference_nodes)} node(s))")
    else:
        if target not in _models:
            _fatal(
                f"routing.{tier} references model '{target}' which is not defined",
                f"Add '{target}' to the 'models' section in {CONFIG_PATH}, or change routing.{tier}.",
            )
        MODEL_ROUTING[tier] = target
        _ok(f"  {tier:>8} → {target}")

# ── Monitoring ───────────────────────────────────────────────────────

_monitoring = _cfg.get("monitoring", {})
BESZEL_HUB_URL: str | None = _monitoring.get("beszel_hub")

# ── Redlock ──────────────────────────────────────────────────────────

LOCK_TTL_MS = int(os.getenv("LOCK_TTL_MS", "300000"))
LOCK_RETRY_DELAY_MS = int(os.getenv("LOCK_RETRY_DELAY_MS", "200"))

# ── Full Config (for entrypoint scripts that need the raw dict) ──────

RAW_CONFIG = _cfg

# ── Startup Summary ─────────────────────────────────────────────────

print("", file=sys.stderr)
_optional_features = []
if BESZEL_HUB_URL:
    _optional_features.append(f"monitoring={BESZEL_HUB_URL}")
if _inference_nodes:
    _optional_features.append(f"edge_inference={len(_inference_nodes)}_nodes")

print("┌─────────────────────────────────────────────┐", file=sys.stderr)
print(f"│  ✅ {PROJECT_NAME} — Configuration OK        ", file=sys.stderr)
print("└─────────────────────────────────────────────┘", file=sys.stderr)
print(f"  Redis:    {CP_HOST}:{CP_PORTS['redis']}", file=sys.stderr)
print(f"  LiteLLM:  {CP_HOST}:{CP_PORTS['litellm']}", file=sys.stderr)
print(f"  Triage:   {'enabled' if TRIAGE_ENABLED else 'disabled'} ({TRIAGE_MODEL})", file=sys.stderr)
print(f"  Agents:   {', '.join(AGENT_ENDPOINTS.keys()) or 'none'}", file=sys.stderr)
print(f"  Routing:  {' | '.join(f'{k}→{v}' for k, v in MODEL_ROUTING.items())}", file=sys.stderr)
if _optional_features:
    print(f"  Optional: {', '.join(_optional_features)}", file=sys.stderr)
print("", file=sys.stderr)
