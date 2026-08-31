"""Foundation Stage 0C: the immutable policy set and fenced run context.

The policy set binds the policy versions that admission evaluated. It
never contains a live owner, grant, lease, cancellation, pause, or
deadline value; an authority boundary resolves live access grants and
run controls again on every mutation.

The run context is the trusted frozen input that the host creates for
one run. It carries references to the live authorities — the task
fence, the lease, and the run-control row — and never freezes their
live values.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from core.digest_profile import digest_hex
from core.run_contracts import (
    RunContractError,
    RuntimeAdmission,
    VersionSet,
    compile_run_admission,
)

if TYPE_CHECKING:
    from core.variants import RuntimeKey

POLICY_SET_DIGEST_DOMAIN = "policy-set"

# Live control values that never appear inside a frozen contract.
LIVE_CONTROL_FIELD_TOKENS = (
    "owner",
    "grant",
    "lease_expires",
    "expiry",
    "cancellation",
    "pause",
    "deadline",
)


class PolicyDigestMismatchError(RunContractError):
    """The declared policy-set digest does not match the object."""


class SeedPolicy(StrEnum):
    """The registered seed policies for one run admission."""

    RECORDED = "recorded"
    APPLIED = "applied"
    NONE = "none"


def _require_hex_digest(field_name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RunContractError(
            f"{field_name} must be one lowercase hexadecimal SHA-256 digest"
        )
    return value


@dataclass(frozen=True)
class PolicySet:
    """The immutable policy identity of one admitted run."""

    schema_version: str
    access_policy_digest: str
    model_policy_digest: str
    tool_policy_digest: str
    environment_policy_digest: str
    source_trust_policy_digest: str
    redaction_policy_digest: str
    retention_policy_digest: str

    def __post_init__(self) -> None:
        if not self.schema_version or not isinstance(self.schema_version, str):
            raise RunContractError(
                "schema_version must be one non-empty string"
            )
        for spec in fields(self):
            if spec.name == "schema_version":
                continue
            _require_hex_digest(spec.name, getattr(self, spec.name))

    def to_dict(self) -> dict[str, str]:
        """Return the canonical JSON shape of this policy set."""
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}

    def digest(self) -> str:
        """Return the policy-set digest under the bmas-digest profile."""
        return digest_hex(POLICY_SET_DIGEST_DOMAIN, self.to_dict())


def validate_policy_set_digest(
    policy_set: PolicySet, declared_digest: str,
) -> str:
    """Validate one declared policy-set digest or fail closed."""
    computed = policy_set.digest()
    if computed != declared_digest:
        raise PolicyDigestMismatchError(
            "The declared policy-set digest does not match the policy set"
        )
    return computed


@dataclass(frozen=True)
class RunContext:
    """The trusted frozen context of one run.

    The host creates this context; the model cannot change any field.
    The context references the live authorities through ``task_fence``,
    ``lease_ref``, and ``run_control_ref``. It never freezes an owner
    scope, a lease owner, an expiry, a pause, a cancellation, or a
    deadline value, because those change while an activation runs.
    """

    task_id: str
    run_id: str
    runtime_key: RuntimeKey
    version_set: VersionSet
    effective_spec_digest: str
    capability_digest: str
    asset_manifest_id: str
    asset_manifest_digest: str
    seed_policy: SeedPolicy
    requested_seed: int | str | None
    task_fence: str
    lease_ref: str
    run_control_ref: str
    policy_set: PolicySet
    policy_set_digest: str


def assert_frozen_contract_holds_no_live_value(contract_type: type) -> None:
    """Reject a frozen contract that embeds a live control value."""
    for spec in fields(contract_type):
        for token in LIVE_CONTROL_FIELD_TOKENS:
            if token in spec.name:
                raise RunContractError(
                    f"{contract_type.__name__}.{spec.name} freezes the live "
                    f"control value {token!r}"
                )


def compile_fenced_admission(
    *,
    policy_set: PolicySet,
    policy_set_digest: str,
    **admission_arguments: Any,
) -> RuntimeAdmission:
    """Compile one run admission behind the policy-set digest gate.

    A policy-set digest mismatch blocks the admission before the
    routing compiler runs.
    """
    validate_policy_set_digest(policy_set, policy_set_digest)
    return compile_run_admission(**admission_arguments)


def create_run_context(
    *,
    admission: RuntimeAdmission,
    policy_set: PolicySet,
    policy_set_digest: str,
    asset_manifest_id: str,
    asset_manifest_digest: str,
    task_fence: str,
    lease_ref: str,
    run_control_ref: str,
) -> RunContext:
    """Create the trusted run context from one compiled admission.

    A policy-set digest mismatch blocks context creation. The context
    copies the frozen routing identity from the admission and binds the
    authorized asset manifest, so a blueprint step always receives the
    manifest through this context.
    """
    validate_policy_set_digest(policy_set, policy_set_digest)
    _require_hex_digest("asset_manifest_digest", asset_manifest_digest)
    if not asset_manifest_id:
        raise RunContractError(
            "A run context requires the authorized asset manifest"
        )
    for name, value in (
        ("task_fence", task_fence),
        ("lease_ref", lease_ref),
        ("run_control_ref", run_control_ref),
    ):
        if not value or not isinstance(value, str):
            raise RunContractError(f"{name} must be one non-empty string")
    return RunContext(
        task_id=admission.task_id,
        run_id=admission.run_id,
        runtime_key=admission.runtime_key,
        version_set=admission.version_set,
        effective_spec_digest=admission.specification_digest,
        capability_digest=admission.capability_document_digest,
        asset_manifest_id=asset_manifest_id,
        asset_manifest_digest=asset_manifest_digest,
        seed_policy=SeedPolicy(admission.seed_policy),
        requested_seed=admission.requested_seed,
        task_fence=task_fence,
        lease_ref=lease_ref,
        run_control_ref=run_control_ref,
        policy_set=policy_set,
        policy_set_digest=policy_set_digest,
    )
