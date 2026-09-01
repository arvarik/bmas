"""Pin terminal-outcome semantics per runtime pair and per experiment.

One ``OutcomeMapping`` exists for every qualified runtime pair. It
maps each registered terminal reason to its benchmark class, retry
rule, missingness rule, and denominator rule. One sorted
``OutcomeMappingSet`` exists for every experiment: each member stores
the runtime pair, the mapping identifier, and the mapping digest, and
the set hashes with the Foundation canonical digest under the
``bmas/outcome-mapping-set`` identifier. The run plan pins the set
digest and one exact member per arm, the comparison invariant digest
carries only the complete set digest, and analysis rejects an unknown
reason or a stale mapping before any number computes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from core.digest_profile import digest_hex

if TYPE_CHECKING:
    from collections.abc import Mapping

# The contract version travels as metadata; it never enters an
# identifier.
OUTCOME_MAPPING_CONTRACT_VERSION = "1"
# The full domain identifier is part of the hashed payload. The digest
# framing itself uses the profile's domain charset.
OUTCOME_MAPPING_SET_IDENTIFIER = "bmas/outcome-mapping-set"
OUTCOME_MAPPING_IDENTIFIER = "bmas/outcome-mapping"
_SET_DIGEST_DOMAIN = "outcome-mapping-set"
_MAPPING_DIGEST_DOMAIN = "outcome-mapping"

BENCHMARK_CLASSES = (
    "success",
    "substantive_failure",
    "infrastructure_failure",
    "cancelled",
)
RETRY_RULES = ("allowed", "prohibited")
MISSINGNESS_RULES = ("observed", "excludable", "missing_work")
DENOMINATOR_RULES = ("unconditional", "excludable")


class OutcomeMappingError(ValueError):
    """An outcome mapping or mapping set violates its contract."""


def _validated_rules(reason: str, rules: dict[str, str]) -> dict[str, str]:
    expected = {
        "benchmark_class": BENCHMARK_CLASSES,
        "retry_rule": RETRY_RULES,
        "missingness": MISSINGNESS_RULES,
        "denominator": DENOMINATOR_RULES,
    }
    if set(rules) != set(expected):
        raise OutcomeMappingError(
            f"The reason {reason!r} needs exactly the rule fields "
            f"{sorted(expected)}"
        )
    for name, allowed in expected.items():
        if rules[name] not in allowed:
            raise OutcomeMappingError(
                f"The reason {reason!r} has an unknown {name}: "
                f"{rules[name]!r}"
            )
    return dict(rules)


@dataclass(frozen=True)
class OutcomeMapping:
    """One immutable terminal-reason mapping for one runtime pair."""

    mapping_id: str
    runtime_id: str
    runtime_contract_version: str
    reasons: Mapping[str, dict[str, str]] = field(repr=False)
    contract_version: str = OUTCOME_MAPPING_CONTRACT_VERSION

    def payload(self) -> dict[str, Any]:
        """Return the canonical mapping content for hashing."""
        return {
            "identifier": OUTCOME_MAPPING_IDENTIFIER,
            "contract_version": self.contract_version,
            "mapping_id": self.mapping_id,
            "runtime_id": self.runtime_id,
            "runtime_contract_version": self.runtime_contract_version,
            "reasons": {
                reason: dict(rules)
                for reason, rules in sorted(self.reasons.items())
            },
        }

    @property
    def digest(self) -> str:
        return digest_hex(_MAPPING_DIGEST_DOMAIN, self.payload())

    def member(self) -> dict[str, str]:
        """Return this mapping as one set member."""
        return {
            "runtime_id": self.runtime_id,
            "runtime_contract_version": self.runtime_contract_version,
            "mapping_id": self.mapping_id,
            "mapping_digest": self.digest,
        }

    def resolve(self, reason: str) -> dict[str, str]:
        """Resolve one terminal reason or fail closed."""
        rules = self.reasons.get(str(reason))
        if rules is None:
            raise OutcomeMappingError(
                f"The outcome reason {reason!r} is not registered for "
                f"{self.runtime_id}/{self.runtime_contract_version}"
            )
        return dict(rules)


def build_outcome_mapping(
    *,
    runtime_id: str,
    runtime_contract_version: str,
    reasons: dict[str, dict[str, str]],
    mapping_id: str | None = None,
) -> OutcomeMapping:
    """Validate and freeze one outcome mapping."""
    if not reasons:
        raise OutcomeMappingError("A mapping needs at least one reason")
    validated = {
        str(reason): _validated_rules(str(reason), rules)
        for reason, rules in reasons.items()
    }
    return OutcomeMapping(
        mapping_id=mapping_id
        or f"outcome-mapping-{runtime_id}-{runtime_contract_version}",
        runtime_id=str(runtime_id),
        runtime_contract_version=str(runtime_contract_version),
        reasons=MappingProxyType(validated),
    )


# ── The shared reason table for daemon-scheduled task runtimes ───────

# The daemon task layer normalizes every runtime's terminal state into
# these reasons, so each qualified pair registers the same table today.
# The digests still differ per pair, because the pair identity is part
# of the hashed payload.
SHARED_TASK_REASONS: dict[str, dict[str, str]] = {
    "completed": {
        "benchmark_class": "success",
        "retry_rule": "prohibited",
        "missingness": "observed",
        "denominator": "unconditional",
    },
    "execution": {
        "benchmark_class": "substantive_failure",
        "retry_rule": "prohibited",
        "missingness": "observed",
        "denominator": "unconditional",
    },
    "timeout": {
        "benchmark_class": "substantive_failure",
        "retry_rule": "prohibited",
        "missingness": "observed",
        "denominator": "unconditional",
    },
    # A treatment-caused budget stop stays a substantive failure with
    # zero success; it never leaves the unconditional denominator.
    "budget_stop": {
        "benchmark_class": "substantive_failure",
        "retry_rule": "prohibited",
        "missingness": "observed",
        "denominator": "unconditional",
    },
    "configuration": {
        "benchmark_class": "infrastructure_failure",
        "retry_rule": "allowed",
        "missingness": "excludable",
        "denominator": "excludable",
    },
    "infrastructure": {
        "benchmark_class": "infrastructure_failure",
        "retry_rule": "allowed",
        "missingness": "excludable",
        "denominator": "excludable",
    },
    "cancelled": {
        "benchmark_class": "cancelled",
        "retry_rule": "allowed",
        "missingness": "missing_work",
        "denominator": "unconditional",
    },
}

_REGISTRY: dict[tuple[str, str], OutcomeMapping] = {}


def register_outcome_mapping(mapping: OutcomeMapping) -> None:
    """Register one mapping for one runtime pair exactly once."""
    key = (mapping.runtime_id, mapping.runtime_contract_version)
    existing = _REGISTRY.get(key)
    if existing is not None and existing.digest != mapping.digest:
        raise OutcomeMappingError(
            f"The runtime pair {key} already has a different registered "
            "mapping; a changed mapping needs a new mapping set and run "
            "plan"
        )
    _REGISTRY[key] = mapping


def registered_mapping(
    runtime_id: str, runtime_contract_version: str,
) -> OutcomeMapping:
    """Return the registered mapping for one runtime pair."""
    _register_builtin_mappings()
    key = (str(runtime_id), str(runtime_contract_version))
    mapping = _REGISTRY.get(key)
    if mapping is None:
        raise OutcomeMappingError(
            f"No outcome mapping is registered for the runtime pair "
            f"{key[0]}/{key[1]}"
        )
    return mapping


_builtin_loaded = False


def _register_builtin_mappings() -> None:
    """Register one mapping for every qualified runtime pair."""
    global _builtin_loaded
    if _builtin_loaded:
        return
    from core import variants

    for key in variants.registered_runtime_keys():
        register_outcome_mapping(
            build_outcome_mapping(
                runtime_id=key.runtime_id,
                runtime_contract_version=key.runtime_contract_version,
                reasons=SHARED_TASK_REASONS,
            ),
        )
    _builtin_loaded = True


# ── The per-experiment sorted mapping set ────────────────────────────


def build_outcome_mapping_set(
    members: list[dict[str, str]],
    *,
    contract_version: str = OUTCOME_MAPPING_CONTRACT_VERSION,
) -> dict[str, Any]:
    """Build one sorted, canonical mapping set from its members.

    Members sort by runtime pair and mapping identifier, so reversed
    input builds equal canonical bytes and an equal set digest. A
    duplicate runtime pair or a duplicate mapping identifier rejects,
    because one experiment resolves each pair through exactly one
    member.
    """
    if not members:
        raise OutcomeMappingError("A mapping set needs at least one member")
    normalized = []
    for member in members:
        missing = {
            "runtime_id",
            "runtime_contract_version",
            "mapping_id",
            "mapping_digest",
        } - set(member)
        if missing:
            raise OutcomeMappingError(
                f"A set member misses the fields {sorted(missing)}"
            )
        normalized.append({
            "runtime_id": str(member["runtime_id"]),
            "runtime_contract_version": str(
                member["runtime_contract_version"],
            ),
            "mapping_id": str(member["mapping_id"]),
            "mapping_digest": str(member["mapping_digest"]),
        })
    normalized.sort(
        key=lambda member: (
            member["runtime_id"],
            member["runtime_contract_version"],
            member["mapping_id"],
        ),
    )
    pairs = [
        (member["runtime_id"], member["runtime_contract_version"])
        for member in normalized
    ]
    if len(set(pairs)) != len(pairs):
        raise OutcomeMappingError(
            "A mapping set holds one member per runtime pair"
        )
    mapping_ids = [member["mapping_id"] for member in normalized]
    if len(set(mapping_ids)) != len(mapping_ids):
        raise OutcomeMappingError(
            "A mapping set holds each mapping identifier once"
        )
    payload = {
        "identifier": OUTCOME_MAPPING_SET_IDENTIFIER,
        "contract_version": contract_version,
        "members": normalized,
    }
    return {
        "identifier": OUTCOME_MAPPING_SET_IDENTIFIER,
        "contract_version": contract_version,
        "members": normalized,
        "digest": digest_hex(_SET_DIGEST_DOMAIN, payload),
    }


def mapping_set_for_arms(
    arms: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the experiment mapping set from the admitted arms.

    Admission rejects here when any arm's runtime pair has no
    registered mapping, so a run plan never publishes with a missing
    member.
    """
    from core import variants

    members: dict[tuple[str, str], dict[str, str]] = {}
    for arm in arms:
        runtime_id = str(arm["runtime_id"])
        try:
            key = variants.resolve_runtime_key(runtime_id)
            contract = key.runtime_contract_version
            canonical = key.runtime_id
        except variants.UnknownVariantError:
            canonical = runtime_id
            contract = "1"
        mapping = registered_mapping(canonical, contract)
        members[(canonical, contract)] = mapping.member()
    return build_outcome_mapping_set(list(members.values()))


