"""Tests for the CoordinationVariant seam (doc 03 §6)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.variants import (
    CoordinationVariant,
    SEAMS_CHECKLIST,
    verify_seams_checklist,
    register_variant,
    get_variant_class,
    available_variants,
    _VARIANTS,
)


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

    def setup_method(self):
        """Clear registry before each test."""
        _VARIANTS.clear()

    def test_register_and_get(self):
        class FakeVariant:
            name = "fake"
        register_variant("fake", FakeVariant)
        assert get_variant_class("fake") is FakeVariant

    def test_get_unknown_returns_none(self):
        assert get_variant_class("nonexistent") is None

    def test_available_variants(self):
        class V1:
            name = "v1"
        class V2:
            name = "v2"
        register_variant("v1", V1)
        register_variant("v2", V2)
        assert sorted(available_variants()) == ["v1", "v2"]

    def test_register_non_class_raises(self):
        with pytest.raises(TypeError):
            register_variant("bad", "not a class")  # type: ignore


class TestProtocol:

    def test_protocol_is_runtime_checkable(self):
        """CoordinationVariant is runtime-checkable."""

        class GoodVariant:
            name = "good"
            async def genesis(self, task): ...
            def build_turn_payload(self, task, actor, board): ...
            def parse_agent_response(self, task, actor, raw): ...
            async def apply(self, task, mutations): ...
            async def step(self, task, board): ...
            def is_terminal(self, board): ...

        assert isinstance(GoodVariant(), CoordinationVariant)
