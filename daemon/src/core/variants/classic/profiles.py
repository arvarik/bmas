"""The fidelity profiles, the effort presets, the alias table, and the caps.

A fidelity profile selects coordination semantics and fixes the fields
a later layer cannot change. An effort preset selects resource
intensity and procedure strength. The alias table maps the shipped
Classic effort levels to the preset classes. The deployment cap policy
names the final allowed range of every bounded field. Each table
carries its own version string, and the compiled specification records
the versions it used.

Every value names a specification field by its dotted path. A money
value travels as a decimal string here and becomes exact nanos in the
compiled specification.
"""
from __future__ import annotations

from typing import Any

from core.variants.classic.spec import ClassicEffortProfile, ClassicFidelityProfile

# ── Schema defaults ───────────────────────────────────────────────────
# The value every field holds before any layer applies. A deployment
# setting or a profile changes most of them; the rest are the values
# the engine applies today and records here so a historical run never
# reads them from a newer default.

SCHEMA_DEFAULTS: dict[str, Any] = {
    "team.experts_by_tier.simple": 0,
    "team.experts_by_tier.light": 1,
    "team.experts_by_tier.medium": 2,
    "team.experts_by_tier.complex": 4,
    "team.model_assignment": "capability_round_robin",
    "team.independent_model_families": 1,
    "models.temperature": None,
    "randomness.candidate_order": "board_order",
    "coordination.control_strategy": "model_ranked",
    "coordination.proposal_mode": "shared_board",
    "coordination.round_execution": "concurrent",
    "coordination.max_parallel_agents": 3,
    "coordination.max_rounds": 4,
    "coordination.narration": False,
    "board.view_strategy": "role_bounded",
    "board.view_budget_tokens": 12000,
    "board.max_title_characters": 200,
    "board.max_entry_body_characters": 8000,
    "board.retain_counterevidence": True,
    "board.salience_weights.confidence": 0.4,
    "board.salience_weights.recency": 0.2,
    "board.salience_weights.refs_in": 0.3,
    "board.salience_weights.penalty": 0.3,
    "board.salience_weights.operator_boost_factor": 2.0,
    "cleaner.enabled": True,
    "cleaner.policy_id": "salience_condensation",
    "cleaner.policy_version": "salience-condensation/1",
    "cleaner.entry_threshold": 12,
    "cleaner.token_threshold": 8000,
    "cleaner.retain_recent_rounds": 2,
    "cleaner.retention_weights.salience": 2.0,
    "cleaner.retention_weights.confidence": 1.0,
    "cleaner.retention_weights.recency": 0.1,
    "cleaner.retention_weights.size_penalty": 0.01,
    "cleaner.retention_weights.minority_claim": 0.0,
    "memory.actor_memory": "none",
    "memory.provider_session_scope": "task",
    "memory.durable_goals": False,
    "memory.causal_retrieval": False,
    "verification.evidence_policy": "optional_sources",
    "verification.solution_review": "required",
    "verification.independent_verifiers": 0,
    "verification.critical_claims_only": False,
    "verification.adversarial_critic": False,
    "verification.final_verification": False,
    "consensus.strategy": "token_similarity",
    "consensus.strategy_version": "token-similarity/1",
    # The vote selects the answer with the highest summed similarity.
    # No minimum score applies, so the threshold records zero.
    "consensus.threshold": 0.0,
    "consensus.failure_behavior": "continue_work",
    "limits.max_cost": "0.50",
    "limits.max_duration_seconds": 1800,
    "limits.max_input_tokens": 250000,
    "limits.max_output_tokens": 50000,
    "limits.strict_pricing": False,
    "recovery.stall_rounds": 2,
    "recovery.max_replans": 2,
    "recovery.checkpoint_every_rounds": 1,
    "recovery.loop_detection": "standard",
    "recovery.fault_policy": "retry_then_replan",
    "termination.policy_id": "verified_candidate",
    "termination.policy_version": "verified-candidate/1",
    "termination.require_completion_evidence": False,
    "termination.allow_empty_candidate_set": False,
}

# ── Fidelity profiles ─────────────────────────────────────────────────

