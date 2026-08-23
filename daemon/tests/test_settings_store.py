# /opt/bmas/daemon/tests/test_settings_store.py
"""
Tests for the runtime settings store.
"""

import asyncio
import copy

# We need to mock config imports before importing settings_store
from unittest.mock import MagicMock, patch

import pytest

# Minimal mock config values
MOCK_MODEL_ROUTING = {
    "simple": "edge-node-1",
    "light": "gemini-flash-lite",
    "medium": "gemini-flash",
    "complex": "gemini-pro",
}

MOCK_ROLE_REGISTRY = {
    "planner": {
        "preferred_host": "10.0.0.101",
        "profile": "planner",
        "dispatch_port": 8000,
        "endpoints": ["http://10.0.0.101:8000"],
    },
    "expert": {
        "preferred_host": None,
        "profile": "expert",
        "dispatch_port": 8000,
        "endpoints": ["http://10.0.0.101:8000"],
    },
}

MOCK_RAW_CONFIG = {
    "models": {
        "gemini-pro": {"provider": "gemini", "model": "gemini-3.1-pro-preview", "api_key_env": "GEMINI_API_KEY"},
        "gemini-flash": {"provider": "gemini", "model": "gemini-3.5-flash", "api_key_env": "GEMINI_API_KEY"},
        "gemini-flash-lite": {"provider": "gemini", "model": "gemini-3.1-flash-lite-preview", "api_key_env": "GEMINI_API_KEY"},
    },
    "nodes": [
        {"host": "10.0.0.101", "name": "node-1", "role": "planner", "port": 8000},
    ],
}


MOCK_CLASSIC_CONFIG = {
    "max_rounds": 4,
    "max_duration_s": 1800,
    "budget_ceiling_usd": 0.5,
    "max_concurrent_activations": 3,
    "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 4},
    "cleaner_entry_threshold": 12,
    "cleaner_token_threshold": 8000,
    "cleaner_retention_weights": {"salience": 2.0, "confidence": 1.0, "recency": 0.1, "size_penalty": 0.01},
    "stall_rounds": 2,
    "max_replans": 2,
    "cu_mode": "llm",
    "coordinator_narration": False,
    "sole_similarity": "auto",
}


@pytest.fixture(autouse=True)
def mock_config():
    """Patch config module so settings_store seeds from mocks."""
    with patch.dict("sys.modules", {
        "config": MagicMock(
            MODEL_ROUTING=copy.deepcopy(MOCK_MODEL_ROUTING),
            ROLE_REGISTRY=copy.deepcopy(MOCK_ROLE_REGISTRY),
            RAW_CONFIG=MOCK_RAW_CONFIG,
            EDGE_NODE_MODELS=["edge-node-1"],
            NODES_BY_ROLE={},
            CLASSIC_CONFIG=copy.deepcopy(MOCK_CLASSIC_CONFIG),
            ROUND_EXECUTION="concurrent",
            VIEW_BUDGET_TOKENS=12000,
        )
    }):
        # Reset singleton between tests
        import sys
        if "settings_store" in sys.modules:
            del sys.modules["settings_store"]
        yield
    # Cleanup
    if "settings_store" in sys.modules:
        del sys.modules["settings_store"]


def get_fresh_store():
    """Import and return a fresh SettingsStore instance (bypasses singleton)."""
    import importlib
    import sys
    if "settings_store" in sys.modules:
        del sys.modules["settings_store"]
    mod = importlib.import_module("settings_store")
    return mod.SettingsStore()


@pytest.mark.asyncio
async def test_get_routing_returns_defaults():
    store = get_fresh_store()
    routing = await store.get_routing()
    assert routing["simple"] == "edge-node-1"
    assert routing["medium"] == "gemini-flash"
    assert routing["complex"] == "gemini-pro"


@pytest.mark.asyncio
async def test_patch_routing_updates_tier():
    store = get_fresh_store()
    result = await store.patch_routing({"simple": "gemini-flash-lite"})
    assert result["simple"] == "gemini-flash-lite"
    # Other tiers unchanged
    assert result["medium"] == "gemini-flash"


@pytest.mark.asyncio
async def test_patch_routing_rejects_invalid_tier():
    store = get_fresh_store()
    with pytest.raises(ValueError, match="Unknown complexity tier"):
        await store.patch_routing({"ultra": "gemini-pro"})


@pytest.mark.asyncio
async def test_patch_routing_rejects_unknown_model():
    store = get_fresh_store()
    with pytest.raises(ValueError, match="Unknown model alias"):
        await store.patch_routing({"medium": "gpt-99-turbo"})


@pytest.mark.asyncio
async def test_defaults_routing_not_mutated():
    store = get_fresh_store()
    await store.patch_routing({"simple": "gemini-pro"})
    defaults = await store.get_defaults_routing()
    # Defaults should still be the original yaml values
    assert defaults["simple"] == "edge-node-1"


@pytest.mark.asyncio
async def test_reset_to_defaults():
    store = get_fresh_store()
    await store.patch_routing({"medium": "gemini-pro"})
    result = await store.reset_to_defaults()
    assert result["routing"]["medium"] == "gemini-flash"


