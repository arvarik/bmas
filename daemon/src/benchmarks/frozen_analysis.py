"""Frozen analysis: specification first, deterministic computation second.

Every analysis freezes its complete specification before any number
exists: the target population, the primary estimand, the declared
task-family and case weights normalized separately, the repetition
reduction with its tie rules, the missingness rules and limits, the
cluster and resampling order, the master seed with the portable
``bmas-analysis-rng`` algorithm version, and the predeclared
comparison family with its margins, directions, minimum sample sizes,
and multiplicity method. The frozen input carries every planned slot
with its observed value or its missingness class. Slot reduction
happens before weighting, family and case weights apply exactly once
in aggregation and never as draw probabilities, and the
family-stratified weighted case bootstrap derives every draw from the
master seed, the input digest, the replicate index, the family
digest, and the draw index. The stored snapshot pins the engine,
build, dependency lock, runtime, and toolchain digests with the
canonical input and output checksums, and its replay claim never
describes external execution as reproducible.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import platform
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks import analysis_engine, analysis_rng
from benchmarks.analysis import outcome_slots
from benchmarks.evaluation_contracts import validate_record
from benchmarks.provenance import content_checksum

ENGINE_NAME = "bmas-frozen-analysis"
ENGINE_VERSION = "1"
PRIMARY_ESTIMAND = "family-balanced-unconditional-task-success"
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_RESAMPLE_COUNT = 999
DEFAULT_MIN_FAMILY_CASES = 5
DEFAULT_MAX_MISSING_TOTAL_WEIGHT = 0.05
DEFAULT_MAX_MISSING_FAMILY_WEIGHT = 0.20
EXACT_ENUMERATION_LIMIT = 12
BINARY_REDUCTIONS = ("strict_majority", "all", "at_least_k")
DIRECTIONS = ("higher_is_better", "lower_is_better")
HYPOTHESES = ("non_inferiority", "superiority")
UNIT_HIERARCHY = ["family", "case", "repetition"]


class FrozenAnalysisError(ValueError):
    """The specification or input violates the frozen analysis contract."""


# ── Specification freeze ─────────────────────────────────────────────


def _normalize(weights: dict[str, float], *, what: str) -> dict[str, float]:
    for name, value in weights.items():
        if value < 0:
            raise FrozenAnalysisError(
                f"The {what} weight for {name} is negative"
            )
    total = sum(weights.values())
    if total <= 0:
        raise FrozenAnalysisError(
            f"The included {what} weight vector sums to zero"
        )
    return {name: value / total for name, value in sorted(weights.items())}


def freeze_specification(
    *,
    families: dict[str, list[str]],
    scorer_id: str,
    master_seed: int,
    comparison_family: dict[str, Any],
    family_weights: dict[str, float] | None = None,
    case_weights: dict[str, float] | None = None,
    binary_reduction: str = "strict_majority",
    at_least_k: int | None = None,
    infrastructure_categories: list[str] | None = None,
    zero_success_categories: list[str] | None = None,
    max_missing_total_weight: float = DEFAULT_MAX_MISSING_TOTAL_WEIGHT,
    max_missing_family_weight: float = DEFAULT_MAX_MISSING_FAMILY_WEIGHT,
    min_family_cases: int = DEFAULT_MIN_FAMILY_CASES,
    resample_count: int = DEFAULT_RESAMPLE_COUNT,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    target_population: str = "declared dataset cases",
    filters: dict[str, Any] | None = None,
    algorithm_version: int = analysis_rng.RNG_ALGORITHM_VERSION,
    metric_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze the complete specification before any calculation.

    Family weights normalize over the included families, case weights
    normalize inside each family, and both reject a zero-sum vector
    before admission. Every comparison predeclares its metric, arms,
    direction, hypothesis, margin, and minimum usable case count.
    """
    if not families:
        raise FrozenAnalysisError("An analysis includes at least one family")
    declared_family = {
        family: float((family_weights or {}).get(family, 1.0))
        for family in families
    }
    normalized_family = _normalize(declared_family, what="family")
    normalized_cases: dict[str, dict[str, float]] = {}
    for family, case_ids in sorted(families.items()):
        if not case_ids:
            raise FrozenAnalysisError(
                f"The included family {family} holds no cases"
            )
        declared = {
            case_id: float((case_weights or {}).get(case_id, 1.0))
            for case_id in case_ids
        }
        try:
            normalized_cases[family] = _normalize(declared, what="case")
        except FrozenAnalysisError as error:
            raise FrozenAnalysisError(
                f"The included family {family}: {error}"
            ) from error
    if binary_reduction not in BINARY_REDUCTIONS:
        raise FrozenAnalysisError(
            f"Unknown binary reduction: {binary_reduction!r}"
        )
    if binary_reduction == "at_least_k" and (
        not isinstance(at_least_k, int) or at_least_k < 1
    ):
        raise FrozenAnalysisError("at_least_k needs one positive threshold")
    if not 0 <= master_seed <= 2**64 - 1:
        raise FrozenAnalysisError(
            "The master seed fits one unsigned 64-bit integer"
        )
    comparisons = []
    family_id = str(comparison_family.get("family_id") or "")
    if not family_id:
        raise FrozenAnalysisError("A comparison family names itself")
    for entry in comparison_family.get("comparisons") or []:
        hypothesis = str(entry.get("hypothesis") or "non_inferiority")
        direction = str(entry.get("direction") or "higher_is_better")
        if hypothesis not in HYPOTHESES:
            raise FrozenAnalysisError(f"Unknown hypothesis: {hypothesis!r}")
        if direction not in DIRECTIONS:
            raise FrozenAnalysisError(f"Unknown direction: {direction!r}")
        margin = entry.get("non_inferiority_margin")
        if hypothesis == "non_inferiority" and (
            not isinstance(margin, (int, float)) or margin < 0
        ):
            raise FrozenAnalysisError(
                "A non-inferiority comparison predeclares a nonnegative "
                "margin"
            )
        comparisons.append({
            "comparison_id": str(entry["comparison_id"]),
            "metric": str(entry.get("metric") or "unconditional_success"),
            "baseline_arm": str(entry["baseline_arm"]),
            "candidate_arm": str(entry["candidate_arm"]),
            "direction": direction,
            "hypothesis": hypothesis,
            "non_inferiority_margin": (
                float(margin) if margin is not None else None
            ),
            "minimum_usable_cases": int(
                entry.get("minimum_usable_cases") or min_family_cases,
            ),
        })
    if not comparisons:
        raise FrozenAnalysisError(
            "A comparison family declares at least one comparison"
        )
    specification = {
        "specification": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "target_population": target_population,
        "primary_estimand": PRIMARY_ESTIMAND,
        "scorer_id": scorer_id,
        "unit_hierarchy": list(UNIT_HIERARCHY),
        "families": sorted(families),
        "cluster_order": sorted(families),
        "case_ids": {
            family: sorted(case_ids) for family, case_ids in families.items()
        },
        "family_weights_declared": declared_family,
        "family_weights": normalized_family,
        "case_weights": normalized_cases,
        "reduction": {
            "fractional": "mean_of_nonmissing_slots",
            "binary": binary_reduction,
            "at_least_k": at_least_k,
            "tie_rule": "strict_majority_ties_fail",
            "order": "slots_before_weights",
        },
        "missingness": {
            "zero_success_categories": sorted(
                zero_success_categories
                or ["model", "runtime", "tool", "timeout", "policy",
                    "budget", "deadline", "cancellation"],
            ),
            "infrastructure_categories": sorted(
                infrastructure_categories or ["infrastructure"],
            ),
            "paired_slot_removal": True,
            "arm_mean_imputation": "never",
            "max_missing_total_weight": float(max_missing_total_weight),
            "max_missing_family_weight": float(max_missing_family_weight),
        },
        "small_cluster": {
            "min_family_cases": int(min_family_cases),
            "policy": "insufficient_bootstrap_then_exact_or_descriptive",
        },
        "resampling": {
            "algorithm": analysis_rng.RNG_ALGORITHM,
            "algorithm_version": int(algorithm_version),
            "implementation": analysis_rng.implementation_for(
                int(algorithm_version),
            ),
            "master_seed": int(master_seed),
            "resample_count": int(resample_count),
            "unit": "case",
            "draw": "uniform_with_replacement_within_family",
            "weighting": "each_case_weight_once_in_aggregation",
            "exact_enumeration_limit": EXACT_ENUMERATION_LIMIT,
        },
        "comparison_family": {
            "family_id": family_id,
            "alpha": 1.0 - float(confidence_level),
            "multiplicity": "holm",
            "comparisons": comparisons,
        },
        "confidence_level": float(confidence_level),
        "filters": dict(filters or {}),
        # Every displayed metric of the report resolves to one of
        # these published metric definitions.
        "metric_ids": sorted({str(metric_id) for metric_id in metric_ids or []}),
    }
    return {
        **specification,
        "specification_digest": content_checksum(specification),
    }


