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

BMAS_NODE_KEY = _require_env(
    "BMAS_NODE_KEY",
    "Shared bearer secret for node↔daemon auth (doc 03 §4). Generate with: openssl rand -hex 32",
)
_ok("BMAS_NODE_KEY is set")

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

# ── Model Pricing (Phase 0 — daemon-side cost_usd, doc 06 §3.1) ─────

print("", file=sys.stderr)
print("  Checking model pricing...", file=sys.stderr)

MODEL_PRICING: dict[str, dict[str, float | str]] = {}
for _model_alias, _model_cfg in _models.items():
    _pricing = _model_cfg.get("pricing", {})
    if _pricing:
        try:
            _in_cost = float(_pricing.get("input_cost_per_token", 0.0))
            _out_cost = float(_pricing.get("output_cost_per_token", 0.0))
        except (ValueError, TypeError):
            _fatal(
                f"Invalid pricing for model '{_model_alias}'",
                "pricing.input_cost_per_token and pricing.output_cost_per_token must be numbers.",
            )
        MODEL_PRICING[_model_alias] = {
            "input_cost_per_token": _in_cost,
            "output_cost_per_token": _out_cost,
            "source": str(_pricing.get("source", "bmas.yaml")),
        }
        _ok(f"  {_model_alias}: in=${_in_cost:.2e}/tok, out=${_out_cost:.2e}/tok")
    else:
        _warn(f"  {_model_alias}: no pricing — cost_usd will be 0.0 until pricing is set")

# ── Model Pools (Phase 3b — model-pool diversity, doc 05 §2.1) ──────
# Optional: map each complexity tier to a list of model aliases for
# round-robin diversity across generated experts.
# Falls back to [routing.<tier>] when not configured (single-model pool).

_pools_raw = _models.get("pools", {})
MODEL_POOLS: dict[str, list[str]] = {}
if isinstance(_pools_raw, dict):
    for _pool_tier, _pool_list in _pools_raw.items():
        if isinstance(_pool_list, list) and _pool_list:
            MODEL_POOLS[_pool_tier] = [str(m) for m in _pool_list]
if MODEL_POOLS:
    _ok(f"Model pools: {', '.join(f'{k}={len(v)}' for k, v in MODEL_POOLS.items())}")

# ── Coordination (Blackboard Migration, doc 05 §3, doc 10 Phase 0) ───

print("", file=sys.stderr)
print("  Validating coordination config...", file=sys.stderr)

_coordination = _cfg.get("coordination", {})

_VALID_VARIANTS = {"traditional", "patchboard", "stigmergic"}
COORDINATION_VARIANT: str = _coordination.get("variant", "traditional")
if COORDINATION_VARIANT not in _VALID_VARIANTS:
    _fatal(
        f"Invalid coordination.variant: '{COORDINATION_VARIANT}'",
        f"Must be one of: {', '.join(sorted(_VALID_VARIANTS))}.",
    )

BLACKBOARD_V2: bool = bool(_coordination.get("blackboard_v2", False))

VIEW_BUDGET_TOKENS: int = int(_coordination.get("view_budget_tokens", 12000))
if VIEW_BUDGET_TOKENS <= 0:
    _fatal(
        f"coordination.view_budget_tokens must be > 0, got {VIEW_BUDGET_TOKENS}",
    )

_VALID_ROUND_EXECUTION = {"concurrent", "sequential"}
ROUND_EXECUTION: str = _coordination.get("round_execution", "concurrent")
if ROUND_EXECUTION not in _VALID_ROUND_EXECUTION:
    _fatal(
        f"Invalid coordination.round_execution: '{ROUND_EXECUTION}'",
        f"Must be one of: {', '.join(sorted(_VALID_ROUND_EXECUTION))}.",
    )

# Traditional variant sub-config (doc 05 §3)
_trad = _coordination.get("traditional", {})


def _trad_int(key: str, default: int, min_val: int = 1) -> int:
    val = int(_trad.get(key, default))
    if val < min_val:
        _fatal(
            f"coordination.traditional.{key} must be >= {min_val}, got {val}",
        )
    return val


def _trad_float(key: str, default: float, min_val: float = 0.0) -> float:
    val = float(_trad.get(key, default))
    if val <= min_val:
        _fatal(
            f"coordination.traditional.{key} must be > {min_val}, got {val}",
        )
    return val


_VALID_CU_MODES = {"llm", "heuristic_first"}
_VALID_SOLE_SIMILARITY = {"auto", "exact", "embedding", "judge"}

_trad_cu_mode = str(_trad.get("cu_mode", "llm"))
if _trad_cu_mode not in _VALID_CU_MODES:
    _fatal(
        f"Invalid coordination.traditional.cu_mode: '{_trad_cu_mode}'",
        f"Must be one of: {', '.join(sorted(_VALID_CU_MODES))}.",
    )