FIDELITY_PROFILES: dict[str, ClassicFidelityProfile] = {
    "paper_aligned": ClassicFidelityProfile(
        profile_id="paper_aligned",
        profile_version="paper-aligned/1",
        description=(
            "Four sequential rounds over the full board, no cleaner, no actor "
            "memory, seeded random model assignment, the pinned "
            "solution-extraction vote, and an empty verifier set. Only "
            "platform integrity gates apply."
        ),
        values={
            "coordination.round_execution": "sequential",
            "coordination.max_rounds": 4,
            "coordination.control_strategy": "model_ranked",
            "coordination.proposal_mode": "shared_board",
            "board.view_strategy": "full_board",
            "board.retain_counterevidence": True,
            "cleaner.enabled": False,
            "memory.actor_memory": "none",
            "memory.provider_session_scope": "activation",
            "memory.durable_goals": False,
            "memory.causal_retrieval": False,
            "verification.evidence_policy": "optional_sources",
            "verification.solution_review": "not_required",
            "verification.independent_verifiers": 0,
            "verification.critical_claims_only": False,
            "verification.adversarial_critic": False,
            "verification.final_verification": False,
            "consensus.strategy": "token_similarity",
            "consensus.failure_behavior": "continue_work",
            "team.model_assignment": "seeded_random",
            "team.independent_model_families": 1,
            "models.temperature": 0.7,
            "limits.strict_pricing": False,
            "recovery.fault_policy": "replan_only",
            "termination.policy_id": "paper_aligned_candidate",
            "termination.policy_version": "paper-aligned-candidate/1",
            "termination.require_completion_evidence": False,
            "termination.allow_empty_candidate_set": True,
        },
        # The round execution, the view strategy, the cleaner, the actor
        # memory, the verifier set, the pinned vote, and the termination
        # policy never change through a later layer.
        fixed_fields=[
            "coordination.round_execution",
            "board.view_strategy",
            "cleaner.enabled",
            "memory.actor_memory",
            "verification.independent_verifiers",
            "verification.adversarial_critic",
            "verification.final_verification",
            "consensus.strategy",
            "termination.policy_id",
        ],
    ),
    "production_safe": ClassicFidelityProfile(
        profile_id="production_safe",
        profile_version="production-safe/1",
        description=(
            "Bounded role views, typed claims and evidence, a required "
            "solution review, strict budget reservations, durable goals and "
            "recovery, and host-owned actor memory."
        ),
        values={
            "coordination.control_strategy": "model_ranked",
            "coordination.proposal_mode": "shared_board",
            "board.view_strategy": "role_bounded",
            "board.retain_counterevidence": True,
            "cleaner.enabled": True,
            "memory.actor_memory": "host_owned",
            "memory.provider_session_scope": "task",
            "memory.durable_goals": True,
            "memory.causal_retrieval": True,
            "verification.evidence_policy": "typed_sources",
            "verification.solution_review": "required",
            "verification.independent_verifiers": 1,
            "verification.critical_claims_only": False,
            "verification.adversarial_critic": False,
            "verification.final_verification": True,
            "consensus.strategy": "token_similarity",
            "consensus.failure_behavior": "use_verified_single_candidate",
            "team.model_assignment": "capability_round_robin",
            "team.independent_model_families": 1,
            "models.temperature": None,
            "limits.strict_pricing": True,
            "recovery.fault_policy": "retry_then_replan",
            "termination.policy_id": "verified_candidate",
            "termination.policy_version": "verified-candidate/1",
            "termination.require_completion_evidence": True,
            "termination.allow_empty_candidate_set": False,
        },
        fixed_fields=[
            "board.view_strategy",
            "memory.actor_memory",
            "limits.strict_pricing",
            "termination.policy_id",
        ],
    ),
}

# ── Effort presets ────────────────────────────────────────────────────

EFFORT_PROFILES: dict[str, ClassicEffortProfile] = {
    "quick": ClassicEffortProfile(
        profile_id="quick",
        profile_version="quick/1",
        description=(
            "One or two experts, bounded views, low round and cost limits, "
            "and one verifier for critical claims only."
        ),
        values={
            "team.experts_by_tier.simple": 0,
            "team.experts_by_tier.light": 1,
            "team.experts_by_tier.medium": 1,
            "team.experts_by_tier.complex": 2,
            "board.view_strategy": "role_bounded",
            "coordination.max_rounds": 2,
            "limits.max_duration_seconds": 900,
            "limits.max_cost": "0.25",
            "recovery.stall_rounds": 1,
            "recovery.max_replans": 1,
            "verification.solution_review": "not_required",
            "verification.independent_verifiers": 1,
            "verification.critical_claims_only": True,
        },
        overridable_by_deployment=False,
    ),
    "balanced": ClassicEffortProfile(
        profile_id="balanced",
        profile_version="balanced/1",
        description=(
            "Sequential scheduling, bounded role views, typed evidence, one "
            "independent verifier, and four rounds. The deployment settings "
            "override these documented defaults."
        ),
        values={
            "coordination.round_execution": "sequential",
            "coordination.max_rounds": 4,
            "board.view_strategy": "role_bounded",
            "verification.evidence_policy": "typed_sources",
            "verification.independent_verifiers": 1,
        },
        overridable_by_deployment=True,
    ),
    "rigorous": ClassicEffortProfile(
        profile_id="rigorous",
        profile_version="rigorous/1",
        description=(
            "An independent proposal phase, two model families when "
            "available, strong evidence gates, an adversarial critic, and "
            "final verification."
        ),
        values={
            "coordination.proposal_mode": "blind_independent",
            "coordination.max_rounds": 12,
            "team.independent_model_families": 2,
            "verification.evidence_policy": "typed_sources_strict",
            "verification.adversarial_critic": True,
            "verification.final_verification": True,
            "verification.solution_review": "required",
            "verification.independent_verifiers": 1,
            "memory.provider_session_scope": "activation",
            "limits.max_duration_seconds": 3600,
            "limits.max_cost": "2.00",
            "recovery.stall_rounds": 2,
            "recovery.max_replans": 3,
        },
        overridable_by_deployment=False,
    ),
    "exploratory": ClassicEffortProfile(
        profile_id="exploratory",
        profile_version="exploratory/1",
        description=(
            "Blind independent proposals, strong minority-claim retention, "
            "recorded candidate randomization, and disagreement diagnostics."
        ),
        values={
            "coordination.proposal_mode": "blind_independent",
            "coordination.max_rounds": 8,
            "cleaner.retention_weights.minority_claim": 1.0,
            "randomness.candidate_order": "recorded_random",
            "board.retain_counterevidence": True,
            "limits.max_duration_seconds": 3600,
            "limits.max_cost": "2.00",
        },
        overridable_by_deployment=False,
    ),
    "long_horizon": ClassicEffortProfile(
        profile_id="long_horizon",
        profile_version="long-horizon/1",
        description=(
            "A goal graph and durable ledger, more action and time budget, "
            "frequent checkpoints, and strong loop and forgetting checks."
        ),
        values={
            "coordination.max_rounds": 32,
            "memory.durable_goals": True,
            "memory.provider_session_scope": "activation",
            "recovery.checkpoint_every_rounds": 1,
            "recovery.loop_detection": "strong",
            "recovery.stall_rounds": 3,
            "recovery.max_replans": 5,
            "verification.evidence_policy": "typed_sources",
            "verification.solution_review": "required",
            "limits.max_duration_seconds": 10800,
            "limits.max_cost": "10.00",
        },
        overridable_by_deployment=False,
    ),
    "adversarial": ClassicEffortProfile(
        profile_id="adversarial",
        profile_version="adversarial/1",
        description=(
            "Red-team critic activations, stronger evidence gates, and "
            "required independent verification of every critical claim."
        ),
        values={
            "coordination.max_rounds": 8,
            "verification.adversarial_critic": True,
            "verification.evidence_policy": "typed_sources_strict",
            "verification.independent_verifiers": 1,
            "verification.critical_claims_only": False,
            "verification.final_verification": True,
            "verification.solution_review": "required",
            "limits.max_duration_seconds": 3600,
            "limits.max_cost": "2.00",
        },
        overridable_by_deployment=False,
    ),
}

