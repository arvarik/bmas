"""Foundation Stage 0A: golden fixtures freeze every existing runtime pair.

Each fixture in ``conformance/runtime_fixtures`` stores one frozen
capture of current behavior: the capability document, the effective
configurations, full deterministic lifecycles, the legacy metadata
migration, the protocol vocabulary, the historical labels, and the
user-interface adapter identity.

A mismatch means the current contract changed. An intentional contract
change updates the fixture inside a reviewed commit:

    BMAS_UPDATE_RUNTIME_FIXTURES=1 python -m pytest tests/test_runtime_golden_fixtures.py

An unexplained mismatch is a defect.
"""

from __future__ import annotations

import copy
import json
import os

import pytest
import runtime_fixture_capture as capture

import config
from core.variants import classic as classic_runtime_module
from core.variants import collaborative as collaborative_runtime_module
from core.variants.collaborative import (
    PatchboardVariantRuntime,
    StigmergicVariantRuntime,
)

UPDATE_MODE = os.environ.get("BMAS_UPDATE_RUNTIME_FIXTURES") == "1"

FIXTURE_IDS = sorted(capture.CAPTURES)

# The pinned capture environment. Every golden capture reads exactly
# these values, so no other test module can change the captured bytes
# through shared module state.
PINNED_CONFIGURATION = {
    "MODEL_POOLS": {},
    "MODEL_PRICING": {},
    "AGENT_ENDPOINTS": {},
    "EDGE_NODE_MODELS": ["edge-node-1"],
    "MODEL_ROUTING": {
        "simple": "local",
        "light": "test-light",
        "medium": "test-medium",
        "complex": "test-pro",
    },
    "ROLE_REGISTRY": {},
    "CLASSIC_CONFIG": {
        "max_rounds": 4,
        "max_duration_s": 1800,
        "budget_ceiling_usd": 0.50,
        "max_concurrent_activations": 3,
        "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
        "cleaner_entry_threshold": 12,
        "stall_rounds": 2,
        "max_replans": 2,
        "cu_mode": "llm",
        "coordinator_narration": False,
        "sole_similarity": "auto",
    },
}


@pytest.fixture()
def pinned_capture_environment(monkeypatch):
    """Pin every configuration value that a golden capture reads.

    The runtime modules bind configuration names at import time, so the
    pin covers the runtime modules and the config module.
    """
    targets = (config, classic_runtime_module, collaborative_runtime_module)
    for name, value in PINNED_CONFIGURATION.items():
        for module in targets:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, copy.deepcopy(value))
    capture.reset_runtime_settings()
    yield
    capture.reset_runtime_settings()


def read_fixture(fixture_id: str) -> dict:
    return json.loads(capture.fixture_path(fixture_id).read_bytes())


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
async def test_runtime_fixture_stays_frozen(
    fixture_id: str, pinned_capture_environment
) -> None:
    captured = await capture.capture_fixture_bytes(fixture_id)
    path = capture.fixture_path(fixture_id)
    if UPDATE_MODE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(captured)
        return
    assert path.is_file(), (
        f"The golden fixture {path.name} is missing. Regenerate it with "
        "BMAS_UPDATE_RUNTIME_FIXTURES=1 and review the result."
    )
    frozen = path.read_bytes()
    assert captured == frozen, (
        f"The runtime contract no longer matches the golden fixture {path.name}. "
        "An intentional contract change must update the fixture in a reviewed "
        "commit with BMAS_UPDATE_RUNTIME_FIXTURES=1. An unexplained change is "
        "a defect."
    )


def test_fixture_directory_holds_no_unknown_fixture() -> None:
    if UPDATE_MODE:
        pytest.skip("update mode rewrites fixtures")
    on_disk = sorted(
        path.name[: -len(".json")]
        for path in capture.FIXTURES_DIR.glob("*.json")
    )
    assert on_disk == FIXTURE_IDS


def test_historical_labels_stay_frozen() -> None:
    record = read_fixture("runtime-labels")["record"]
    labels = {entry["runtime_id"]: entry for entry in record["labels"]}
    assert labels["classic"]["historical_label"] == "Classic blackboard"
    assert labels["classic"]["aliases"] == ["traditional"]
    assert labels["patchboard"]["historical_label"] == "Patchboard"
    assert labels["patchboard"]["study_label"] == "Parallel synthesis"
    assert labels["stigmergic"]["historical_label"] == "Stigmergic workspace"
    for entry in labels.values():
        assert entry["contract_version"] == "1"


@pytest.mark.parametrize(
    "runtime", [PatchboardVariantRuntime, StigmergicVariantRuntime]
)
def test_frozen_final_checkpoint_stays_loadable(runtime) -> None:
    record = read_fixture(f"{runtime.descriptor.id}-lifecycle")["record"]
    final_checkpoint = record["fresh_run"]["checkpoints"][-1]
    restored = runtime._restore(final_checkpoint)
    assert restored["answer"] == record["fresh_run"]["outcome"]["answer"]


def test_resumed_lifecycle_never_dispatches_again() -> None:
    for runtime_id in ("patchboard", "stigmergic"):
        record = read_fixture(f"{runtime_id}-lifecycle")["record"]
        resumed = record["resumed_run"]
        assert resumed["dispatch_count"] == 0
        assert resumed["checkpoint_writes"] == 0
        fresh_answer = record["fresh_run"]["outcome"]["answer"]
        assert resumed["outcome"]["answer"] == fresh_answer
