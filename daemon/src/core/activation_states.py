"""Foundation activation, dispatch, and effect state machines.

Each machine is one versioned transition table and one pure validator.
The tables come from the Foundation implementation plan. A validator
returns the registered condition tag for one allowed transition and
fails closed on every unknown state, unknown extension, or undeclared
transition.

The activation schema can add a registered runtime state in a later
version. Each extension maps to one shared lifecycle class and
declares all of its transitions. PatchBoard uses the shared wait
states without a private state extension.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TRANSITION_TABLE_VERSION = "1"


class StateMachineError(ValueError):
    """A state machine rule failed closed."""


class UnknownStateError(StateMachineError):
    """The state is not registered. The transition fails closed."""


class UndeclaredTransitionError(StateMachineError):
    """The transition is not in the versioned table. It fails closed."""


class TerminalStateError(UndeclaredTransitionError):
    """The current state is terminal. A retry creates a new attempt."""


# ── Activation states ────────────────────────────────────────────────

ACTIVATION_STATES = (
    "queued",
    "leased",
    "dispatch_queued",
    "dispatched",
    "result_received",
    "proposal_recorded",
    "awaiting_gate",
    "awaiting_human",
    "suspended",
    "resume_queued",
    "committed",
    "cancelled",
    "abandoned",
)

# ``committed`` means one accepted or rejected proposal decision
# reached the journal. It does not always mean runtime state changed.
ACTIVATION_TERMINAL_STATES = frozenset({"committed", "cancelled", "abandoned"})

ACTIVATION_WAIT_STATES = frozenset({"awaiting_gate", "awaiting_human"})

# The versioned activation transition table. Each entry maps one
# allowed (current, next) pair to its required condition tag.
ACTIVATION_TRANSITIONS: dict[tuple[str, str], str] = {
    ("queued", "leased"): "atomic_claim_stores_new_lease",
    ("queued", "cancelled"): "no_dispatch_obligation",
    ("queued", "abandoned"): "no_dispatch_obligation",
    ("leased", "queued"): "lease_expired_before_dispatch_queuing",
    ("leased", "dispatch_queued"): "claimant_owns_fences_and_reservation",
    ("leased", "proposal_recorded"): "validation_resume_revalidates_proposal",
    ("leased", "cancelled"): "no_dispatch_obligation",
    ("leased", "abandoned"): "no_dispatch_obligation",
    ("dispatch_queued", "dispatched"): "agent_accepts_authenticated_grant",
    ("dispatch_queued", "cancelled"): "host_proves_transport_not_started",
    ("dispatch_queued", "abandoned"): "valid_acknowledgement_rejects_grant",
    ("dispatched", "result_received"): "protected_authenticated_observation",
    ("dispatched", "suspended"): "effect_outcome_unknown",
    ("result_received", "proposal_recorded"): "parsing_succeeds",
    ("result_received", "cancelled"): "no_proposal_can_commit",
    ("result_received", "abandoned"): "no_proposal_can_commit",
    ("proposal_recorded", "committed"): "unit_of_work_records_decision",
    ("proposal_recorded", "awaiting_gate"): "proposal_digest_persisted",
    ("proposal_recorded", "awaiting_human"): "proposal_digest_persisted",
    ("awaiting_gate", "resume_queued"): "durable_decision_permits_revalidation",
    ("awaiting_human", "resume_queued"): (
        "durable_decision_permits_revalidation"
    ),
    ("awaiting_gate", "suspended"): "policy_pauses_work",
    ("awaiting_gate", "cancelled"): "policy_cancels_work",
    ("awaiting_human", "suspended"): "policy_pauses_work",
    ("awaiting_human", "cancelled"): "policy_cancels_work",
    ("suspended", "resume_queued"): "control_decision_records_reason",
    ("suspended", "cancelled"): "control_decision_records_reason",
    ("suspended", "abandoned"): "control_decision_records_reason",
    ("resume_queued", "leased"): "new_lease_starts_full_revalidation",
}

# ── Activation-dispatch row states ───────────────────────────────────

ACTIVATION_DISPATCH_STATES = (
    "queued",
    "claimed",
    "delivery_unknown",
    "acknowledged",
    "cancelled",
    "dead_letter",
)

ACTIVATION_DISPATCH_TERMINAL_STATES = frozenset(
    {"acknowledged", "cancelled", "dead_letter"},
)

ACTIVATION_DISPATCH_TRANSITIONS: dict[tuple[str, str], str] = {
    ("queued", "claimed"): "dispatcher_commits_owner_fence_and_expiry",
    ("queued", "cancelled"): "cancellation_live_without_claim_or_send_start",
    ("queued", "dead_letter"): "grant_expired_and_policy_forbids_delivery",
    ("claimed", "queued"): "claim_expired_without_send_start_marker",
    ("claimed", "delivery_unknown"): "claim_expired_after_send_start_marker",
    ("claimed", "acknowledged"): "valid_accepted_acknowledgement_commits",
    ("claimed", "cancelled"): (
        "claimant_proves_no_transport_and_cancellation_live"
    ),
    ("claimed", "dead_letter"): "valid_rejected_acknowledgement_commits",
    ("delivery_unknown", "claimed"): (
        "same_byte_redelivery_after_live_checks"
    ),
    ("delivery_unknown", "acknowledged"): (
        "accepted_acknowledgement_before_grant_expiry"
    ),
    ("delivery_unknown", "cancelled"): (
        "authenticated_non_acceptance_without_child_effect"
    ),
    ("delivery_unknown", "dead_letter"): (
        "valid_rejection_or_authenticated_recovery_decision"
    ),
}

# ── External-effect attempt states ───────────────────────────────────

EFFECT_ATTEMPT_STATES = (
    "intent",
    "approved",
    "dispatch_queued",
    "dispatch_claimed",
    "outcome_unknown",
    "observed",
    "reconciled",
    "denied",
    "cancelled",
)

EFFECT_TERMINAL_STATES = frozenset({"reconciled", "denied", "cancelled"})

# The states before any transport could have started. An effect
# attempt never returns to one of these after transport could have
# started; a transport retry creates a new attempt instead.
EFFECT_PRE_TRANSPORT_STATES = frozenset(
    {"intent", "approved", "dispatch_queued"},
)

EFFECT_TRANSITIONS: dict[tuple[str, str], str] = {
    ("intent", "approved"): "policy_and_budget_checks_pass",
    ("intent", "denied"): "no_dispatch_obligation",
    ("intent", "cancelled"): "no_dispatch_obligation",
    ("approved", "dispatch_queued"): "unit_of_work_creates_outbox_row",
    ("approved", "cancelled"): "host_proves_no_dispatch_exists",
    ("dispatch_queued", "dispatch_claimed"): (
        "dispatcher_owns_durable_dispatch_lease"
    ),
    ("dispatch_queued", "cancelled"): "no_dispatcher_started_transport",
    ("dispatch_claimed", "observed"): (
        "verified_response_or_provider_lookup_proves_outcome"
    ),
    ("dispatch_claimed", "outcome_unknown"): (
        "transport_could_have_started_without_proven_outcome"
    ),
    ("dispatch_claimed", "dispatch_queued"): (
        "current_dispatcher_proves_no_transport_start_marker"
    ),
    ("outcome_unknown", "observed"): (
        "lookup_or_late_receipt_proves_outcome"
    ),
    ("outcome_unknown", "reconciled"): (
        "operator_records_irrecoverable_unknown_outcome"
    ),
    ("observed", "reconciled"): "outcome_and_usage_reconciliation_finish",
}

# ── Shared lifecycle classes and state extensions ────────────────────

# Every activation state maps to one shared lifecycle class, so a
# registered extension state joins one known class.
LIFECYCLE_CLASSES = ("pending", "active", "waiting", "terminal")

ACTIVATION_LIFECYCLE_CLASS: dict[str, str] = {
    "queued": "pending",
    "leased": "active",
    "dispatch_queued": "active",
    "dispatched": "active",
    "result_received": "active",
    "proposal_recorded": "active",
    "awaiting_gate": "waiting",
    "awaiting_human": "waiting",
    "suspended": "waiting",
    "resume_queued": "pending",
    "committed": "terminal",
    "cancelled": "terminal",
    "abandoned": "terminal",
}


@dataclass(frozen=True)
class StateExtension:
    """One registered runtime-specific activation state extension."""

    state: str
    lifecycle_class: str
    transitions: dict[tuple[str, str], str] = field(default_factory=dict)


class ActivationStateRegistry:
    """The shared activation states plus registered extensions.

    An unknown state or an undeclared transition fails closed. Every
    extension transition must name the extension state on at least one
    side, and every other endpoint must be a known state.
    """

    def __init__(self) -> None:
        self._extensions: dict[str, StateExtension] = {}

    def register(self, extension: StateExtension) -> None:
        """Register one extension state with its declared transitions."""
        if extension.state in ACTIVATION_STATES:
            raise StateMachineError(
                f"{extension.state!r} is already a shared activation state"
            )
        if extension.lifecycle_class not in LIFECYCLE_CLASSES:
            raise StateMachineError(
                f"Unknown lifecycle class: {extension.lifecycle_class!r}"
            )
        if not extension.transitions:
            raise StateMachineError(
                "An extension declares all of its transitions"
            )
        known = set(ACTIVATION_STATES) | {extension.state}
        for current, target in extension.transitions:
            if extension.state not in (current, target):
                raise StateMachineError(
                    "An extension transition must involve the extension state"
                )
            if current not in known or target not in known:
                raise StateMachineError(
                    f"Unknown transition endpoint: {current!r} -> {target!r}"
                )
        self._extensions[extension.state] = extension

    def lifecycle_class(self, state: str) -> str:
        """Return the lifecycle class of one known state."""
        if state in ACTIVATION_LIFECYCLE_CLASS:
            return ACTIVATION_LIFECYCLE_CLASS[state]
        extension = self._extensions.get(state)
        if extension is None:
            raise UnknownStateError(f"Unknown activation state: {state!r}")
        return extension.lifecycle_class

    def validate_transition(self, current: str, target: str) -> str:
        """Validate one transition against the shared and extended tables."""
        self.lifecycle_class(current)
        self.lifecycle_class(target)
        if self.lifecycle_class(current) == "terminal":
            raise TerminalStateError(
                f"{current!r} is terminal; a retry creates a new attempt"
            )
        condition = ACTIVATION_TRANSITIONS.get((current, target))
        if condition is not None:
            return condition
        for extension in self._extensions.values():
            condition = extension.transitions.get((current, target))
            if condition is not None:
                return condition
        raise UndeclaredTransitionError(
            f"The activation transition {current!r} -> {target!r} is not "
            "declared"
        )


def _validate_from_table(
    current: str,
    target: str,
    *,
    states: tuple[str, ...],
    table: dict[tuple[str, str], str],
    terminal: frozenset[str],
    machine: str,
) -> str:
    if current not in states:
        raise UnknownStateError(f"Unknown {machine} state: {current!r}")
    if target not in states:
        raise UnknownStateError(f"Unknown {machine} state: {target!r}")
    if current in terminal:
        raise TerminalStateError(
            f"{current!r} is a terminal {machine} state"
        )
    condition = table.get((current, target))
    if condition is None:
        raise UndeclaredTransitionError(
            f"The {machine} transition {current!r} -> {target!r} is not "
            "declared"
        )
    return condition


def validate_activation_transition(current: str, target: str) -> str:
    """Validate one shared activation transition without extensions."""
    return _validate_from_table(
        current,
        target,
        states=ACTIVATION_STATES,
        table=ACTIVATION_TRANSITIONS,
        terminal=ACTIVATION_TERMINAL_STATES,
        machine="activation",
    )


def validate_activation_dispatch_transition(current: str, target: str) -> str:
    """Validate one activation-dispatch row transition."""
    return _validate_from_table(
        current,
        target,
        states=ACTIVATION_DISPATCH_STATES,
        table=ACTIVATION_DISPATCH_TRANSITIONS,
        terminal=ACTIVATION_DISPATCH_TERMINAL_STATES,
        machine="activation-dispatch",
    )


def validate_effect_transition(current: str, target: str) -> str:
    """Validate one external-effect attempt transition."""
    condition = _validate_from_table(
        current,
        target,
        states=EFFECT_ATTEMPT_STATES,
        table=EFFECT_TRANSITIONS,
        terminal=EFFECT_TERMINAL_STATES,
        machine="effect",
    )
    return condition
