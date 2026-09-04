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


# ── Publication: one test revision, one run plan, one study record ───


async def publish_study(
    study: dict[str, Any],
    *,
    runtime_id: str,
    scorer_versions: list[dict[str, Any]],
    test_name: str | None = None,
    description: str = "",
    max_concurrency: int | None = None,
    timeout_seconds: int = 600,
    starvation_limit: int = 25,
    now: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Persist one authored study as a test revision, a run plan, and a study.

    The test revision carries one arm per study arm with the prepared
    runtime envelope, the run plan freezes the case schedule, the seed
    schedule, the rotated interleave, the repetitions, the limits, and
    the estimand, and the study record links both so admission can
    enforce the study conditions on every attempt of the run.
    """
    import uuid

    from benchmarks import evaluation_records, facade
    from benchmarks.outcome_mappings import mapping_set_for_arms
    from benchmarks.runtime import prepare_benchmark_arm

    invariants = study["invariants"]
    dataset_version_id = str(invariants["dataset_version_id"])
    case_ids = [str(case_id) for case_id in invariants["case_ids"]]
    if not case_ids:
        raise StudyAuthoringError("The study freezes at least one case")
    repetitions = int(invariants["repetitions"])
    prepared_arms = []
    for index, arm in enumerate(study["arms"]):
        envelope = await prepare_benchmark_arm(
            runtime_id, {"submission_overrides": dict(arm["configuration"])},
        )
        prepared_arms.append({
            "id": f"arm-{uuid.uuid4().hex}",
            "name": str(arm["slug"]),
            "slug": str(arm["slug"]),
            "sort_order": index,
            **envelope,
        })
    concurrency = int(
        max_concurrency or study["estimates"].get("max_concurrency") or 4
    )
    test_id = f"test-{uuid.uuid4().hex}"
    revision_id = f"testrev-{uuid.uuid4().hex}"
    revision = await facade.execute(
        "create_test_revision",
        {
            "test_id": test_id,
            "revision_id": revision_id,
            "name": test_name or str(study["name"]),
            "description": description or f"study {study['study_type']}",
            "dataset_version_id": dataset_version_id,
            "configuration": {
                "repetitions": repetitions,
                "seed": int(invariants["seed_schedule"].get("base_seed", 0))
                if isinstance(invariants.get("seed_schedule"), dict)
                else int(study["estimand"]["resampling"]["master_seed"]),
                "max_concurrency": concurrency,
                "study_digest": study["study_digest"],
            },
            "arms": prepared_arms,
            "scorers": [dict(link) for link in scorer_versions],
        },
        generation="legacy",
    )
    mapping_set = mapping_set_for_arms(
        [{"runtime_id": runtime_id} for _ in prepared_arms],
    )
    dataset_record = await evaluation_records.get_record(
        "dataset-version", dataset_version_id,
    )
    trust = {"level": "public_untrusted", "policy_version": "1"}
    dataset_digest = content_checksum({"dataset_version_id": dataset_version_id})
    if dataset_record is not None:
        dataset_digest = str(dataset_record["record"]["content_digest"])
        inputs = dataset_record["record"].get("trust_inputs") or []
        if inputs:
            trust = dict(inputs[0])
    seed_schedule = invariants.get("seed_schedule")
    base_seed = (
        int(seed_schedule.get("base_seed", 0))
        if isinstance(seed_schedule, dict)
        else int(study["estimand"]["resampling"]["master_seed"])
    )
    plan_id = f"plan-{uuid.uuid4().hex}"
    study_id = f"study-{uuid.uuid4().hex}"
    estimand = {
        **study["estimand"],
        "direction": study["estimand"]["comparison_family"]["comparisons"][0]["direction"],
        "study_id": study_id,
    }
    run_plan: dict[str, Any] = {
        "schema_id": "run-plan",
        "schema_version": 2,
        "plan_id": plan_id,
        "digests": {
            "dataset": dataset_digest,
            "runtime": content_checksum([
                arm["configuration_checksum"] for arm in prepared_arms
            ]),
            "scorers": content_checksum(scorer_versions),
            "schema": content_checksum({"contract_generation": 2}),
        },
        "outcome_mapping_set_digest": str(mapping_set["digest"]),
        "arm_mappings": list(mapping_set["members"]),
        "case_ids": case_ids,
        "seed_schedule": {"base_seed": base_seed, "scope": "item-repetition"},
        "arm_order": {"strategy": "rotated_interleave"},
        "repetitions": repetitions,
        "limits": {
            "max_concurrency": concurrency,
            "timeout_seconds": int(timeout_seconds),
            "run_cost": dict(study["estimates"]["cost"]),
        },
        "retry_rules": {
            "infrastructure_exclusions": list(
                study["estimand"]["missingness"]["infrastructure_categories"],
            ),
            "reason": "predeclared infrastructure exclusions",
        },
        "unit_hierarchy": list(frozen_analysis.UNIT_HIERARCHY),
        "estimand": estimand,
        "trust_policy": {"trust": trust, "capabilities": []},
        "resource_estimate": {
            "expected_cost": dict(study["estimates"]["cost"]),
            "pricing_version": str(study["estimates"]["pricing_basis"]),
        },
        "settlement": {"policy": "strict", "maximum_wait_seconds": 3600},
        "dispatch": {"fairness_policy": "weighted_round_robin",
                     "starvation_limit": int(starvation_limit)},
    }
    plan = await facade.execute(
        "create_run_plan",
        {"record": run_plan, "test_revision_id": revision_id, "run_id": None},
    )
    await facade.execute("publish_run_plan", {"record_id": plan["id"]})
    study_record = {
        "schema_id": "study",
        "schema_version": 2,
        "study_id": study_id,
        "study_type": study["study_type"],
        "name": study["name"],
        "arms": [
            {
                "slug": arm["slug"], "treatment": arm["treatment"],
                "configuration_digest": arm["configuration_digest"],
                "configuration": arm["configuration"],
            }
            for arm in study["arms"]
        ],
        "expansion_rule": study["expansion_rule"],
        "invariants": study["invariants"],
        "estimand": study["estimand"],
        "gates": study["gates"],
        "sample_plan": study["sample_plan"],
        "estimates": study["estimates"],
        "treatment_paths": list(study["treatment_paths"]),
        "study_digest": study["study_digest"],
        "run_plan_id": plan_id,
        "test_revision_id": revision_id,
        "authored_at": now,
    }
    saved = await facade.execute(
        "record_study",
        {"record": study_record, "run_plan_id": plan_id,
         "test_revision_id": revision_id},
    )
    return {
        "study_id": saved["id"],
        "study": study_record,
        "test_id": test_id,
        "test_revision_id": revision_id,
        "revision": revision,
        "run_plan_id": plan_id,
        "run_plan": run_plan,
    }


async def enforce_study_admission(run_id: str) -> dict[str, Any] | None:
    """Validate the study conditions of one run before admission.

    A run whose test revision carries a study plan admits only when
    ``validate_study`` passes at the admission stage; a run without a
    study plan admits unchanged. The check reads the stored run plan,
    the dataset version record, and the pinned sources.
    """
    from benchmarks import evaluation_records, repository

    run = await repository.get_run(run_id)
    if run is None:
        return None
    revision_id = str(run.get("test_revision_id") or "")
    if not revision_id:
        return None
    stored_plan = await evaluation_records.run_plan_for_revision(revision_id)
    if stored_plan is None:
        return None
    study = await evaluation_records.study_for_run_plan(str(stored_plan["id"]))
    if study is None:
        return None
    plan = stored_plan["record"]
    dataset_version_id = str(
        study["record"]["invariants"].get("dataset_version_id") or "",
    )
    source: dict[str, Any] | None = None
    dataset_record = await evaluation_records.get_record(
        "dataset-version", dataset_version_id,
    )
    if dataset_record is not None:
        for source_id in dataset_record["record"].get("source_lineage") or []:
            stored_source = await evaluation_records.get_record(
                "benchmark-source", str(source_id),
            )
            if stored_source is not None:
                source = stored_source["record"]
                break
    holdout_hidden = bool(
        (plan.get("trust_policy") or {}).get("holdout_hidden", True),
    )
    verdict = frozen_analysis.validate_study(
        run_plan=plan,
        source=source,
        holdout_hidden=holdout_hidden,
        report=None,
        cost_includes_retries_and_control_plane=bool(
            plan.get("resource_estimate"),
        ),
        stage="admission",
    )
    if not verdict["ready"]:
        raise StudyAdmissionError(
            "The study conditions block admission: "
            + ", ".join(verdict["blocking"])
        )
    return {"study_id": str(study["id"]), "plan_id": str(stored_plan["id"]),
            "checks": verdict["checks"]}


class StudyAdmissionError(ValueError):
    """The run violates its study conditions at admission."""