_trad_sole_sim = str(_trad.get("sole_similarity", "auto"))
if _trad_sole_sim not in _VALID_SOLE_SIMILARITY:
    _fatal(
        f"Invalid coordination.traditional.sole_similarity: '{_trad_sole_sim}'",
        f"Must be one of: {', '.join(sorted(_VALID_SOLE_SIMILARITY))}.",
    )

# Validate experts_per_tier shape
_experts_raw = _trad.get("experts_per_tier", {"simple": 0, "light": 1, "medium": 2, "complex": 3})
if not isinstance(_experts_raw, dict):
    _fatal(
        "coordination.traditional.experts_per_tier must be a mapping",
        'Expected: { simple: 0, light: 1, medium: 2, complex: 3 }',
    )
for _tier_key in ("simple", "light", "medium", "complex"):
    if _tier_key not in _experts_raw:
        _fatal(
            f"coordination.traditional.experts_per_tier missing key: '{_tier_key}'",
            'Required keys: simple, light, medium, complex',
        )
    try:
        int(_experts_raw[_tier_key])
    except (ValueError, TypeError):
        _fatal(
            f"coordination.traditional.experts_per_tier.{_tier_key} must be an integer",
        )

TRADITIONAL_CONFIG: dict[str, object] = {
    "max_rounds": _trad_int("max_rounds", 4),
    "max_duration_s": _trad_int("max_duration_s", 1800),
    "budget_ceiling_usd": _trad_float("budget_ceiling_usd", 0.50),
    "max_concurrent_activations": _trad_int("max_concurrent_activations", 3),
    "experts_per_tier": {k: int(v) for k, v in _experts_raw.items()},
    "cleaner_entry_threshold": _trad_int("cleaner_entry_threshold", 12),
    "stall_rounds": _trad_int("stall_rounds", 2),
    "cu_mode": _trad_cu_mode,
    "coordinator_narration": bool(_trad.get("coordinator_narration", False)),
    "sole_similarity": _trad_sole_sim,
}

# ── Board Config (Phase 2, doc 04 §4, §7) ────────────────────────────

_board = _coordination.get("board", {})

MAX_ENTRY_CHARS: int = int(_board.get("max_entry_chars", 8000))
if MAX_ENTRY_CHARS <= 0:
    _fatal(f"coordination.board.max_entry_chars must be > 0, got {MAX_ENTRY_CHARS}")

MAX_TITLE_LEN: int = int(_board.get("max_title_len", 200))
if MAX_TITLE_LEN <= 0:
    _fatal(f"coordination.board.max_title_len must be > 0, got {MAX_TITLE_LEN}")

# Salience weights (doc 04 §7)
_sal_weights = _board.get("salience_weights", {})
SALIENCE_W_C: float = float(_sal_weights.get("confidence", 0.4))
SALIENCE_W_R: float = float(_sal_weights.get("recency", 0.2))
SALIENCE_W_X: float = float(_sal_weights.get("refs_in", 0.3))
SALIENCE_W_P: float = float(_sal_weights.get("penalty", 0.3))

_ok(f"Coordination: variant={COORDINATION_VARIANT}, bb_v2={BLACKBOARD_V2}, round_exec={ROUND_EXECUTION}")
_ok(f"Board: max_entry={MAX_ENTRY_CHARS}, max_title={MAX_TITLE_LEN}, salience_w=[{SALIENCE_W_C},{SALIENCE_W_R},{SALIENCE_W_X},{SALIENCE_W_P}]")

# ── Role Registry (Phase 3a, doc 12 §2.5) ────────────────────────────
# Maps each blackboard role to its Hermes profile, preferred host, and
# dispatch endpoints (preferred first, then all other nodes as fallback).

_role_reg = _coordination.get("role_registry", {})
_node_hosts = {n["host"] for n in _nodes if n.get("host")}

ROLE_REGISTRY: dict[str, dict] = {}

if _role_reg:
    print("", file=sys.stderr)
    print("  Validating role registry (doc 12 §2.5)...", file=sys.stderr)
    for _role_name, _role_cfg in _role_reg.items():
        if not isinstance(_role_cfg, dict):
            _warn(f"role_registry.{_role_name}: expected a mapping, got {type(_role_cfg).__name__}; skipped")
            continue

        _pref = _role_cfg.get("preferred_host")
        if _pref and _pref not in _node_hosts:
            _warn(
                f"role_registry.{_role_name}.preferred_host '{_pref}' "
                f"is not a configured node host ({', '.join(sorted(_node_hosts))})"
            )

        _port = int(_role_cfg.get("dispatch_port", 8000))
        _profile = str(_role_cfg.get("profile", _role_name))

        # Build endpoint list: preferred first, then all other hosts as fallback
        _endpoints: list[str] = []
        if _pref:
            _endpoints.append(f"http://{_pref}:{_port}")
        for _n in _nodes:
            _ep = f"http://{_n['host']}:{_port}"
            if _ep not in _endpoints:
                _endpoints.append(_ep)

        ROLE_REGISTRY[_role_name] = {
            "preferred_host": _pref,
            "profile": _profile,
            "dispatch_port": _port,
            "endpoints": _endpoints,
        }
        _ok(
            f"Role '{_role_name}' → profile={_profile}, "
            f"home={_pref or 'any'}, endpoints={len(_endpoints)}"
        )