def member_for_arm(
    mapping_set: dict[str, Any], runtime_id: str,
) -> dict[str, str]:
    """Resolve one arm's exact runtime-pair member from the set."""
    from core import variants

    try:
        key = variants.resolve_runtime_key(str(runtime_id))
        canonical = key.runtime_id
        contract = key.runtime_contract_version
    except variants.UnknownVariantError:
        canonical = str(runtime_id)
        contract = "1"
    for member in mapping_set.get("members") or []:
        if (
            str(member.get("runtime_id")) == canonical
            and str(member.get("runtime_contract_version")) == contract
        ):
            return dict(member)
    raise OutcomeMappingError(
        f"The mapping set has no member for the runtime pair "
        f"{canonical}/{contract}"
    )


def validate_run_outcome_contract(run: dict[str, Any]) -> None:
    """Reject a broken outcome contract before any analysis.

    The check runs only for a run plan that pins a mapping set. It
    validates the set shape, each arm's pinned member against the set
    and against the live registry (a changed mapping is stale), the
    declared exclusion categories against the excludable reasons, and
    every attempt's terminal reason against its arm's mapping.
    """
    plan = run.get("execution_plan") or {}
    mapping_set = plan.get("outcome_mapping_set")
    if not isinstance(mapping_set, dict):
        return
    # Rebuilding from the stored members proves the sorted shape, the
    # uniqueness rules, and the stored digest.
    rebuilt = build_outcome_mapping_set(
        list(mapping_set.get("members") or []),
        contract_version=str(
            mapping_set.get("contract_version")
            or OUTCOME_MAPPING_CONTRACT_VERSION,
        ),
    )
    if rebuilt["digest"] != str(mapping_set.get("digest")):
        raise OutcomeMappingError(
            "The stored mapping-set digest does not match its members"
        )
    arm_runtime: dict[str, str] = {
        str(arm["id"]): str(arm.get("runtime_id"))
        for arm in run.get("arms") or []
    }
    mappings_by_arm: dict[str, OutcomeMapping] = {}
    for plan_arm in plan.get("arms") or []:
        pinned = plan_arm.get("outcome_mapping")
        if not isinstance(pinned, dict):
            raise OutcomeMappingError(
                "Every arm pins one exact mapping-set member"
            )
        member = member_for_arm(
            rebuilt, str(plan_arm.get("runtime_id")),
        )
        if str(pinned.get("mapping_digest")) != member["mapping_digest"]:
            raise OutcomeMappingError(
                "The arm's pinned mapping digest does not match its "
                "set member"
            )
        live = registered_mapping(
            member["runtime_id"], member["runtime_contract_version"],
        )
        if live.digest != member["mapping_digest"]:
            raise OutcomeMappingError(
                "The pinned mapping is stale; a changed mapping needs a "
                "new mapping set and run plan"
            )
        mappings_by_arm[str(plan_arm.get("id"))] = live
    configuration = run.get("test_configuration") or {}
    exclusions = configuration.get("infrastructure_exclusions") or {}
    declared = [str(item) for item in exclusions.get("categories") or []]
    for arm_id, mapping in mappings_by_arm.items():
        for category in declared:
            rules = mapping.resolve(category)
            if rules["denominator"] != "excludable":
                raise OutcomeMappingError(
                    f"The declared exclusion category {category!r} is "
                    "not excludable under the pinned mapping"
                )
        del arm_id
    for attempt in run.get("attempts") or []:
        status = str(attempt.get("status") or "")
        if status in {"queued", "running"}:
            continue
        arm_id = str(attempt.get("arm_id") or "")
        attempt_mapping = mappings_by_arm.get(arm_id)
        if attempt_mapping is None:
            runtime_id = arm_runtime.get(arm_id)
            if runtime_id is None:
                continue
            attempt_mapping = registered_mapping(
                *_pair_for_runtime(runtime_id),
            )
        reason = (
            "completed"
            if status == "completed"
            else str(attempt.get("failure_category") or status)
        )
        attempt_mapping.resolve(reason)


def _pair_for_runtime(runtime_id: str) -> tuple[str, str]:
    from core import variants

    try:
        key = variants.resolve_runtime_key(runtime_id)
    except variants.UnknownVariantError:
        return (runtime_id, "1")
    return (key.runtime_id, key.runtime_contract_version)
