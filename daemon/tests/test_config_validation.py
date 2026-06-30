"""Tests for Phase 0 config validation (coordination, storage, pricing, BMAS_NODE_KEY).

These tests exercise the config validation logic by loading bmas.yaml
with controlled modifications and checking that the expected constants
are exposed.  Since config.py runs at import time with sys.exit() on
error, we test by spawning subprocesses that import config and report
results.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap

import yaml

# ── Helpers ──────────────────────────────────────────────────────────

# daemon/tests/ → daemon/ → /opt/bmas (repo root)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DAEMON_DIR = os.path.join(REPO_ROOT, "daemon")
SRC_DIR = os.path.join(DAEMON_DIR, "src")
# bmas.yaml is gitignored; fall back to bmas.example.yaml on CI
_bmas_yaml = os.path.join(REPO_ROOT, "bmas.yaml")
_bmas_example = os.path.join(REPO_ROOT, "bmas.example.yaml")
BMAS_YAML = _bmas_yaml if os.path.exists(_bmas_yaml) else _bmas_example


def _run_config_probe(yaml_override: dict | None = None,
                      env_override: dict | None = None,
                      probe_expr: str = "print('OK')") -> subprocess.CompletedProcess:
    """Spawn a subprocess that imports config and evaluates a probe expression.

    Returns the CompletedProcess.  If yaml_override is given, a temp YAML
    file is written with the base config merged with the override.

    Storage dirs are automatically overridden to a temp directory so tests
    don't require /data/uploads to exist (which needs root).
    """
    # Load base config
    with open(BMAS_YAML) as f:
        base = yaml.safe_load(f)

    if yaml_override:
        _deep_merge(base, yaml_override)

    # Ensure storage dirs point somewhere writable — unless the test is
    # explicitly testing storage validation with its own dirs.
    storage = base.setdefault("storage", {})
    if storage.get("enabled", False):
        _tmpdir = tempfile.mkdtemp(prefix="bmas-cfg-test-")
        if storage.get("user_media_dir", "").startswith("/data"):
            storage["user_media_dir"] = os.path.join(_tmpdir, "uploads")
        if storage.get("artifacts_dir", "").startswith("/data"):
            storage["artifacts_dir"] = os.path.join(_tmpdir, "output")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        yaml.dump(base, tmp)
        tmp_path = tmp.name

    env = os.environ.copy()
    env["BMAS_CONFIG"] = tmp_path
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # Ensure required env vars are present
    env.setdefault("REDIS_PASSWORD", "test-redis-pass")
    env.setdefault("LITELLM_MASTER_KEY", "sk-test-key")
    env.setdefault("BMAS_NODE_KEY", "test-node-key-for-ci")

    if env_override:
        env.update(env_override)

    script = textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {SRC_DIR!r})
        import config
        {probe_expr}
    """)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    with contextlib.suppress(OSError):
        os.unlink(tmp_path)

    return result


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ── Tests: Coordination Config ───────────────────────────────────────