# ── Storage (Files & Artifacts, doc 17 §2) ───────────────────────────

print("", file=sys.stderr)
print("  Validating storage config...", file=sys.stderr)

_storage = _cfg.get("storage", {})
STORAGE_ENABLED: bool = bool(_storage.get("enabled", False))

_VALID_PDF_EXTRACTION = {"pymupdf", "pypdf", "off"}

STORAGE_CONFIG: dict[str, object] = {
    "enabled": STORAGE_ENABLED,
    "user_media_dir": str(_storage.get("user_media_dir", "/opt/bmas-data/uploads")),
    "artifacts_dir": str(_storage.get("artifacts_dir", "/opt/output")),
    "max_upload_mb": int(_storage.get("max_upload_mb", 50)),
    "max_task_output_mb": int(_storage.get("max_task_output_mb", 500)),
    "allowed_upload_types": list(_storage.get("allowed_upload_types",
                                              ["pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"])),
    "pdf_extraction": str(_storage.get("pdf_extraction", "pymupdf")),
    "extraction_max_chars": int(_storage.get("extraction_max_chars", 60000)),
}

if str(STORAGE_CONFIG["pdf_extraction"]) not in _VALID_PDF_EXTRACTION:
    _fatal(
        f"Invalid storage.pdf_extraction: '{STORAGE_CONFIG['pdf_extraction']}'",
        f"Must be one of: {', '.join(sorted(_VALID_PDF_EXTRACTION))}.",
    )

if STORAGE_ENABLED:
    # Startup directory-writability checks (doc 17 §2)
    for _dir_key in ("user_media_dir", "artifacts_dir"):
        _dir_path = str(STORAGE_CONFIG[_dir_key])
        if not os.path.isdir(_dir_path):
            try:
                os.makedirs(_dir_path, exist_ok=True)
                _ok(f"Created storage directory: {_dir_path}")
            except OSError as e:
                _fatal(
                    f"Cannot create storage.{_dir_key} directory: {_dir_path}",
                    f"Error: {e}. Create the directory manually or check permissions.",
                )
        if not os.access(_dir_path, os.W_OK):
            _fatal(
                f"storage.{_dir_key} is not writable: {_dir_path}",
                "Check permissions or reconfigure the path.",
            )
        _ok(f"storage.{_dir_key}: {_dir_path} (writable)")
    _ok(f"Storage: enabled (upload_cap={STORAGE_CONFIG['max_upload_mb']}MB, output_cap={STORAGE_CONFIG['max_task_output_mb']}MB)")
else:
    _ok("Storage: disabled (no file/artifact support until enabled)")

# Export individual storage constants for route modules (doc 17 §3, §6)
STORAGE_USER_MEDIA_DIR: str = str(STORAGE_CONFIG["user_media_dir"])
STORAGE_ARTIFACTS_DIR: str = str(STORAGE_CONFIG["artifacts_dir"])
STORAGE_MAX_UPLOAD_MB: int = int(STORAGE_CONFIG["max_upload_mb"])  # type: ignore[call-overload]
STORAGE_MAX_TASK_OUTPUT_MB: int = int(STORAGE_CONFIG["max_task_output_mb"])  # type: ignore[call-overload]
STORAGE_ALLOWED_TYPES: set[str] = set(STORAGE_CONFIG["allowed_upload_types"])  # type: ignore[call-overload]
STORAGE_PDF_EXTRACTION: str = str(STORAGE_CONFIG["pdf_extraction"])
STORAGE_EXTRACTION_MAX_CHARS: int = int(STORAGE_CONFIG["extraction_max_chars"])  # type: ignore[call-overload]

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
print(f"  Variant:  {COORDINATION_VARIANT} (bb_v2={'on' if BLACKBOARD_V2 else 'off'})", file=sys.stderr)
print(f"  Storage:  {'enabled' if STORAGE_ENABLED else 'disabled'}", file=sys.stderr)
print(f"  Roles:    {len(ROLE_REGISTRY)} registered" if ROLE_REGISTRY else "  Roles:    none", file=sys.stderr)
print(f"  Pricing:  {len(MODEL_PRICING)}/{len(_models)} models configured", file=sys.stderr)
if _optional_features:
    print(f"  Optional: {', '.join(_optional_features)}", file=sys.stderr)
print("", file=sys.stderr)