# The shipped Classic effort levels map to the preset classes through
# this table. A preset class name is also a valid requested level.
EFFORT_ALIASES: dict[str, str] = {
    "quick": "quick",
    "standard": "balanced",
    "thorough": "rigorous",
    "exhaustive": "long_horizon",
}
EFFORT_ALIAS_TABLE_VERSION = "classic-effort-aliases/1"
DEFAULT_FIDELITY = "production_safe"
DEFAULT_EFFORT_LEVEL = "standard"


def valid_effort_levels() -> tuple[str, ...]:
    """Every accepted effort value: the shipped levels, then the presets."""
    presets = [name for name in EFFORT_PROFILES if name not in EFFORT_ALIASES]
    return (*EFFORT_ALIASES, *presets)


def resolve_effort_level(value: Any) -> tuple[str, str]:
    """Return (requested level, preset class) or raise ``ValueError``."""
    level = DEFAULT_EFFORT_LEVEL if value is None else str(value).strip().lower()
    preset = EFFORT_ALIASES.get(level, level)
    if preset not in EFFORT_PROFILES:
        raise ValueError(
            f"Unknown effort level '{value}'. Valid levels: {', '.join(valid_effort_levels())}"
        )
    return level, preset


def resolve_fidelity(value: Any) -> str:
    """Return the fidelity profile identifier or raise ``ValueError``."""
    profile = DEFAULT_FIDELITY if value is None else str(value).strip().lower()
    if profile not in FIDELITY_PROFILES:
        raise ValueError(
            f"Unknown fidelity profile '{value}'. Valid profiles: {', '.join(FIDELITY_PROFILES)}"
        )
    return profile


# ── Deployment caps ───────────────────────────────────────────────────

DEPLOYMENT_CAPS_VERSION = "deployment-caps/1"

# The final allowed range of every bounded field, as (minimum, maximum).
# The bounds equal the classic settings contract the settings store
# publishes, so the interface and the compiler agree on every range.
DEPLOYMENT_CAPS: dict[str, tuple[Any, Any]] = {
    "coordination.max_rounds": (1, 50),
    "coordination.max_parallel_agents": (1, 32),
    "limits.max_duration_seconds": (30, 14400),
    "limits.max_cost": ("0.01", "1000"),
    "recovery.stall_rounds": (1, 20),
    "recovery.max_replans": (0, 20),
    "board.view_budget_tokens": (512, 200000),
    "cleaner.entry_threshold": (1, 500),
    "cleaner.token_threshold": (1, 500000),
    "team.experts_by_tier.simple": (0, 12),
    "team.experts_by_tier.light": (0, 12),
    "team.experts_by_tier.medium": (0, 12),
    "team.experts_by_tier.complex": (0, 12),
    "cleaner.retention_weights.salience": (0, 100),
    "cleaner.retention_weights.confidence": (0, 100),
    "cleaner.retention_weights.recency": (0, 100),
    "cleaner.retention_weights.size_penalty": (0, 100),
    "cleaner.retention_weights.minority_claim": (0, 100),
}