@pytest.mark.asyncio
async def test_get_role_registry_returns_entries():
    store = get_fresh_store()
    registry = await store.get_role_registry()
    assert "planner" in registry
    assert registry["planner"]["profile"] == "planner"
    assert registry["planner"]["dispatch_port"] == 8000


@pytest.mark.asyncio
async def test_patch_role_registry_updates_host():
    store = get_fresh_store()
    result = await store.patch_role_registry({
        "planner": {"preferred_host": "10.0.0.200"}
    })
    assert result["planner"]["preferred_host"] == "10.0.0.200"
    # Other fields unchanged
    assert result["planner"]["profile"] == "planner"


@pytest.mark.asyncio
async def test_patch_role_registry_set_null_host():
    store = get_fresh_store()
    result = await store.patch_role_registry({
        "planner": {"preferred_host": None}
    })
    assert result["planner"]["preferred_host"] is None


@pytest.mark.asyncio
async def test_patch_role_registry_invalid_port():
    store = get_fresh_store()
    with pytest.raises(ValueError, match="dispatch_port"):
        await store.patch_role_registry({
            "planner": {"dispatch_port": 99999}
        })


@pytest.mark.asyncio
async def test_get_schema_returns_models():
    store = get_fresh_store()
    schema = await store.get_schema()
    aliases = [m["alias"] for m in schema["available_models"]]
    assert "gemini-pro" in aliases
    assert "gemini-flash" in aliases


@pytest.mark.asyncio
async def test_concurrent_patch_is_safe():
    """Verify lock prevents data races under concurrent patches."""
    store = get_fresh_store()

    async def patch_simple(val: str):
        await store.patch_routing({"simple": val})

    # Fire 5 concurrent patches — all should succeed without exceptions
    # Use values that are in available_models (named model aliases or defaults)
    await asyncio.gather(
        patch_simple("gemini-pro"),
        patch_simple("gemini-flash"),
        patch_simple("gemini-flash-lite"),
        patch_simple("gemini-flash"),   # repeat is fine
        patch_simple("gemini-pro"),
    )
    routing = await store.get_routing()
    # Final value is one of the patched values — exact value is non-deterministic
    assert routing["simple"] in {"gemini-pro", "gemini-flash", "gemini-flash-lite"}


# ── Classic runtime limits ───────────────────────────────────────────────



@pytest.fixture
def classic_store():
    """A fresh store seeded from the mocked config."""
    return get_fresh_store()


def test_classic_seeds_from_config(classic_store):
    classic = asyncio.run(classic_store.get_classic())
    assert classic["max_rounds"] == 4
    assert classic["round_execution"] == "concurrent"
    assert classic["view_budget_tokens"] == 12000


def test_patch_classic_merges_and_validates(classic_store):
    updated = asyncio.run(classic_store.patch_classic({
        "max_rounds": 6,
        "experts_per_tier": {"complex": 5},
        "round_execution": "sequential",
    }))
    assert updated["max_rounds"] == 6
    assert updated["experts_per_tier"] == {"simple": 0, "light": 1, "medium": 2, "complex": 5}
    assert updated["round_execution"] == "sequential"
    assert updated["budget_ceiling_usd"] == 0.5


@pytest.mark.parametrize("patch_body", [
    {"max_rounds": 0},
    {"budget_ceiling_usd": 0},
    {"cu_mode": "random"},
    {"round_execution": "parallel"},
    {"view_budget_tokens": 100},
    {"experts_per_tier": {"huge": 3}},
    {"unknown_key": 1},
])
def test_patch_classic_rejects_invalid_values(classic_store, patch_body):
    with pytest.raises(ValueError):
        asyncio.run(classic_store.patch_classic(patch_body))
    assert asyncio.run(classic_store.get_classic()) == asyncio.run(classic_store.get_defaults_classic())


def test_reset_restores_classic_defaults(classic_store):
    asyncio.run(classic_store.patch_classic({"max_rounds": 9}))
    restored = asyncio.run(classic_store.reset_to_defaults())
    assert restored["classic"]["max_rounds"] == 4
    assert asyncio.run(classic_store.get_classic())["max_rounds"] == 4


def test_role_registry_patch_accepts_enabled(classic_store):
    updated = asyncio.run(classic_store.patch_role_registry({"expert": {"enabled": False}}))
    assert updated["expert"]["enabled"] is False
    with pytest.raises(ValueError):
        asyncio.run(classic_store.patch_role_registry({"expert": {"enabled": "no"}}))


def test_schema_lists_classic_fields(classic_store):
    schema = asyncio.run(classic_store.get_schema())
    keys = [field["key"] for field in schema["classic_fields"]]
    assert "max_rounds" in keys and "round_execution" in keys


def test_validate_classic_settings_rejects_unknown_keys(classic_store):
    from settings_store import validate_classic_settings
    base = asyncio.run(classic_store.get_classic())
    assert validate_classic_settings(base)["max_rounds"] == 4
    with pytest.raises(ValueError):
        validate_classic_settings({**base, "mystery": 1})
