"""Effort profiles — the deliberation lever shared by every runtime.

An effort level names how hard a runtime pushes before it accepts an
answer. Each runtime declares its own profile table: the settings one
level changes for that runtime. "standard" changes nothing, so the
session settings apply as-is.

Resolution order for one task:
    session settings  <  effort profile  <  explicit per-task overrides
"""

from __future__ import annotations

import copy
from typing import Any

EFFORT_LEVELS: tuple[str, ...] = ("quick", "standard", "thorough", "exhaustive")
DEFAULT_EFFORT = "standard"

# Operator-facing metadata. `settings` holds the runtime-specific bundle.
CLASSIC_EFFORT_PROFILES: dict[str, dict[str, Any]] = {
    "quick": {
        "label": "Quick",
        "description": "One pass and a fast answer. Skips the extra verification round.",
        "settings": {
            "max_rounds": 2,
            "max_duration_s": 900,
            "budget_ceiling_usd": 0.25,
            "stall_rounds": 1,
            "max_replans": 1,
            "grace_verification": False,
        },
    },
    "standard": {
        "label": "Standard",
        "description": "The session settings as configured. Verified when possible.",
        "settings": {},
    },
    "thorough": {
        "label": "Thorough",
        "description": "More rounds with fresh agent context each turn. The answer must pass review before a limit stop.",
        "settings": {
            "max_rounds": 12,
            "max_duration_s": 3600,
            "budget_ceiling_usd": 2.0,
            "stall_rounds": 2,
            "max_replans": 3,
            "grace_verification": True,
            "actor_context": "fresh",
        },
    },
    "exhaustive": {
        "label": "Exhaustive",
        "description": "Long-horizon run: many rounds, a high budget ceiling, strict verification. Can take an hour.",
        "settings": {
            "max_rounds": 32,
            "max_duration_s": 10800,
            "budget_ceiling_usd": 10.0,
            "stall_rounds": 3,
            "max_replans": 5,
            "grace_verification": True,
            "actor_context": "fresh",
        },
    },
}


def resolve_effort(value: Any) -> str:
    """Validate one effort value and return the canonical level name."""
    if value is None:
        return DEFAULT_EFFORT
    level = str(value).strip().lower()
    if level not in EFFORT_LEVELS:
        raise ValueError(
            f"Unknown effort level '{value}'. "
            f"Valid levels: {', '.join(EFFORT_LEVELS)}"
        )
    return level


def apply_effort_profile(
    base_settings: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    effort: str,
) -> dict[str, Any]:
    """Layer one effort profile's settings over the session settings."""
    merged = copy.deepcopy(base_settings)
    profile = profiles.get(effort) or {}
    for key, value in (profile.get("settings") or {}).items():
        merged[key] = copy.deepcopy(value)
    return merged


def public_effort_profiles(
    profiles: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the capability-document form of a profile table."""
    return {
        level: {
            "label": str(profile.get("label", level.title())),
            "description": str(profile.get("description", "")),
            "settings": copy.deepcopy(profile.get("settings") or {}),
        }
        for level, profile in profiles.items()
    }
