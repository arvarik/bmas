# /opt/bmas/daemon/src/core/capabilities.py
"""Capability-based authorization for the Board Gateway (doc 04 §4).

Authorization is capability-based, not role-name-based (seam rule 4).
The gateway calls authorize() with the actor's capability list; it
never inspects the actor string to decide authorization.

The variant decides which capabilities an actor gets —
capabilities_for_role() is a convenience for the traditional variant,
never used by the gateway itself.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.protocol import (
    ENTRY_TYPE_ARTIFACT,
    ENTRY_TYPE_ATTACHMENT,
    ENTRY_TYPE_CONFLICT,
    ENTRY_TYPE_CRITIQUE,
    ENTRY_TYPE_DIRECTIVE,
    ENTRY_TYPE_FINDING,
    ENTRY_TYPE_OBJECTIVE,
    ENTRY_TYPE_PLAN,
    ENTRY_TYPE_REBUTTAL,
    ENTRY_TYPE_SOLUTION,
)

# ── Capability Profiles (doc 04 §4) ─────────────────────────────────

@dataclass(frozen=True)
class CapabilityProfile:
    """Defines what a capability profile may/may not do."""
    may_post: frozenset[str]      # entry types this profile can post
    may_remove: frozenset[str]    # entry types this profile can remove
    may_never: frozenset[str]     # entry types this profile can never post


# Table from doc 04 §4
CAPABILITY_PROFILES: dict[str, CapabilityProfile] = {
    "plan_writer": CapabilityProfile(
        may_post=frozenset({ENTRY_TYPE_PLAN}),
        may_remove=frozenset(),
        may_never=frozenset({ENTRY_TYPE_SOLUTION}),
    ),
    "finding_writer": CapabilityProfile(
        may_post=frozenset({ENTRY_TYPE_FINDING, ENTRY_TYPE_REBUTTAL}),
        may_remove=frozenset(),
        may_never=frozenset({ENTRY_TYPE_SOLUTION}),
    ),
    "critique_writer": CapabilityProfile(
        may_post=frozenset({ENTRY_TYPE_CRITIQUE}),
        may_remove=frozenset(),
        may_never=frozenset({ENTRY_TYPE_SOLUTION, ENTRY_TYPE_FINDING}),
    ),
    "conflict_mediator": CapabilityProfile(
        may_post=frozenset({ENTRY_TYPE_CONFLICT, ENTRY_TYPE_REBUTTAL}),
        may_remove=frozenset(),
        may_never=frozenset({ENTRY_TYPE_SOLUTION}),
    ),
    "board_maintenance": CapabilityProfile(
        may_post=frozenset({ENTRY_TYPE_FINDING}),  # can post condensed findings
        may_remove=frozenset(  # can remove any non-objective/non-solution
            {ENTRY_TYPE_PLAN, ENTRY_TYPE_FINDING, ENTRY_TYPE_CRITIQUE,
             ENTRY_TYPE_REBUTTAL, ENTRY_TYPE_CONFLICT, ENTRY_TYPE_DIRECTIVE,
             ENTRY_TYPE_ATTACHMENT, ENTRY_TYPE_ARTIFACT}
        ),
        may_never=frozenset(),
    ),
    "decision_writer": CapabilityProfile(
        may_post=frozenset(
            {ENTRY_TYPE_OBJECTIVE, ENTRY_TYPE_DIRECTIVE, ENTRY_TYPE_SOLUTION}
        ),
        may_remove=frozenset(),
        may_never=frozenset(),
    ),
}


class AuthorizationError(Exception):
    """Raised when an actor lacks the capability for an action."""
    def __init__(self, reason: str, actor: str = "", entry_type: str = ""):
        self.reason = reason
        self.actor = actor
        self.entry_type = entry_type
        super().__init__(reason)


def authorize_post(capabilities: list[str], entry_type: str) -> None:
    """Check if the given capabilities allow posting an entry of this type.

    Raises AuthorizationError if not authorized.

    The check is: at least one capability in the list must include the
    entry_type in its may_post set, AND no capability must include it
    in its may_never set.

    Also supports direct ``post:TYPE`` capability strings (e.g.
    ``post:attachment``, ``post:artifact``) for daemon-originated entries.
    """
    if not capabilities:
        raise AuthorizationError(
            reason=f"No capabilities provided; cannot post '{entry_type}'",
            entry_type=entry_type,
        )

    # Check may_never across ALL capabilities — any deny is final
    for cap_name in capabilities:
        profile = CAPABILITY_PROFILES.get(cap_name)
        if profile and entry_type in profile.may_never:
            raise AuthorizationError(
                reason=(
                    f"Capability '{cap_name}' explicitly denies "
                    f"posting '{entry_type}'"
                ),
                entry_type=entry_type,
            )

    # Check may_post — at least one capability must allow it
    allowed = False
    for cap_name in capabilities:
        # Direct post:TYPE capability (daemon-originated entries)
        if cap_name == f"post:{entry_type}":
            allowed = True
            break
        profile = CAPABILITY_PROFILES.get(cap_name)
        if profile and entry_type in profile.may_post:
            allowed = True
            break

    if not allowed:
        raise AuthorizationError(
            reason=(
                f"No capability in {capabilities} allows "
                f"posting '{entry_type}'"
            ),
            entry_type=entry_type,
        )


def authorize_remove(
    capabilities: list[str], entry_type: str
) -> None:
    """Check if the given capabilities allow removing an entry of this type.

    Raises AuthorizationError if not authorized.
    """
    if not capabilities:
        raise AuthorizationError(
            reason=f"No capabilities provided; cannot remove '{entry_type}'",
            entry_type=entry_type,
        )

    # Objective and solution entries can never be removed
    if entry_type in (ENTRY_TYPE_OBJECTIVE, ENTRY_TYPE_SOLUTION):
        raise AuthorizationError(
            reason=f"Entries of type '{entry_type}' cannot be removed",
            entry_type=entry_type,
        )

    allowed = False
    for cap_name in capabilities:
        profile = CAPABILITY_PROFILES.get(cap_name)
        if profile and entry_type in profile.may_remove:
            allowed = True
            break

    if not allowed:
        raise AuthorizationError(
            reason=(
                f"No capability in {capabilities} allows "
                f"removing '{entry_type}'"
            ),
            entry_type=entry_type,
        )


# ── Traditional Variant Convenience ──────────────────────────────────
#
# The traditional variant maps roles to capability profiles.
# This is a convenience; the gateway itself never calls this.

ROLE_CAPABILITIES: dict[str, list[str]] = {
    "planner": ["plan_writer"],
    "critic": ["critique_writer"],
    "conflict_resolver": ["conflict_mediator"],
    "cleaner": ["board_maintenance"],
    "decider": ["decision_writer"],
    # executor/auditor are legacy aliases (doc 04 §4 note)
    "executor": ["finding_writer"],
    "auditor": ["critique_writer"],
}

# expert.* gets finding_writer
_EXPERT_CAP_PREFIX = "expert."


def capabilities_for_role(role: str) -> list[str]:
    """Map a traditional-variant role to its capability profile(s).

    For generated experts (expert.*), returns ["finding_writer"].
    For unknown roles, returns an empty list (the gateway will reject).

    This function is used ONLY by the traditional variant to populate
    the capabilities list before calling the gateway.  The gateway
    itself is capability-agnostic.
    """
    if role in ROLE_CAPABILITIES:
        return list(ROLE_CAPABILITIES[role])
    if role.startswith(_EXPERT_CAP_PREFIX):
        return ["finding_writer"]
    return []