class TestCoordinationConfig:

    def test_valid_defaults_load(self):
        """Default coordination config loads without error."""
        r = _run_config_probe(probe_expr="print(config.COORDINATION_VARIANT)")
        assert r.returncode == 0
        assert "traditional" in r.stdout

    def test_invalid_variant_fails(self):
        """An invalid variant value triggers a FATAL exit."""
        r = _run_config_probe(
            yaml_override={"coordination": {"variant": "invalid_variant"}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_valid_variant_traditional(self):
        """Setting variant=traditional loads correctly."""
        r = _run_config_probe(
            yaml_override={"coordination": {"variant": "traditional"}},
            probe_expr="print(config.COORDINATION_VARIANT)",
        )
        assert r.returncode == 0
        assert "traditional" in r.stdout

    def test_blackboard_v2_flag(self):
        """blackboard_v2 flag is exposed as a bool."""
        r = _run_config_probe(
            yaml_override={"coordination": {"blackboard_v2": True}},
            probe_expr="print(config.BLACKBOARD_V2)",
        )
        assert r.returncode == 0
        assert "True" in r.stdout

    def test_invalid_round_execution_fails(self):
        """Invalid round_execution triggers FATAL."""
        r = _run_config_probe(
            yaml_override={"coordination": {"round_execution": "parallel"}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_view_budget_tokens_zero_fails(self):
        """view_budget_tokens <= 0 triggers FATAL."""
        r = _run_config_probe(
            yaml_override={"coordination": {"view_budget_tokens": 0}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_traditional_defaults(self):
        """Traditional sub-config defaults load correctly."""
        r = _run_config_probe(
            probe_expr="import json; print(json.dumps(config.TRADITIONAL_CONFIG))"
        )
        assert r.returncode == 0
        cfg = json.loads(r.stdout.strip())
        assert cfg["max_rounds"] == 4
        assert cfg["budget_ceiling_usd"] == 0.50
        assert cfg["cu_mode"] == "llm"
        assert cfg["experts_per_tier"]["complex"] == 4

    def test_invalid_cu_mode_fails(self):
        """Invalid cu_mode triggers FATAL."""
        r = _run_config_probe(
            yaml_override={"coordination": {"traditional": {"cu_mode": "gpt"}}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_invalid_sole_similarity_fails(self):
        """Invalid sole_similarity triggers FATAL."""
        r = _run_config_probe(
            yaml_override={"coordination": {"traditional": {"sole_similarity": "cosine"}}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_experts_per_tier_missing_key_fails(self):
        """Missing tier key in experts_per_tier triggers FATAL."""
        # Load base config, then surgically remove a key (deep_merge can't
        # remove keys, so we modify the base directly)
        with open(BMAS_YAML) as f:
            base = yaml.safe_load(f)
        base.setdefault("coordination", {}).setdefault("traditional", {})
        base["coordination"]["traditional"]["experts_per_tier"] = {
            "simple": 0, "light": 1, "medium": 2
            # "complex" deliberately missing
        }
        # Override storage dirs to avoid /data permission errors
        storage = base.setdefault("storage", {})
        if storage.get("enabled", False):
            _td = tempfile.mkdtemp(prefix="bmas-cfg-test-")
            if storage.get("user_media_dir", "").startswith("/data"):
                storage["user_media_dir"] = os.path.join(_td, "uploads")
            if storage.get("artifacts_dir", "").startswith("/data"):
                storage["artifacts_dir"] = os.path.join(_td, "output")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            yaml.dump(base, tmp)
            tmp_path = tmp.name
        env = os.environ.copy()
        env["BMAS_CONFIG"] = tmp_path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.setdefault("REDIS_PASSWORD", "test-redis-pass")
        env.setdefault("LITELLM_MASTER_KEY", "sk-test-key")
        env.setdefault("BMAS_NODE_KEY", "test-node-key-for-ci")
        script = textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, {SRC_DIR!r})
            import config
            print('OK')
        """)
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, timeout=10,
        )
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_experts_per_tier_non_int_fails(self):
        """Non-integer value in experts_per_tier triggers FATAL."""
        r = _run_config_probe(
            yaml_override={"coordination": {"traditional": {
                "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": "many"}
            }}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr


# ── Tests: Storage Config ────────────────────────────────────────────

class TestStorageConfig:

    def test_storage_disabled_by_default(self):
        """Storage disabled → STORAGE_ENABLED is False."""
        r = _run_config_probe(
            yaml_override={"storage": {"enabled": False}},
            probe_expr="print(config.STORAGE_ENABLED)",
        )
        assert r.returncode == 0
        assert "False" in r.stdout

    def test_storage_enabled_with_writable_dirs(self):
        """Storage enabled with valid dirs succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            r = _run_config_probe(
                yaml_override={"storage": {
                    "enabled": True,
                    "user_media_dir": os.path.join(tmpdir, "uploads"),
                    "artifacts_dir": os.path.join(tmpdir, "output"),
                }},
                probe_expr="print(config.STORAGE_ENABLED)",
            )
            assert r.returncode == 0
            assert "True" in r.stdout

    def test_storage_invalid_pdf_extraction_fails(self):
        """Invalid pdf_extraction triggers FATAL."""
        r = _run_config_probe(
            yaml_override={"storage": {"pdf_extraction": "tika"}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_storage_config_values(self):
        """Storage config values are parsed correctly."""
        r = _run_config_probe(
            probe_expr="import json; print(json.dumps(config.STORAGE_CONFIG))"
        )
        assert r.returncode == 0
        cfg = json.loads(r.stdout.strip())
        assert cfg["max_upload_mb"] == 50
        assert "pdf" in cfg["allowed_upload_types"]


# ── Tests: Model Pricing ─────────────────────────────────────────────

class TestModelPricing:

    def test_pricing_loaded(self):
        """Model pricing is loaded from bmas.yaml."""
        r = _run_config_probe(
            probe_expr="import json; print(json.dumps(config.MODEL_PRICING))"
        )
        assert r.returncode == 0
        pricing = json.loads(r.stdout.strip())
        # The test bmas.yaml has pricing for gemini-pro
        assert "gemini-pro" in pricing
        assert pricing["gemini-pro"]["input_cost_per_token"] > 0

    def test_model_without_pricing_warns(self):
        """Models without pricing produce a warning, not an error."""
        r = _run_config_probe(
            yaml_override={"models": {
                "test-model": {
                    "provider": "openai",
                    "model": "gpt-test",
                    "api_key_env": "OPENAI_API_KEY",
                    "max_tokens": 1000,
                },
            }},
        )
        assert r.returncode == 0
        assert "no pricing" in r.stderr


# ── Tests: BMAS_NODE_KEY ─────────────────────────────────────────────

class TestBmasNodeKey:

    def test_node_key_required(self):
        """Missing BMAS_NODE_KEY triggers FATAL."""
        r = _run_config_probe(env_override={"BMAS_NODE_KEY": ""})
        assert r.returncode != 0
        assert "BMAS_NODE_KEY" in r.stderr

    def test_node_key_loaded(self):
        """BMAS_NODE_KEY is loaded from environment."""
        r = _run_config_probe(
            probe_expr="print(repr(config.BMAS_NODE_KEY))"
        )
        assert r.returncode == 0
        assert "test-node-key-for-ci" in r.stdout


# ── Tests: Triage Backend ────────────────────────────────────────────

class TestTriageBackend:

    def test_default_backend_is_gemini(self):
        """When triage.backend is not set, it defaults to 'gemini'."""
        r = _run_config_probe(
            yaml_override={"triage": {"enabled": True}},
            probe_expr="print(config.TRIAGE_BACKEND)",
        )
        assert r.returncode == 0
        assert "gemini" in r.stdout

    def test_backend_gemini_loads(self):
        """Setting backend=gemini loads correctly with gemini model alias."""
        r = _run_config_probe(
            yaml_override={"triage": {"backend": "gemini", "model": "gemini-flash-lite"}},
            probe_expr="print(f'{config.TRIAGE_BACKEND}|{config.TRIAGE_MODEL}')",
        )
        assert r.returncode == 0
        assert "gemini|gemini-flash-lite" in r.stdout

    def test_backend_local_loads(self):
        """Setting backend=local loads correctly with local model name."""
        r = _run_config_probe(
            yaml_override={"triage": {"backend": "local", "local_model": "Qwen/Qwen3-1.7B"}},
            probe_expr="print(f'{config.TRIAGE_BACKEND}|{config.TRIAGE_LOCAL_MODEL}')",
        )
        assert r.returncode == 0
        assert "local|Qwen/Qwen3-1.7B" in r.stdout

    def test_invalid_backend_fails(self):
        """An invalid backend value triggers a FATAL exit."""
        r = _run_config_probe(
            yaml_override={"triage": {"backend": "ollama"}}
        )
        assert r.returncode != 0
        assert "FATAL" in r.stderr

    def test_backend_local_model_alias(self):
        """With backend=local, TRIAGE_MODEL points to the local model."""
        r = _run_config_probe(
            yaml_override={"triage": {"backend": "local", "local_model": "Qwen/Qwen3-1.7B"}},
            probe_expr="print(config.TRIAGE_MODEL)",
        )
        assert r.returncode == 0
        assert "Qwen/Qwen3-1.7B" in r.stdout

    def test_backend_gemini_model_alias(self):
        """With backend=gemini, TRIAGE_MODEL points to the gemini model alias."""
        r = _run_config_probe(
            yaml_override={"triage": {"backend": "gemini", "model": "gemini-flash-lite"}},
            probe_expr="print(config.TRIAGE_MODEL)",
        )
        assert r.returncode == 0
        assert "gemini-flash-lite" in r.stdout

    def test_gemini_backend_warns_on_unknown_model(self):
        """Gemini backend with unknown model alias produces a warning (not FATAL)."""
        r = _run_config_probe(
            yaml_override={"triage": {"backend": "gemini", "model": "nonexistent-model"}},
        )
        assert r.returncode == 0  # Warning, not fatal
        assert "WARNING" in r.stderr or "not defined" in r.stderr

    def test_triage_disabled_ignores_backend(self):
        """When triage is disabled, backend validation still passes."""
        r = _run_config_probe(
            yaml_override={"triage": {"enabled": False, "backend": "gemini"}},
            probe_expr="print(config.TRIAGE_ENABLED)",
        )
        assert r.returncode == 0
        assert "False" in r.stdout

