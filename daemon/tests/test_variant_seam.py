"""Tests for the CoordinationVariant seam (doc 03 §6)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import core.variants as variant_registry
from core.variants import (
    _ALIASES,
    _VARIANTS,
    SEAMS_CHECKLIST,
    CoordinationVariant,
    UnknownVariantError,
    VariantDescriptor,
    available_variants,
    canonical_variant_id,
    get_variant_class,
    register_variant,
    verify_seams_checklist,
)


@pytest.fixture(autouse=True)
def preserve_variant_registry():
    """Restore the complete runtime registry after each test."""
    variants = dict(_VARIANTS)
    aliases = dict(_ALIASES)
    availability = dict(variant_registry._AVAILABILITY)
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


class TestSeamsChecklist:

    def test_checklist_has_8_items(self):
        """The seams checklist must have exactly 8 items (doc 03 §6)."""
        assert len(SEAMS_CHECKLIST) == 8

    def test_verify_returns_copy(self):
        """verify_seams_checklist returns a copy, not the original."""
        result = verify_seams_checklist()
        assert result == SEAMS_CHECKLIST
        assert result is not SEAMS_CHECKLIST

    def test_checklist_items_are_strings(self):
        for item in SEAMS_CHECKLIST:
            assert isinstance(item, str)
            assert len(item) > 20  # Non-trivial content


class TestVariantRegistry:
    def test_register_and_get(self):
        class FakeVariant(RuntimeStub):
            name = "fake"
            descriptor = VariantDescriptor("fake", "Fake", "1")
        register_variant("fake", FakeVariant)
        assert get_variant_class("fake") is FakeVariant

    def test_get_unknown_returns_none(self):
        assert get_variant_class("nonexistent") is None

    def test_available_variants(self):
        class V1(RuntimeStub):
            name = "v1"
            descriptor = VariantDescriptor("v1", "V1", "1")
        class V2(RuntimeStub):
            name = "v2"
            descriptor = VariantDescriptor("v2", "V2", "1")
        register_variant("v1", V1)
        register_variant("v2", V2)
        assert {"classic", "v1", "v2"}.issubset(available_variants())

    def test_register_non_class_raises(self):
        with pytest.raises(TypeError):
            register_variant("bad", "not a class")  # type: ignore

    def test_register_malformed_runtime_raises(self):
        class MalformedRuntime:
            descriptor = VariantDescriptor("malformed", "Malformed", "1")

        with pytest.raises(TypeError, match="capture_configuration"):
            register_variant("malformed", MalformedRuntime)

    def test_builtin_rejects_unregistered_variant(self):
        with pytest.raises(UnknownVariantError):
            canonical_variant_id("unregistered")

    def test_descriptor_publishes_an_extensible_benchmark_contract(self):
        contract = VariantDescriptor("future", "Future", "1").to_dict()["benchmark"]

        assert contract["supported"] is True
        assert contract["configuration_schema"]["type"] == "object"
        assert contract["seed_strategy"] == "recorded"
        assert contract["required_snapshot_fields"] == [
            "runtime_id",
            "runtime_configuration",
            "random_seed",
        ]


class TestProtocol:

    def test_protocol_is_runtime_checkable(self):
        """CoordinationVariant is runtime-checkable."""

        class GoodVariant:
            descriptor = VariantDescriptor("good", "Good", "1")

            @classmethod
            async def capture_configuration(cls, overrides=None): ...

            @classmethod
            def configuration_from_metadata(cls, metadata): ...

            @classmethod
            async def run(cls, host, request): ...

        assert isinstance(GoodVariant(), CoordinationVariant)
