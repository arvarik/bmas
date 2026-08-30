"""Foundation Stage 0B: runtime routing resolves exact pairs only.

The registry stores several contract versions of one runtime family at
the same time. Every lookup resolves one complete pair. No path selects
a newer contract version silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.variants as variant_registry
from core.variants import (
    _ALIASES,
    _VARIANTS,
    MissingCheckpointReaderError,
    RuntimeKey,
    RuntimeNotAdmissibleError,
    UnknownVariantError,
    UnsupportedContractError,
    VariantDescriptor,
    canonical_variant_id,
    capability_record,
    load_builtin_variants,
    register_variant,
    registered_runtime_keys,
    require_admissible_runtime,
    require_checkpoint_reader,
    require_runtime,
    resolve_runtime_key,
    runtime_availability,
    variant_capabilities,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def preserve_variant_registry():
    """Run every routing test against exactly the built-in registry.

    Another test module can leak a registration into the shared
    registry. The fixture resets the registry to the built-in pairs
    first, then restores the previous state afterward.
    """
    variants = dict(_VARIANTS)
    aliases = dict(_ALIASES)
    availability = dict(variant_registry._AVAILABILITY)
    _VARIANTS.clear()
    _ALIASES.clear()
    variant_registry._AVAILABILITY.clear()
    load_builtin_variants()
    yield
    _VARIANTS.clear()
    _VARIANTS.update(variants)
    _ALIASES.clear()
    _ALIASES.update(aliases)
    variant_registry._AVAILABILITY.clear()
    variant_registry._AVAILABILITY.update(availability)


class RuntimeStub:
    """Provide the required runtime methods for registry tests."""

    @classmethod
    async def capture_configuration(cls, overrides=None): ...

    @classmethod
    def configuration_from_metadata(cls, metadata): ...

    @classmethod
    async def run(cls, host, request): ...


def successor_runtime(
    runtime_id: str, *, supports_recovery: bool = True,
) -> type:
    """Build one planned successor runtime with contract version two."""

    class SuccessorRuntime(RuntimeStub):
        descriptor = VariantDescriptor(
            runtime_id,
            f"{runtime_id} successor",
            "2",
            supports_recovery=supports_recovery,
        )

    return SuccessorRuntime


def register_successors() -> dict[str, RuntimeKey]:
    """Register the planned successor pairs beside the built-in pairs."""
    keys = {}
    for runtime_id in ("classic", "patchboard"):
        keys[runtime_id] = register_variant(
            runtime_id,
            successor_runtime(runtime_id),
            availability="planned",
            bind_aliases=False,
        )
    return keys


def test_registry_stores_both_contract_versions_together():
    register_successors()
    keys = registered_runtime_keys()
    assert RuntimeKey("classic", "1") in keys
    assert RuntimeKey("classic", "2") in keys
    assert RuntimeKey("patchboard", "1") in keys
    assert RuntimeKey("patchboard", "2") in keys
    assert RuntimeKey("stigmergic", "1") in keys


def test_exact_pair_lookup():
    register_successors()
    first = require_runtime(RuntimeKey("classic", "1"))
    second = require_runtime(RuntimeKey("classic", "2"))
    assert first is not second
    assert first.descriptor.contract_version == "1"
    assert second.descriptor.contract_version == "2"


def test_alias_resolves_to_one_complete_pair():
    register_successors()
    assert resolve_runtime_key("traditional") == RuntimeKey("classic", "1")
    assert resolve_runtime_key("classic") == RuntimeKey("classic", "1")
    assert canonical_variant_id("traditional") == "classic"


def test_unknown_runtime_fails_closed():
    with pytest.raises(UnknownVariantError):
        require_runtime(RuntimeKey("unheard-of", "1"))
    with pytest.raises(UnknownVariantError):
        resolve_runtime_key("unheard-of")


def test_unknown_contract_version_fails_closed():
    with pytest.raises(UnsupportedContractError):
        require_runtime(RuntimeKey("classic", "3"))


def test_missing_checkpoint_reader_is_rejected():
    class FrozenReaderRuntime(RuntimeStub):
        descriptor = VariantDescriptor(
            "frozen-reader",
            "Frozen reader",
            "1",
            supports_recovery=False,
        )

    key = register_variant(
        "frozen-reader", FrozenReaderRuntime, availability="test_only",
    )
    with pytest.raises(MissingCheckpointReaderError):
        require_checkpoint_reader(key)
    assert require_checkpoint_reader(RuntimeKey("classic", "1"))


def test_capability_negotiation_publishes_every_qualified_pair():
    register_successors()
    document = variant_capabilities()
    published = {
        (record["id"], record["contract_version"])
        for record in document["variants"]
    }
    assert published == {
        ("classic", "1"),
        ("patchboard", "1"),
        ("stigmergic", "1"),
    }
    for runtime_id, contract_version in published:
        record = capability_record(RuntimeKey(runtime_id, contract_version))
        assert record["id"] == runtime_id
        assert record["contract_version"] == contract_version


def test_planned_pair_never_becomes_a_runnable_choice():
    register_successors()
    document = variant_capabilities()
    versions = [
        record["contract_version"] for record in document["variants"]
    ]
    assert "2" not in versions


def test_interface_adapter_identity_matches_the_frozen_fixture():
    fixture_path = (
        REPO_ROOT / "conformance" / "runtime_fixtures" / "ui-adapter-support.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    frozen = {
        (entry["id"], version)
        for entry in fixture["record"]["variants"]
        for version in entry["contract_versions"]
    }
    qualified = {
        (key.runtime_id, key.runtime_contract_version)
        for key in registered_runtime_keys()
        if runtime_availability(key) == "qualified"
    }
    assert frozen == qualified


def test_production_admission_denies_planned_and_test_only():
    successor_keys = register_successors()
    test_key = register_variant(
        "classic",
        successor_runtime("classic"),
        availability="test_only",
        bind_aliases=False,
    )
    assert test_key == successor_keys["classic"]
    with pytest.raises(RuntimeNotAdmissibleError):
        require_admissible_runtime(successor_keys["patchboard"])
    with pytest.raises(RuntimeNotAdmissibleError):
        require_admissible_runtime(test_key)


def test_qualified_pair_is_admissible():
    for key in registered_runtime_keys():
        assert runtime_availability(key) == "qualified"
        assert require_admissible_runtime(key)


def test_no_path_selects_a_newer_contract_version_silently():
    register_successors()
    # The bound alias still names the original complete pair.
    assert resolve_runtime_key("classic") == RuntimeKey("classic", "1")
    # A successor registration cannot steal the bound alias.
    with pytest.raises(ValueError, match="already bound"):
        register_variant(
            "classic",
            successor_runtime("classic"),
            availability="planned",
            bind_aliases=True,
        )
    # An exact request for an unregistered version stays an error.
    with pytest.raises(UnsupportedContractError):
        require_runtime(RuntimeKey("stigmergic", "2"))


def test_runtime_key_normalizes_id_and_preserves_version():
    key = RuntimeKey("  Classic ", "1")
    assert key.runtime_id == "classic"
    assert key.runtime_contract_version == "1"
    with pytest.raises(ValueError):
        RuntimeKey("classic", " 1 ")
    with pytest.raises(ValueError):
        RuntimeKey("classic", "")
    with pytest.raises(ValueError):
        RuntimeKey("", "1")