# ── Frozen input ─────────────────────────────────────────────────────


def freeze_input(
    run: dict[str, Any],
    specification: dict[str, Any],
    *,
    planned_repetitions: int,
) -> dict[str, Any]:
    """Freeze every planned slot with its value or missingness class.

    A slot seals with the first valid substantive outcome in attempt
    order. A substantive failure counts as zero success. A slot whose
    every attempt was a predeclared infrastructure failure stays
    missing, and a planned slot with no attempt stays missing too.
    """
    scorer_id = str(specification["scorer_id"])
    infrastructure = set(
        specification["missingness"]["infrastructure_categories"],
    )
    scores_by_attempt: dict[str, dict[str, Any]] = {}
    for score in run.get("scores") or []:
        if str(score.get("scorer_id")) != scorer_id:
            continue
        if score.get("status") == "scored" and score.get("score") is not None:
            scores_by_attempt[str(score["attempt_id"])] = score
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in run.get("attempts") or []:
        by_arm[str(attempt["arm_id"])].append(attempt)
    case_family = {
        case_id: family
        for family, case_ids in specification["case_ids"].items()
        for case_id in case_ids
    }
    slots: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    counts: dict[str, dict[str, int]] = {}
    for arm_id in sorted(by_arm):
        sealed = outcome_slots(by_arm[arm_id], infrastructure)
        arm_slots: dict[str, dict[str, dict[str, Any]]] = {}
        count = {"planned": 0, "admitted": 0, "failed": 0, "retried": 0,
                 "missing": 0, "excluded": 0, "observed": 0}
        for case_id in sorted(case_family):
            case_slots: dict[str, dict[str, Any]] = {}
            for repeat in range(1, planned_repetitions + 1):
                count["planned"] += 1
                slot = sealed.get((case_id, repeat))
                record: dict[str, Any]
                if slot is None:
                    record = {"value": None, "passed": None,
                              "state": "unplanned_missing",
                              "retry_attempts": 0}
                    count["missing"] += 1
                else:
                    count["admitted"] += 1
                    count["retried"] += int(slot["retry_attempts"])
                    outcome = slot["sealed"]
                    if outcome is None:
                        record = {"value": None, "passed": None,
                                  "state": "infrastructure_missing",
                                  "retry_attempts": slot["retry_attempts"]}
                        count["excluded"] += 1
                        count["missing"] += 1
                    elif str(outcome.get("status")) == "completed":
                        score = scores_by_attempt.get(str(outcome["id"]))
                        if score is None:
                            record = {"value": None, "passed": None,
                                      "state": "unscored_missing",
                                      "retry_attempts":
                                          slot["retry_attempts"]}
                            count["missing"] += 1
                        else:
                            record = {
                                "value": float(score["score"]),
                                "passed": bool(score.get("passed")),
                                "state": "observed",
                                "retry_attempts": slot["retry_attempts"],
                            }
                            count["observed"] += 1
                    else:
                        record = {"value": 0.0, "passed": False,
                                  "state": "failed_zero",
                                  "retry_attempts": slot["retry_attempts"]}
                        count["failed"] += 1
                        count["observed"] += 1
                case_slots[str(repeat)] = record
            arm_slots[case_id] = case_slots
        slots[arm_id] = arm_slots
        counts[arm_id] = count
    frozen = {
        "run_id": str(run.get("id") or ""),
        "specification_digest": specification["specification_digest"],
        "arms": sorted(by_arm),
        "planned_repetitions": int(planned_repetitions),
        "case_families": case_family,
        "slots": slots,
        "counts": counts,
    }
    return {**frozen, "input_digest": content_checksum(frozen)}


