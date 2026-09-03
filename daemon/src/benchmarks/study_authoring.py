"""Study authoring: frozen treatments, invariants, estimands, and gates.

A study template expands one declared treatment into an immutable
arm list, keeps every invariant equal across arms, freezes the
estimand and the comparison family through the frozen analysis
engine, records the sample plan, and shows the attempt, cost, and
duration estimates before publication. Supported study types are
one-factor ablation, small parameter grid, preset comparison, runtime
comparison, and model-family comparison, and the generated arm count
stays bounded.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from benchmarks import frozen_analysis
from benchmarks.costs import money_to_json
from benchmarks.provenance import content_checksum

if TYPE_CHECKING:
    from core.money import Money

STUDY_TYPES = (
    "one_factor_ablation",
    "parameter_grid",
    "preset_comparison",
    "runtime_comparison",
    "model_family_comparison",
)
MAX_GENERATED_ARMS = 12
INVARIANT_FIELDS = (
    "dataset_version_id", "case_ids", "seed_schedule", "scorers",
    "arm_order", "repetitions",
)


class StudyAuthoringError(ValueError):
    """The study declaration violates the authoring contract."""


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def expand_arms(
    study_type: str,
    *,
    base_configuration: dict[str, Any],
    treatment: dict[str, Any],
) -> list[dict[str, Any]]:
    """Generate the immutable arm list from one expansion rule."""
    if study_type not in STUDY_TYPES:
        raise StudyAuthoringError(f"Unknown study type: {study_type!r}")
    arms: list[dict[str, Any]] = []
    if study_type == "parameter_grid":
        factors = treatment.get("factors") or {}
        if not factors:
            raise StudyAuthoringError("A parameter grid names its factors")
        names = sorted(factors)
        for values in itertools.product(*(factors[name] for name in names)):
            configuration = _copy(base_configuration)
            for name, value in zip(names, values, strict=True):
                _set_path(configuration, name, value)
            arms.append({
                "slug": "grid-" + "-".join(
                    f"{name.split('.')[-1]}-{value}"
                    for name, value in zip(names, values, strict=True)
                ),
                "treatment": dict(zip(names, values, strict=True)),
                "configuration": configuration,
            })
    else:
        path = str(treatment.get("path") or "")
        treatment_values = list(treatment.get("values") or [])
        if not path or not treatment_values:
            raise StudyAuthoringError(
                "The treatment names one path and its values"
            )
        for value in treatment_values:
            configuration = _copy(base_configuration)
            _set_path(configuration, path, value)
            arms.append({
                "slug": f"{path.split('.')[-1]}-{value}",
                "treatment": {path: value},
                "configuration": configuration,
            })
    if len(arms) > MAX_GENERATED_ARMS:
        raise StudyAuthoringError(
            f"The study generates {len(arms)} arms; the limit is "
            f"{MAX_GENERATED_ARMS}"
        )
    slugs = [arm["slug"] for arm in arms]
    if len(set(slugs)) != len(slugs):
        raise StudyAuthoringError("Generated arm slugs must be unique")
    return arms


def _copy(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value))


def author_study(
    *,
    study_type: str,
    name: str,
    base_configuration: dict[str, Any],
    treatment: dict[str, Any],
    invariants: dict[str, Any],
    families: dict[str, list[str]],
    scorer_id: str,
    master_seed: int,
    comparison_margin: float,
    per_attempt_cost: Money,
    seconds_per_attempt: int,
    max_concurrency: int = 4,
    hypothesis: str = "non_inferiority",
    family_weights: dict[str, float] | None = None,
    resample_count: int = 999,
) -> dict[str, Any]:
    """Author one frozen study with estimates shown before publication."""
    for field_name in INVARIANT_FIELDS:
        if field_name not in invariants:
            raise StudyAuthoringError(
                f"The study freezes the invariant {field_name}"
            )
    arms = expand_arms(
        study_type, base_configuration=base_configuration, treatment=treatment,
    )
    baseline = arms[0]["slug"]
    comparisons = [
        {
            "comparison_id": f"{baseline}-vs-{arm['slug']}",
            "baseline_arm": baseline,
            "candidate_arm": arm["slug"],
            "hypothesis": hypothesis,
            "non_inferiority_margin": (
                comparison_margin if hypothesis == "non_inferiority"
                else None
            ),
        }
        for arm in arms[1:]
    ]
    specification = frozen_analysis.freeze_specification(
        families=families,
        scorer_id=scorer_id,
        master_seed=master_seed,
        comparison_family={"family_id": f"{name}-primary",
                           "comparisons": comparisons},
        family_weights=family_weights,
        resample_count=resample_count,
    )
    case_count = sum(len(ids) for ids in families.values())
    repetitions = int(invariants["repetitions"])
    attempts = case_count * repetitions * len(arms)
    cost = per_attempt_cost.scale_ratio(attempts, 1)
    duration_seconds = (
        -(-attempts // max(int(max_concurrency), 1)) * int(seconds_per_attempt)
    )
    study = {
        "study_type": study_type,
        "name": name,
        "arms": [
            {"slug": arm["slug"], "treatment": arm["treatment"],
             "configuration_digest": content_checksum(arm["configuration"]),
             "configuration": arm["configuration"]}
            for arm in arms
        ],
        "expansion_rule": {"study_type": study_type, "treatment": treatment},
        "invariants": {
            field_name: invariants[field_name] for field_name in INVARIANT_FIELDS
        },
        "estimand": specification,
        "gates": {
            "comparison_family": specification["comparison_family"],
            "predeclared": True,
        },
        "sample_plan": {
            "cases": case_count,
            "repetitions": repetitions,
            "arms": len(arms),
            "attempts": attempts,
            "families": {family: len(ids) for family, ids in sorted(
                families.items(),
            )},
        },
        "estimates": {
            "attempts": attempts,
            "cost": money_to_json(cost),
            "pricing_basis": "per_attempt_reservation",
            "duration_seconds": duration_seconds,
            "max_concurrency": int(max_concurrency),
        },
        "treatment_paths": sorted({
            path for arm in arms for path in arm["treatment"]
        }),
    }
    _assert_invariants_hold(study)
    return {**study, "study_digest": content_checksum(study)}


def _assert_invariants_hold(study: dict[str, Any]) -> None:
    """Every arm differs only inside the declared treatment paths."""
    arms = study["arms"]
    treatment_paths = set(study["treatment_paths"])
    reference = _flatten(arms[0]["configuration"])
    for arm in arms[1:]:
        flattened = _flatten(arm["configuration"])
        for key in set(reference) | set(flattened):
            if reference.get(key) != flattened.get(key) and (
                key not in treatment_paths
            ):
                raise StudyAuthoringError(
                    f"The arm {arm['slug']} changes the invariant {key}"
                )


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        flattened.update(_flatten(item, path))
    return flattened
