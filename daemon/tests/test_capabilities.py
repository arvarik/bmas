# /opt/bmas/daemon/tests/test_capabilities.py
"""Capability-based authorization tests (doc 04 §4, seam rule 4)."""
from __future__ import annotations

import pytest

from core.capabilities import (
    CAPABILITY_PROFILES,
    AuthorizationError,
    authorize_post,
    authorize_remove,
    capabilities_for_role,
)


class TestCapabilityProfiles:
    """Verify the shape and completeness of capability profiles."""

    def test_all_profiles_defined(self):
        expected = {
            "plan_writer",
            "finding_writer",
            "critique_writer",
            "conflict_mediator",
            "board_maintenance",
            "decision_writer",
        }
        assert set(CAPABILITY_PROFILES.keys()) == expected

    def test_board_maintenance_can_post_finding(self):
        """Board maintenance (Cleaner) can post finding entries."""
        profile = CAPABILITY_PROFILES["board_maintenance"]
        assert "finding" in profile.may_post

    def test_decision_writer_can_post_solution(self):
        profile = CAPABILITY_PROFILES["decision_writer"]
        assert "solution" in profile.may_post

    def test_plan_writer_may_never_post_solution(self):
        profile = CAPABILITY_PROFILES["plan_writer"]
        assert "solution" in profile.may_never


class TestAuthorizePost:
    """Test the authorize_post function."""

    def test_plan_writer_can_post_plan(self):
        authorize_post(["plan_writer"], "plan")  # Should not raise

    def test_finding_writer_can_post_finding(self):
        authorize_post(["finding_writer"], "finding")

    def test_finding_writer_can_post_rebuttal(self):
        authorize_post(["finding_writer"], "rebuttal")

    def test_critique_writer_can_post_critique(self):
        authorize_post(["critique_writer"], "critique")

    def test_conflict_mediator_can_post_conflict(self):
        authorize_post(["conflict_mediator"], "conflict")

    def test_conflict_mediator_can_post_rebuttal(self):
        authorize_post(["conflict_mediator"], "rebuttal")

    def test_decision_writer_can_post_solution(self):
        authorize_post(["decision_writer"], "solution")

    def test_decision_writer_can_post_objective(self):
        authorize_post(["decision_writer"], "objective")

    def test_decision_writer_can_post_directive(self):
        authorize_post(["decision_writer"], "directive")

    def test_critique_writer_cannot_post_solution(self):
        with pytest.raises(AuthorizationError, match="explicitly denies"):
            authorize_post(["critique_writer"], "solution")

    def test_critique_writer_cannot_post_finding(self):
        with pytest.raises(AuthorizationError, match="explicitly denies"):
            authorize_post(["critique_writer"], "finding")

    def test_plan_writer_cannot_post_solution(self):
        with pytest.raises(AuthorizationError, match="explicitly denies"):
            authorize_post(["plan_writer"], "solution")

    def test_finding_writer_cannot_post_solution(self):
        with pytest.raises(AuthorizationError, match="explicitly denies"):
            authorize_post(["finding_writer"], "solution")

    def test_board_maintenance_can_post_finding(self):
        authorize_post(["board_maintenance"], "finding")

    def test_board_maintenance_cannot_post_solution(self):
        with pytest.raises(AuthorizationError, match="No capability"):
            authorize_post(["board_maintenance"], "solution")

    def test_no_capabilities_raises(self):
        with pytest.raises(AuthorizationError, match="No capabilities"):
            authorize_post([], "finding")

    def test_unknown_capability_raises(self):
        with pytest.raises(AuthorizationError, match="No capability"):
            authorize_post(["nonexistent_cap"], "finding")

    def test_multiple_capabilities_allow(self):
        """Multiple capabilities: if one allows, it passes."""
        authorize_post(["finding_writer", "conflict_mediator"], "rebuttal")

    def test_may_never_overrides_may_post(self):
        """If one cap allows and another denies, deny wins."""
        with pytest.raises(AuthorizationError, match="explicitly denies"):
            authorize_post(["decision_writer", "plan_writer"], "solution")
        # plan_writer has solution in may_never

    def test_finding_writer_cannot_post_plan(self):
        with pytest.raises(AuthorizationError, match="No capability"):
            authorize_post(["finding_writer"], "plan")


class TestAuthorizeRemove:
    """Test the authorize_remove function."""

    def test_board_maintenance_can_remove_finding(self):
        authorize_remove(["board_maintenance"], "finding")

    def test_board_maintenance_can_remove_critique(self):
        authorize_remove(["board_maintenance"], "critique")

    def test_board_maintenance_can_remove_plan(self):
        authorize_remove(["board_maintenance"], "plan")

    def test_cannot_remove_objective(self):
        with pytest.raises(AuthorizationError, match="cannot be removed"):
            authorize_remove(["board_maintenance"], "objective")

    def test_cannot_remove_solution(self):
        with pytest.raises(AuthorizationError, match="cannot be removed"):
            authorize_remove(["board_maintenance"], "solution")

    def test_finding_writer_cannot_remove(self):
        with pytest.raises(AuthorizationError, match="No capability"):
            authorize_remove(["finding_writer"], "finding")

    def test_no_capabilities_raises(self):
        with pytest.raises(AuthorizationError, match="No capabilities"):
            authorize_remove([], "finding")


class TestCapabilitiesForRole:
    """Test the traditional variant's role→capability mapping."""

    def test_planner(self):
        assert capabilities_for_role("planner") == ["plan_writer"]

    def test_critic(self):
        assert capabilities_for_role("critic") == ["critique_writer"]

    def test_conflict_resolver(self):
        assert capabilities_for_role("conflict_resolver") == ["conflict_mediator"]

    def test_cleaner(self):
        assert capabilities_for_role("cleaner") == ["board_maintenance"]

    def test_decider(self):
        assert capabilities_for_role("decider") == ["decision_writer"]

    def test_executor_legacy(self):
        assert capabilities_for_role("executor") == ["finding_writer"]

    def test_auditor_legacy(self):
        assert capabilities_for_role("auditor") == ["critique_writer"]

    def test_expert_prefix(self):
        """Generated experts (expert.*) get finding_writer."""
        assert capabilities_for_role("expert.valuation") == ["finding_writer"]
        assert capabilities_for_role("expert.systems-design") == ["finding_writer"]

    def test_unknown_role_returns_empty(self):
        assert capabilities_for_role("unknown_role") == []
        assert capabilities_for_role("") == []