# ── Pairing, reduction, weights, and missingness ─────────────────────


def _binary(passes: list[bool], reduction: dict[str, Any]) -> bool:
    if reduction["binary"] == "all":
        return all(passes)
    if reduction["binary"] == "at_least_k":
        return sum(passes) >= int(reduction["at_least_k"])
    return sum(passes) * 2 > len(passes)


def pair_cases(
    specification: dict[str, Any],
    frozen_input: dict[str, Any],
    *,
    baseline_arm: str,
    candidate_arm: str,
) -> dict[str, Any]:
    """Pair slots, reduce cases, renormalize weights, apply limits.

    A slot leaves both arms when either arm is missing. A case with no
    remaining slot is missing. Case weights renormalize inside each
    family over usable cases, the removed weight reports per family
    and arm, and the primary estimate invalidates when a limit fails.
    """
    slots = frozen_input["slots"]
    if baseline_arm not in slots or candidate_arm not in slots:
        raise FrozenAnalysisError(
            "Both compared arms must exist in the frozen input"
        )
    reduction = specification["reduction"]
    missingness = specification["missingness"]
    families: dict[str, dict[str, Any]] = {}
    for family in specification["cluster_order"]:
        usable: dict[str, dict[str, Any]] = {}
        missing_cases: list[str] = []
        removed_slots = 0
        for case_id in specification["case_ids"][family]:
            baseline_slots = slots[baseline_arm].get(case_id, {})
            candidate_slots = slots[candidate_arm].get(case_id, {})
            kept: list[dict[str, Any]] = []
            for repeat in sorted(baseline_slots, key=int):
                left = baseline_slots[repeat]
                right = candidate_slots.get(repeat) or {"value": None}
                if left["value"] is None or right["value"] is None:
                    removed_slots += 1
                    continue
                kept.append({
                    "repeat": int(repeat),
                    "baseline": float(left["value"]),
                    "candidate": float(right["value"]),
                    "baseline_passed": bool(left["passed"]),
                    "candidate_passed": bool(right["passed"]),
                })
            if not kept:
                missing_cases.append(case_id)
                continue
            usable[case_id] = {
                "slots": kept,
                "baseline_fractional": sum(s["baseline"] for s in kept)
                / len(kept),
                "candidate_fractional": sum(s["candidate"] for s in kept)
                / len(kept),
                "baseline_binary": _binary(
                    [s["baseline_passed"] for s in kept], reduction,
                ),
                "candidate_binary": _binary(
                    [s["candidate_passed"] for s in kept], reduction,
                ),
            }
            usable[case_id]["delta"] = (
                usable[case_id]["candidate_fractional"]
                - usable[case_id]["baseline_fractional"]
            )
        declared = specification["case_weights"][family]
        missing_weight = sum(declared[case_id] for case_id in missing_cases)
        usable_total = sum(declared[case_id] for case_id in usable)
        renormalized = {
            case_id: declared[case_id] / usable_total
            for case_id in sorted(usable)
        } if usable_total > 0 else {}
        families[family] = {
            "usable": usable,
            "usable_case_ids": sorted(usable),
            "missing_case_ids": missing_cases,
            "removed_slots": removed_slots,
            "removed_weight": missing_weight,
            "renormalized_weights": renormalized,
            "family_weight": specification["family_weights"][family],
        }
    total_missing = sum(
        entry["family_weight"] * entry["removed_weight"]
        for entry in families.values()
    )
    limit_failures = []
    if total_missing > missingness["max_missing_total_weight"] + 1e-12:
        limit_failures.append("max_missing_total_weight")
    for family, entry in families.items():
        if entry["removed_weight"] > (
            missingness["max_missing_family_weight"] + 1e-12
        ):
            limit_failures.append(f"max_missing_family_weight:{family}")
    return {
        "baseline_arm": baseline_arm,
        "candidate_arm": candidate_arm,
        "families": families,
        "paired_cases": sum(
            len(entry["usable"]) for entry in families.values()
        ),
        "missing_cases": sum(
            len(entry["missing_case_ids"]) for entry in families.values()
        ),
        "removed_slots": sum(
            entry["removed_slots"] for entry in families.values()
        ),
        "total_missing_weight": total_missing,
        "limit_failures": limit_failures,
        "primary_valid": not limit_failures,
    }


def weighted_estimate(
    paired: dict[str, Any], *, flips: dict[str, bool] | None = None,
    drawn: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Aggregate with each family and case weight applied exactly once."""
    aggregates: dict[str, float] = {}
    numerator = 0.0
    denominator = 0.0
    for family, entry in paired["families"].items():
        members = drawn[family] if drawn is not None else (
            entry["usable_case_ids"]
        )
        if not members:
            continue
        weights = entry["renormalized_weights"]
        weight_sum = analysis_engine.sequential_sum(
            weights[case_id] for case_id in members
        )
        if weight_sum <= 0:
            continue
        total = 0.0
        for case_id in members:
            delta = entry["usable"][case_id]["delta"]
            if flips and flips.get(case_id):
                delta = -delta
            total += weights[case_id] * delta
        aggregates[family] = total / weight_sum
        numerator += entry["family_weight"] * aggregates[family]
        denominator += entry["family_weight"]
    return {
        "estimate": numerator / denominator if denominator > 0 else None,
        "family_aggregates": aggregates,
    }


# ── The weighted cluster bootstrap and the sign-flip test ────────────


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return ordered[min(position, len(ordered) - 1)]


def resolve_engine(
    specification: dict[str, Any],
    *,
    requested: str | None = None,
    record_draws: bool = False,
) -> str:
    """Select the engine that computes one frozen specification.

    The vectorized engine serves algorithm version 2 whenever the
    host resolves it and the caller records no draws; the reference
    engine serves every other request. An explicit request that the
    host cannot honour fails instead of silently changing engines.
    """
    version = int(specification["resampling"]["algorithm_version"])
    if requested in (None, "auto"):
        if (
            version >= 2 and not record_draws
            and analysis_engine.available()
        ):
            return analysis_engine.ENGINE_VECTORIZED
        return analysis_engine.ENGINE_REFERENCE
    if requested not in analysis_engine.ENGINES:
        raise FrozenAnalysisError(f"Unknown analysis engine: {requested!r}")
    if requested == analysis_engine.ENGINE_VECTORIZED:
        if version < 2:
            raise FrozenAnalysisError(
                "Algorithm version 1 derives every draw through SHA-256 "
                "and never vectorizes; freeze algorithm version 2"
            )
        if not analysis_engine.available():
            raise FrozenAnalysisError(
                "The vectorized engine is unavailable on this host"
            )
    return requested


def bootstrap(
    specification: dict[str, Any],
    paired: dict[str, Any],
    input_digest: str,
    *,
    record_draws: bool = False,
    engine: str | None = None,
) -> dict[str, Any]:
    """Run the one weighted cluster bootstrap with derived draws."""
    resampling = specification["resampling"]
    digest = bytes.fromhex(input_digest)
    replicates: list[float] = []
    records: list[dict[str, Any]] = []
    resolved = resolve_engine(
        specification, requested=engine, record_draws=record_draws,
    )
    if resolved == analysis_engine.ENGINE_VECTORIZED:
        replicates = [
            value
            for value in analysis_engine.bootstrap_estimates(
                specification, paired, digest,
            )
            if value is not None
        ]
        return _bootstrap_interval(specification, replicates)
    for replicate_index in range(int(resampling["resample_count"])):
        drawn: dict[str, list[str]] = {}
        for family in specification["cluster_order"]:
            usable = paired["families"][family]["usable_case_ids"]
            if not usable:
                drawn[family] = []
                continue
            indexes = analysis_rng.replicate_draws(
                master_seed=int(resampling["master_seed"]),
                input_digest=digest,
                replicate_index=replicate_index,
                family_id=family,
                case_count=len(usable),
                algorithm_version=int(resampling["algorithm_version"]),
            )
            drawn[family] = [usable[index] for index in indexes]
        estimate = weighted_estimate(paired, drawn=drawn)
        if estimate["estimate"] is not None:
            replicates.append(estimate["estimate"])
        if record_draws:
            records.append({
                "replicate_index": replicate_index,
                "draws": drawn,
                "family_aggregates": estimate["family_aggregates"],
                "combined": estimate["estimate"],
            })
    result = _bootstrap_interval(specification, replicates)
    if record_draws:
        result["replicates"] = records
    return result


def _bootstrap_interval(
    specification: dict[str, Any], replicates: list[float],
) -> dict[str, Any]:
    """Summarize the replicate estimates as the percentile interval."""
    resampling = specification["resampling"]
    alpha = specification["comparison_family"]["alpha"]
    if not replicates:
        interval: dict[str, Any] = {"status": "no_data", "low": None,
                                    "high": None}
    elif len({format(v, ".17g") for v in replicates}) <= 1:
        interval = {"status": "degenerate", "low": min(replicates),
                    "high": max(replicates)}
    else:
        interval = {
            "status": "estimated",
            "low": _percentile(replicates, alpha / 2),
            "high": _percentile(replicates, 1 - alpha / 2),
        }
    return {
        **interval,
        "method": "family_stratified_weighted_case_bootstrap_percentile",
        "unit": "case",
        "replicate_count": len(replicates),
        "algorithm": resampling["algorithm"],
        "algorithm_version": resampling["algorithm_version"],
    }


def sign_flip_test(
    specification: dict[str, Any],
    paired: dict[str, Any],
    input_digest: str,
    *,
    engine: str | None = None,
) -> dict[str, Any]:
    """Paired sign-flip randomization on the weighted statistic."""
    observed = weighted_estimate(paired)["estimate"]
    case_ids = [
        case_id
        for family in specification["cluster_order"]
        for case_id in paired["families"][family]["usable_case_ids"]
    ]
    if observed is None or not case_ids:
        return {"method": "paired_sign_flip", "p_value": None,
                "mode": None, "resamples": 0}
    target = abs(observed) - 1e-12
    count = len(case_ids)
    if count <= EXACT_ENUMERATION_LIMIT:
        at_least = 0
        for mask in range(2**count):
            flips = {
                case_id: bool(mask >> bit & 1)
                for bit, case_id in enumerate(case_ids)
            }
            value = weighted_estimate(paired, flips=flips)["estimate"]
            if value is not None and abs(value) >= target:
                at_least += 1
        return {"method": "paired_sign_flip", "mode": "exact_enumeration",
                "p_value": at_least / 2**count, "resamples": 2**count}
    resampling = specification["resampling"]
    digest = bytes.fromhex(input_digest)
    resamples = int(resampling["resample_count"])
    resolved = resolve_engine(specification, requested=engine)
    if resolved == analysis_engine.ENGINE_VECTORIZED:
        at_least = analysis_engine.sign_flip_exceedances(
            specification, paired, digest, target=target,
        )
        return {"method": "paired_sign_flip", "mode": "monte_carlo",
                "p_value": (1 + at_least) / (resamples + 1),
                "resamples": resamples}
    at_least = 0
    for replicate_index in range(resamples):
        bits = analysis_rng.replicate_sign_flips(
            master_seed=int(resampling["master_seed"]),
            input_digest=digest,
            replicate_index=replicate_index,
            case_count=count,
            algorithm_version=int(resampling["algorithm_version"]),
        )
        flips = dict(zip(case_ids, bits, strict=True))
        value = weighted_estimate(paired, flips=flips)["estimate"]
        if value is not None and abs(value) >= target:
            at_least += 1
    return {"method": "paired_sign_flip", "mode": "monte_carlo",
            "p_value": (1 + at_least) / (resamples + 1),
            "resamples": resamples}


def holm_adjust(p_values: list[float | None]) -> list[float | None]:
    """Holm step-down adjustment inside one declared family."""
    indexed = [(p, i) for i, p in enumerate(p_values) if p is not None]
    adjusted: list[float | None] = [None] * len(p_values)
    count = len(indexed)
    running = 0.0
    for rank, (p_value, index) in enumerate(sorted(indexed)):
        running = max(running, min(1.0, (count - rank) * p_value))
        adjusted[index] = running
    return adjusted


# ── Comparisons and gates ────────────────────────────────────────────


def compare(
    specification: dict[str, Any],
    frozen_input: dict[str, Any],
    comparison: dict[str, Any],
    *,
    record_draws: bool = False,
    engine: str | None = None,
) -> dict[str, Any]:
    """Compute one predeclared comparison under the frozen rules."""
    paired = pair_cases(
        specification, frozen_input,
        baseline_arm=comparison["baseline_arm"],
        candidate_arm=comparison["candidate_arm"],
    )
    point = weighted_estimate(paired)
    minimum = specification["small_cluster"]["min_family_cases"]
    small_families = sorted(
        family for family, entry in paired["families"].items()
        if len(entry["usable_case_ids"]) < minimum
    )
    total_cases = paired["paired_cases"]
    if small_families:
        interval: dict[str, Any] = {
            "status": "insufficient",
            "low": None, "high": None,
            "method": "family_stratified_weighted_case_bootstrap_percentile",
            "unit": "case",
            "reason": "a weighted family has fewer usable cases than the "
                      "small-cluster minimum",
        }
    else:
        interval = bootstrap(
            specification, paired, frozen_input["input_digest"],
            record_draws=record_draws, engine=engine,
        )
    test = sign_flip_test(
        specification, paired, frozen_input["input_digest"], engine=engine,
    )
    comparative_claim = (
        interval["status"] in ("estimated", "degenerate")
        or (small_families and total_cases <= EXACT_ENUMERATION_LIMIT
            and test["mode"] == "exact_enumeration")
    )
    return {
        "comparison_id": comparison["comparison_id"],
        "metric": comparison["metric"],
        "baseline_arm": comparison["baseline_arm"],
        "candidate_arm": comparison["candidate_arm"],
        "direction": comparison["direction"],
        "hypothesis": comparison["hypothesis"],
        "non_inferiority_margin": comparison["non_inferiority_margin"],
        "minimum_usable_cases": comparison["minimum_usable_cases"],
        "estimate": point["estimate"],
        "family_aggregates": point["family_aggregates"],
        "interval": interval,
        "test": test,
        "p_value_adjusted": None,
        "counts": {
            "paired_cases": total_cases,
            "missing_cases": paired["missing_cases"],
            "removed_slots": paired["removed_slots"],
        },
        "weights": {
            family: {
                "family_weight": entry["family_weight"],
                "renormalized_case_weights": entry["renormalized_weights"],
                "removed_weight": entry["removed_weight"],
                "missing_case_ids": entry["missing_case_ids"],
            }
            for family, entry in paired["families"].items()
        },
        "total_missing_weight": paired["total_missing_weight"],
        "limit_failures": paired["limit_failures"],
        "primary_valid": paired["primary_valid"],
        "small_families": small_families,
        "comparative_claim": bool(comparative_claim),
        "statistical_unit": "case",
    }


def gate_decision(
    comparison: dict[str, Any], *, alpha: float,
) -> dict[str, Any]:
    """Decide one predeclared non-inferiority or superiority gate.

    The gate reads only frozen values: the margin, the direction, the
    minimum usable case count, and the missingness limits. Any
    violated precondition yields indeterminate, never a pass.
    """
    reasons: list[str] = []
    if not comparison["primary_valid"]:
        reasons.append("missingness_limit_exceeded")
    if comparison["counts"]["paired_cases"] < comparison["minimum_usable_cases"]:
        reasons.append("below_predeclared_sample_size")
    if comparison["small_families"]:
        reasons.append("insufficient_family_cluster")
    interval = comparison["interval"]
    if interval["status"] not in ("estimated", "degenerate"):
        reasons.append("no_comparative_interval")
    if reasons:
        return {"status": "indeterminate", "reasons": reasons}
    higher = comparison["direction"] == "higher_is_better"
    low, high = interval["low"], interval["high"]
    if comparison["hypothesis"] == "non_inferiority":
        margin = comparison["non_inferiority_margin"]
        passed = (low > -margin) if higher else (high < margin)
        bound = low if higher else high
        return {
            "status": "passed" if passed else "failed",
            "reasons": [],
            "bound": bound,
            "margin": margin,
            "rule": ("lower_bound_above_negative_margin" if higher
                     else "upper_bound_below_margin"),
        }
    adjusted = comparison.get("p_value_adjusted")
    significant = adjusted is not None and adjusted < alpha
    passed = significant and ((low > 0) if higher else (high < 0))
    return {
        "status": "passed" if passed else "failed",
        "reasons": [],
        "p_value_adjusted": adjusted,
        "rule": "holm_adjusted_significance_and_interval_excludes_zero",
    }


# ── The complete frozen report ───────────────────────────────────────


def compute_report(
    specification: dict[str, Any],
    frozen_input: dict[str, Any],
    *,
    record_draws: bool = False,
    ledger_summary: dict[str, Any] | None = None,
    latency_ms_by_arm: dict[str, list[int]] | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Compute every predeclared comparison plus resource analytics.

    The engine choice never changes a number: the vectorized engine
    and the reference engine produce equal replicate estimates,
    intervals, tests, and digests for one frozen specification.
    """
    family = specification["comparison_family"]
    comparisons = [
        compare(
            specification, frozen_input, entry,
            record_draws=record_draws, engine=engine,
        )
        for entry in family["comparisons"]
    ]
    adjusted = holm_adjust([c["test"]["p_value"] for c in comparisons])
    for comparison, value in zip(comparisons, adjusted, strict=True):
        comparison["p_value_adjusted"] = value
        comparison["multiplicity_family"] = family["family_id"]
        comparison["gate"] = gate_decision(comparison, alpha=family["alpha"])
    arms = {}
    for arm_id in frozen_input["arms"]:
        counts = frozen_input["counts"][arm_id]
        successes = sum(
            1
            for case_slots in frozen_input["slots"][arm_id].values()
            for slot in case_slots.values()
            if slot["state"] in ("observed", "failed_zero") and slot["passed"]
        )
        denominator = counts["planned"] - counts["excluded"]
        arms[arm_id] = {
            "counts": counts,
            "unconditional_denominator": denominator,
            "unconditional_successes": successes,
            "unconditional_success_rate": (
                successes / denominator if denominator else None
            ),
            "denominator_statement": (
                "planned slots minus predeclared infrastructure exclusions"
            ),
            "latency_ms": _latency_summary(
                (latency_ms_by_arm or {}).get(arm_id) or [],
            ),
        }
    report = {
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "specification_digest": specification["specification_digest"],
        "input_digest": frozen_input["input_digest"],
        "primary_estimand": specification["primary_estimand"],
        "statistical_unit": "case",
        "metric_ids": list(specification.get("metric_ids") or []),
        "arms": arms,
        "comparisons": comparisons,
        "resources": _resource_analytics(ledger_summary, arms),
        "warnings": sorted({
            failure
            for comparison in comparisons
            for failure in comparison["limit_failures"]
        }),
    }
    return {**report, "results_digest": content_checksum(report)}


def _latency_summary(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "median_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median_ms": _percentile([float(v) for v in ordered], 0.5),
        "p95_ms": _percentile([float(v) for v in ordered], 0.95),
        "estimator": "nearest_rank_percentile",
    }


def _resource_analytics(
    ledger_summary: dict[str, Any] | None, arms: dict[str, Any],
) -> dict[str, Any]:
    """Cost per success from Money totals and unconditional successes."""
    from benchmarks.costs import money_from_json
    from benchmarks.resource_ledger import cost_per_success

    if ledger_summary is None:
        return {"available": False, "statement": "no resource ledger"}
    total = money_from_json(ledger_summary["actual_total"])
    successes = sum(arm["unconditional_successes"] for arm in arms.values())
    return {
        "available": True,
        "currency": ledger_summary["currency"],
        "actual_total": ledger_summary["actual_total"],
        "estimate_total": ledger_summary["estimate_total"],
        "unknown_entry_ids": ledger_summary["unknown_entry_ids"],
        "cost_per_success": cost_per_success(total, successes),
        "cost_per_success_denominator": "unconditional_successes",
        "unconditional_successes": successes,
        "includes": "attempts, retries, scorers, judges, environments, "
                    "imports, storage, and reviews",
    }


# ── Snapshot freeze, storage, and replay ─────────────────────────────


def engine_digests() -> dict[str, Any]:
    """Pin the engine source, build, dependency lock, and runtime."""
    import sys

    source = (
        inspect.getsource(sys.modules[__name__])
        + inspect.getsource(analysis_rng)
        + inspect.getsource(analysis_engine)
    )
    lock_path = Path(__file__).resolve().parents[2] / "requirements.txt"
    lock_bytes = lock_path.read_bytes() if lock_path.is_file() else b""
    return {
        "source_digest": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "build_digest": hashlib.sha256(
            os.getenv("BMAS_BUILD_REVISION", "unknown").encode(),
        ).hexdigest(),
        "dependency_lock_digest": hashlib.sha256(lock_bytes).hexdigest(),
        "runtime_digest": hashlib.sha256(
            f"{platform.python_implementation()} "
            f"{platform.python_version()}".encode(),
        ).hexdigest(),
        "toolchain_versions": {
            "python": platform.python_version(),
            "statistics": analysis_engine.STATISTICS_CONTRACT,
            "numeric": "ieee-754-binary64",
            "vector_engine": analysis_engine.describe(),
            "engine": (
                analysis_engine.ENGINE_VECTORIZED
                if analysis_engine.available()
                else analysis_engine.ENGINE_REFERENCE
            ),
        },
    }


REQUIRED_PROVENANCE_FIELDS = (
    "run_manifest_digest",
    "runtime_specification_digest",
    "trace_digest",
    "final_output_digest",
    "seed_evidence",
    "versions",
    "ledger_references",
)


def execution_provenance(
    evidence_bundles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Report the execution provenance claims from stored evidence.

    ``execution_provenance_complete`` holds only when every required
    field validates on every bundle. The seed flags describe what the
    runtime requested and what the provider confirmed, never an equal
    result from another execution.
    """
    missing: list[str] = []
    if not evidence_bundles:
        missing.append("evidence_bundles")
    for bundle in evidence_bundles:
        attempt = str(bundle.get("attempt_id") or "?")
        for field in REQUIRED_PROVENANCE_FIELDS:
            value = bundle.get(field)
            if value in (None, "", {}, []):
                missing.append(f"{attempt}:{field}")
        if (bundle.get("resources") or {}).get("cost") is None:
            missing.append(f"{attempt}:resources.cost")
    seeds = [bundle.get("seed_evidence") or {} for bundle in evidence_bundles]
    return {
        "execution_provenance_complete": not missing,
        "missing_provenance_fields": sorted(missing),
        "execution_seed_requested": bool(seeds) and all(
            seed.get("seed_control") in ("recorded", "applied")
            for seed in seeds
        ),
        "execution_seed_confirmed": bool(seeds) and all(
            seed.get("applied_seed") is not None for seed in seeds
        ),
    }


def snapshot_record(
    *,
    specification: dict[str, Any],
    frozen_input: dict[str, Any],
    report: dict[str, Any],
    run_checksum: str,
    evidence_checksum: str,
    provenance: dict[str, Any],
    replayable: bool,
) -> dict[str, Any]:
    """Build one validating analysis snapshot with every pin."""
    output_checksum = content_checksum(report)
    record = {
        "schema_id": "analysis-snapshot",
        "schema_version": 2,
        "snapshot_id": f"snapshot-{uuid.uuid4().hex}",
        "run_checksum": run_checksum,
        "evidence_checksum": evidence_checksum,
        "filters": dict(specification["filters"]),
        "missingness_policy": (
            "predeclared_infrastructure_exclusions_with_paired_slot_removal"
        ),
        "estimand": specification,
        "unit_hierarchy": list(UNIT_HIERARCHY),
        "resampling": {
            "cluster_order": list(specification["cluster_order"]),
            "small_cluster_policy": specification["small_cluster"]["policy"],
            "resample_count": int(
                specification["resampling"]["resample_count"],
            ),
            "planned_repetitions": int(
                frozen_input.get("planned_repetitions") or 1,
            ),
        },
        "methods": {
            "estimator": "family_stratified_weighted_case_mean",
            "interval_method": (
                "family_stratified_weighted_case_bootstrap_percentile"
            ),
            "confidence_level": float(specification["confidence_level"]),
        },
        "multiplicity_groups": [
            specification["comparison_family"]["family_id"],
        ],
        "results_digest": report["results_digest"],
        "report_checksum": output_checksum,
        "engine": engine_digests(),
        "random_source": {
            "algorithm": analysis_rng.RNG_ALGORITHM,
            "algorithm_version": int(
                specification["resampling"]["algorithm_version"],
            ),
            "implementation": analysis_rng.implementation_for(
                int(specification["resampling"]["algorithm_version"]),
            ),
            "implementation_digest": analysis_rng.implementation_digest(),
            "master_seed": int(specification["resampling"]["master_seed"]),
            "derivation_schedule": analysis_rng.derivation_schedule(
                int(specification["resampling"]["algorithm_version"]),
            ),
        },
        "io_checksums": {
            "input": frozen_input["input_digest"],
            "output": output_checksum,
        },
        "replay": {
            "claim": (
                "analysis_replayable" if replayable
                else "analysis_not_replayable"
            ),
            **provenance,
        },
    }
    validate_record(record)
    return record


def replay(
    specification: dict[str, Any],
    run: dict[str, Any],
    stored_snapshot: dict[str, Any],
    *,
    planned_repetitions: int,
    ledger_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute one stored snapshot from stored evidence.

    The replay rebuilds the frozen input and the report from the same
    specification and compares the canonical checksums. Equal
    checksums support the analysis replay claim; they never describe
    the model execution itself as reproducible.
    """
    frozen_input = freeze_input(
        run, specification, planned_repetitions=planned_repetitions,
    )
    report = compute_report(
        specification, frozen_input, ledger_summary=ledger_summary,
    )
    equal = (
        frozen_input["input_digest"]
        == stored_snapshot["io_checksums"]["input"]
        and content_checksum(report)
        == stored_snapshot["io_checksums"]["output"]
        and report["results_digest"] == stored_snapshot["results_digest"]
    )
    return {
        "analysis_replayable": equal,
        "input_digest": frozen_input["input_digest"],
        "output_checksum": content_checksum(report),
        "results_digest": report["results_digest"],
        "claim": "analysis_replayable" if equal else "analysis_not_replayable",
        "execution_claim": (
            "analysis replay never proves external execution repeatability"
        ),
    }


async def current_snapshot(run_id: str) -> dict[str, Any] | None:
    """Read the newest stored snapshot of one run that no snapshot supersedes."""
    import json

    import database as db
    from benchmarks import evaluation_records

    superseded = {
        str(row["snapshot_id"])
        for row in await evaluation_records.list_snapshot_supersessions(run_id)
    }
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM analysis_snapshots WHERE run_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (run_id,),
        )
    for row in rows:
        if str(row["id"]) in superseded:
            continue
        return {**dict(row), "record": json.loads(row["record"])}
    return None


async def served_report(
    run_id: str,
    *,
    allow_unresolved: bool = False,
) -> dict[str, Any] | None:
    """Serve the current frozen snapshot of one run as its report.

    The report recomputes from the stored specification and the
    stored evidence, verifies its digests against the snapshot, and
    resolves every declared metric to one published definition. A
    metric without a published definition blocks the report unless
    the caller explicitly allows an unresolved display.
    """
    from benchmarks import evaluation_records, metric_registry, repository
    from benchmarks import resource_ledger

    snapshot = await current_snapshot(run_id)
    if snapshot is None:
        return None
    run = await repository.get_run(run_id)
    if run is None:
        raise FrozenAnalysisError(f"The run {run_id} does not exist")
    record = snapshot["record"]
    specification = record["estimand"]
    planned = int(record["resampling"].get("planned_repetitions") or 1)
    frozen_input = freeze_input(run, specification, planned_repetitions=planned)
    report = compute_report(specification, frozen_input)
    verified = report["results_digest"] == record["results_digest"]
    if not verified:
        entries = await resource_ledger.list_entries(run_id)
        summary = resource_ledger.summarize(entries, currency="USD")
        with_ledger = compute_report(
            specification, frozen_input, ledger_summary=summary,
        )
        if with_ledger["results_digest"] == record["results_digest"]:
            report = with_ledger
            verified = True
    definitions = {
        str(row["id"]): row["record"]
        for row in await evaluation_records.list_records("metric-definition")
    }
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for metric_id in report["metric_ids"]:
        try:
            resolved.append(metric_registry.resolve_display_metric(
                metric_id, definitions,
            ))
        except metric_registry.MetricRegistryError as error:
            unresolved.append({"metric_id": metric_id, "reason": str(error)})
    if not report["metric_ids"]:
        unresolved.append({
            "metric_id": "",
            "reason": "the frozen specification declares no metric "
                      "definitions; freeze with metric_ids",
        })
    if unresolved and not allow_unresolved:
        raise metric_registry.MetricRegistryError(
            "Every displayed metric references one published definition; "
            "unresolved: " + "; ".join(
                f"{entry['metric_id'] or '<none>'}: {entry['reason']}"
                for entry in unresolved
            )
        )
    planned_total = sum(
        int(arm["counts"]["planned"]) for arm in report["arms"].values()
    )
    return {
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "snapshot_id": str(snapshot["id"]),
        "replay_verified": verified,
        "results_digest": report["results_digest"],
        "stored_results_digest": record["results_digest"],
        "metrics": resolved,
        "unresolved_metrics": unresolved,
        "analysis": {
            "estimand": report["primary_estimand"],
            "statistical_unit": report["statistical_unit"],
            "specification_digest": report["specification_digest"],
            "replay_claim": record["replay"]["claim"],
        },
        "denominators": {
            "planned": planned_total,
            "statement": "planned slots minus predeclared infrastructure "
                         "exclusions",
        },
        "comparisons": report["comparisons"],
        "arms": report["arms"],
        "resources": report["resources"],
        "warnings": report["warnings"],
        "report": report,
    }


async def recompute_snapshot(
    snapshot_id: str,
    *,
    ledger_summary: dict[str, Any] | None,
    reason: str,
    reconciliation_id: str | None = None,
) -> dict[str, Any]:
    """Recompute one stored snapshot and record the supersession.

    The new snapshot freezes the same specification and the same
    planned repetitions over the stored evidence with the current
    ledger summary; the superseded snapshot stays immutable and the
    supersession row links the two.
    """
    from benchmarks import evaluation_records, facade

    stored = await evaluation_records.get_record(
        "analysis-snapshot", snapshot_id,
    )
    if stored is None:
        raise FrozenAnalysisError(f"The snapshot {snapshot_id} does not exist")
    record = stored["record"]
    replacement = await freeze_and_store(
        str(stored["run_id"]),
        specification=record["estimand"],
        planned_repetitions=int(
            record["resampling"].get("planned_repetitions") or 1,
        ),
        ledger_summary=ledger_summary,
    )
    supersession = await facade.execute(
        "supersede_analysis_snapshot",
        {
            "snapshot_id": snapshot_id,
            "superseded_by": replacement["snapshot_id"],
            "reason": reason,
            "reconciliation_id": reconciliation_id,
        },
    )
    return {
        "superseded_snapshot_id": snapshot_id,
        "snapshot_id": replacement["snapshot_id"],
        "supersession_id": supersession["supersession_id"],
        "results_digest": replacement["record"]["results_digest"],
        "resources": replacement["report"]["resources"],
    }


async def freeze_and_store(
    run_id: str,
    *,
    specification: dict[str, Any],
    planned_repetitions: int,
    ledger_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze, compute, verify replay, and store one snapshot."""
    import json

    import database as db
    from benchmarks import facade, repository

    run = await repository.get_run(run_id)
    if run is None:
        raise FrozenAnalysisError(f"The run {run_id} does not exist")
    frozen_input = freeze_input(
        run, specification, planned_repetitions=planned_repetitions,
    )
    report = compute_report(
        specification, frozen_input, ledger_summary=ledger_summary,
    )
    attempt_ids = sorted({
        str(attempt["id"]) for attempt in run.get("attempts") or []
    })
    bundles: list[dict[str, Any]] = []
    checksums: list[str] = []
    async with db._connect() as connection:  # noqa: SLF001
        for attempt_id in attempt_ids:
            cursor = await connection.execute(
                "SELECT record, record_checksum FROM attempt_evidence_bundles "
                "WHERE attempt_id = ?",
                (attempt_id,),
            )
            row = await cursor.fetchone()
            if row is not None:
                bundles.append(json.loads(row["record"]))
                checksums.append(str(row["record_checksum"]))
    run_checksum = content_checksum({
        "run_id": run_id,
        "execution_plan_checksum": run.get("execution_plan_checksum"),
        "test_configuration_checksum": run.get("test_configuration_checksum"),
        "dataset_checksum": run.get("dataset_checksum"),
    })
    evidence_checksum = hashlib.sha256(
        "".join(sorted(checksums)).encode("utf-8"),
    ).hexdigest()
    provenance = execution_provenance(bundles)
    probe = {
        "io_checksums": {
            "input": frozen_input["input_digest"],
            "output": content_checksum(report),
        },
        "results_digest": report["results_digest"],
    }
    replayed = replay(
        specification, run, probe,
        planned_repetitions=planned_repetitions,
        ledger_summary=ledger_summary,
    )
    record = snapshot_record(
        specification=specification,
        frozen_input=frozen_input,
        report=report,
        run_checksum=run_checksum,
        evidence_checksum=evidence_checksum,
        provenance=provenance,
        replayable=replayed["analysis_replayable"],
    )
    saved = await facade.execute(
        "record_analysis_snapshot", {"record": record, "run_id": run_id},
    )
    return {
        "snapshot_id": saved["id"],
        "record": record,
        "report": report,
        "frozen_input": frozen_input,
    }


# ── Evaluation study validation ──────────────────────────────────────


def validate_study(
    *,
    run_plan: dict[str, Any],
    source: dict[str, Any] | None,
    holdout_hidden: bool,
    report: dict[str, Any] | None,
    cost_includes_retries_and_control_plane: bool,
    stage: str = "publication",
) -> dict[str, Any]:
    """Check every study condition before a paid model study.

    The ``admission`` stage runs before any attempt executes, so no
    report exists yet; the report checks apply at the publication
    stage of the results.
    """
    if stage not in ("admission", "publication"):
        raise FrozenAnalysisError(f"Unknown study stage: {stage!r}")
    checks = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed),
                       "detail": detail})

    check(
        "source_pinned",
        bool(source and source.get("pinned_revision")
             and (source.get("license") or {}).get("name")),
        "the source revision and license stay pinned",
    )
    check("holdout_hidden", holdout_hidden,
          "the holdout split stays hidden from configuration work")
    case_ids = run_plan.get("case_ids") or []
    check("same_case_schedule", bool(case_ids),
          "every arm uses the same frozen case schedule")
    seed_schedule = run_plan.get("seed_schedule") or {}
    check(
        "same_repetition_seed_schedule",
        seed_schedule.get("base_seed") is not None
        and seed_schedule.get("scope") == "item-repetition",
        "every arm uses the same repetition seed schedule",
    )
    check(
        "interleaved_arms",
        (run_plan.get("arm_order") or {}).get("strategy")
        == "rotated_interleave",
        "the system interleaves arms",
    )
    check(
        "statistical_unit_declared",
        (run_plan.get("unit_hierarchy") or []) == UNIT_HIERARCHY,
        "the run plan declares the statistical unit hierarchy",
    )
    estimand = run_plan.get("estimand") or {}
    check(
        "primary_metric_fixed",
        bool(estimand.get("primary_estimand"))
        and bool(estimand.get("direction") or estimand.get("primary_metric")),
        "the primary metric and direction stay fixed",
    )
    if stage == "publication":
        check(
            "report_shows_failures_and_missingness",
            bool(report) and all(
                "counts" in arm and "unconditional_denominator" in arm
                for arm in (report or {}).get("arms", {}).values()
            ),
            "the report shows failures and missingness",
        )
    check("cost_includes_retries_and_control_plane",
          cost_includes_retries_and_control_plane,
          "cost includes retries and control-plane work")
    return {
        "ready": all(check["passed"] for check in checks),
        "checks": checks,
        "blocking": [
            check["check"] for check in checks if not check["passed"]
        ],
    }
