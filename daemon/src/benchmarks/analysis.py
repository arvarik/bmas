"""Build deterministic benchmark statistics and diagnostic comparisons."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from statistics import NormalDist, fmean, stdev
from typing import TYPE_CHECKING, Any

from benchmarks.provenance import content_checksum

if TYPE_CHECKING:
    from collections.abc import Iterable

ANALYSIS_VERSION = "2"
CONFIDENCE_LEVEL = 0.95
ALPHA = 1 - CONFIDENCE_LEVEL
BOOTSTRAP_RESAMPLES = 999
MAX_ITEM_DIFFERENCES = 500
MAX_SLICES = 100
_NORMAL = NormalDist()


def safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet software from executing exported text as a formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _percentile(values: list[float], percentile: float) -> float | None:
    """Return one linearly interpolated finite sample percentile."""
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    index = (len(clean) - 1) * min(max(percentile, 0.0), 1.0)
    lower_index = math.floor(index)
    upper_index = math.ceil(index)
    if lower_index == upper_index:
        return clean[lower_index]
    weight = index - lower_index
    return clean[lower_index] * (1 - weight) + clean[upper_index] * weight


def _seed(seed_key: str, values: list[float]) -> int:
    """Return one stable bootstrap seed from the metric identity and values."""
    payload = f"{ANALYSIS_VERSION}:{seed_key}:" + ",".join(
        format(value, ".17g") for value in values
    )
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def _adjusted_probability(zero_bias: float, acceleration: float, z: float) -> float:
    """Return one bounded BCa tail probability."""
    denominator = 1 - acceleration * (zero_bias + z)
    if abs(denominator) < 1e-12:
        return 0.0 if denominator < 0 else 1.0
    return min(
        max(_NORMAL.cdf(zero_bias + (zero_bias + z) / denominator), 0.0),
        1.0,
    )


def _mean_interval(
    values: list[float],
    *,
    seed_key: str,
    lower: float | None = None,
    upper: float | None = None,
) -> dict[str, float | int | str | None]:
    """Return a deterministic BCa bootstrap interval for one sample mean."""
    clean = [value for value in values if math.isfinite(value)]
    if not clean:
        return {
            "count": 0,
            "mean": None,
            "ci_low": None,
            "ci_high": None,
            "standard_error": None,
            "interval_status": "no_data",
        }
    mean = fmean(clean)
    if len(clean) < 2:
        return {
            "count": 1,
            "mean": mean,
            "ci_low": None,
            "ci_high": None,
            "standard_error": None,
            "interval_status": "insufficient_sample",
        }
    if all(value == clean[0] for value in clean):
        return {
            "count": len(clean),
            "mean": mean,
            "ci_low": None,
            "ci_high": None,
            "standard_error": 0.0,
            "interval_status": "degenerate_bootstrap",
        }

    rng = random.Random(_seed(seed_key, clean))
    size = len(clean)
    bootstrap = [
        fmean(rng.choices(clean, k=size))
        for _ in range(BOOTSTRAP_RESAMPLES)
    ]
    lower_count = sum(value < mean for value in bootstrap)
    equal_count = sum(value == mean for value in bootstrap)
    proportion = (lower_count + 0.5 * equal_count) / len(bootstrap)
    proportion = min(max(proportion, 1 / (2 * len(bootstrap))), 1 - 1 / (2 * len(bootstrap)))
    zero_bias = _NORMAL.inv_cdf(proportion)

    total = sum(clean)
    jackknife = [(total - value) / (size - 1) for value in clean]
    jackknife_mean = fmean(jackknife)
    deviations = [jackknife_mean - value for value in jackknife]
    denominator = 6 * sum(value * value for value in deviations) ** 1.5
    acceleration = (
        sum(value**3 for value in deviations) / denominator
        if denominator > 0
        else 0.0
    )
    low_probability = _adjusted_probability(
        zero_bias,
        acceleration,
        _NORMAL.inv_cdf(ALPHA / 2),
    )
    high_probability = _adjusted_probability(
        zero_bias,
        acceleration,
        _NORMAL.inv_cdf(1 - ALPHA / 2),
    )
    low = _percentile(bootstrap, min(low_probability, high_probability))
    high = _percentile(bootstrap, max(low_probability, high_probability))
    if lower is not None:
        low = max(lower, low) if low is not None else None
        high = max(lower, high) if high is not None else None
    if upper is not None:
        low = min(upper, low) if low is not None else None
        high = min(upper, high) if high is not None else None
    return {
        "count": size,
        "mean": mean,
        "ci_low": low,
        "ci_high": high,
        "standard_error": stdev(bootstrap),
        "interval_status": "estimated",
    }


def _metric(values: list[float], seed_key: str) -> dict[str, float | int | str | None]:
    """Return location, tail, total, and uncertainty metrics."""
    interval = _mean_interval(values, seed_key=seed_key)
    return {
        **interval,
        "total": sum(values) if values else None,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
    }


def _latest_attempts(attempts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest retry for each trial and repetition."""
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for attempt in attempts:
        key = (str(attempt["trial_id"]), int(attempt.get("repeat_index") or 1))
        previous = latest.get(key)
        if previous is None or int(attempt.get("retry_index") or 0) > int(
            previous.get("retry_index") or 0
        ):
            latest[key] = attempt
    return list(latest.values())


def _matches_filters(attempt: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Return true when one attempt matches all report filters."""
    if filters.get("subject") and attempt.get("subject") != filters["subject"]:
        return False
    if filters.get("split") and attempt.get("split") != filters["split"]:
        return False
    return not (
        filters.get("tag") and filters["tag"] not in (attempt.get("tags") or [])
    )


def _sign_test(deltas: list[float]) -> float | None:
    """Return an exact two-sided sign-test probability without ties."""
    wins = sum(delta > 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    trials = wins + losses
    if trials == 0:
        return None
    lower_tail = sum(
        math.comb(trials, index) for index in range(min(wins, losses) + 1)
    ) / 2**trials
    return min(1.0, 2 * lower_tail)


def _holm_adjust(values: list[float | None]) -> list[float | None]:
    """Apply the Holm step-down family-wise error correction."""
    indexed = sorted(
        (value, index)
        for index, value in enumerate(values)
        if value is not None and math.isfinite(value)
    )
    adjusted: list[float | None] = [None] * len(values)
    running = 0.0
    count = len(indexed)
    for rank, (value, original_index) in enumerate(indexed):
        running = max(running, min(1.0, (count - rank) * value))
        adjusted[original_index] = running
    return adjusted


def _sample_guidance(deltas: list[float], practical_difference: float) -> dict[str, Any]:
    """Estimate the paired sample count for 80 percent normal-approximation power."""
    if len(deltas) < 2 or practical_difference <= 0:
        return {
            "method": "normal_approximation_80_power",
            "practical_difference": practical_difference,
            "recommended_pairs": None,
            "reason": "A variance estimate and a positive practical difference are required",
        }
    deviation = stdev(deltas)
    if deviation == 0:
        recommended = 2 if abs(fmean(deltas)) >= practical_difference else None
    else:
        recommended = max(
            2,
            math.ceil(((1.96 + 0.84) * deviation / practical_difference) ** 2),
        )
    return {
        "method": "normal_approximation_80_power",
        "practical_difference": practical_difference,
        "recommended_pairs": recommended,
        "observed_standard_deviation": deviation,
        "reason": None if recommended is not None else "The observed effect has no variance",
    }


def _paired_effect(
    deltas: list[float],
    *,
    seed_key: str,
    practical_difference: float,
) -> dict[str, Any]:
    """Return paired uncertainty, effect size, significance, and power guidance."""
    interval = _mean_interval(
        deltas,
        seed_key=seed_key,
        lower=-1.0,
        upper=1.0,
    )
    wins = sum(delta > 0 for delta in deltas)
    ties = sum(delta == 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    deviation = stdev(deltas) if len(deltas) >= 2 else None
    standardized = (
        fmean(deltas) / deviation
        if deviation is not None and deviation > 0
        else None
    )
    return {
        **interval,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "probability_of_superiority": (
            (wins + 0.5 * ties) / len(deltas) if deltas else None
        ),
        "standardized_paired_effect": standardized,
        "p_value_raw": _sign_test(deltas),
        "p_value_adjusted": None,
        "practical_difference": practical_difference,
        "sample_guidance": _sample_guidance(deltas, practical_difference),
        "direction": "right_minus_left",
    }


def _classification(metric: dict[str, Any]) -> str:
    """Classify one corrected paired effect without overstating small samples."""
    if metric.get("count", 0) < 2:
        return "insufficient_sample"
    low = metric.get("ci_low")
    high = metric.get("ci_high")
    adjusted = metric.get("p_value_adjusted")
    threshold = float(metric.get("practical_difference") or 0)
    if low is not None and high is not None and low >= -threshold and high <= threshold:
        return "within_practical_range"
    if adjusted is None or adjusted > ALPHA:
        return "inconclusive"
    if low is not None and low > threshold:
        return "meaningful_improvement"
    if high is not None and high < -threshold:
        return "meaningful_regression"
    return "statistically_detectable_but_not_practical"


def _cohen_kappa(left: list[bool], right: list[bool]) -> float | None:
    """Return Cohen's kappa for paired binary judgments."""
    if not left or len(left) != len(right):
        return None
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    left_rate = sum(left) / len(left)
    right_rate = sum(right) / len(right)
    expected = left_rate * right_rate + (1 - left_rate) * (1 - right_rate)
    if expected == 1:
        return 1.0 if observed == 1 else None
    return (observed - expected) / (1 - expected)


def _review_diagnostics(
    score_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare automatic scorers with human judgments when reviews exist."""
    review_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        review_groups[str(review["attempt_id"])].append(review)
    human = {
        attempt_id: {
            "score": fmean(float(item["score"]) for item in items),
            "passed": sum(bool(item["passed"]) for item in items) >= len(items) / 2,
            "review_count": len(items),
        }
        for attempt_id, items in review_groups.items()
    }
    scorer_rows: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for score in score_rows:
        if score.get("status") == "scored" and score.get("score") is not None:
            scorer_rows[str(score["scorer_id"])][str(score["attempt_id"])] = score

    calibration = []
    for scorer_id, rows in sorted(scorer_rows.items()):
        common = sorted(set(rows) & set(human))
        if not common:
            continue
        machine_scores = [float(rows[key]["score"]) for key in common]
        machine_passes = [bool(rows[key].get("passed")) for key in common]
        human_scores = [float(human[key]["score"]) for key in common]
        human_passes = [bool(human[key]["passed"]) for key in common]
        calibration.append({
            "scorer_id": scorer_id,
            "count": len(common),
            "agreement_rate": sum(
                left == right
                for left, right in zip(machine_passes, human_passes, strict=True)
            ) / len(common),
            "cohen_kappa": _cohen_kappa(machine_passes, human_passes),
            "mean_absolute_error": fmean(
                abs(left - right)
                for left, right in zip(machine_scores, human_scores, strict=True)
            ),
            "brier_score": fmean(
                (score - float(passed)) ** 2
                for score, passed in zip(machine_scores, human_passes, strict=True)
            ),
        })

    agreement = []
    scorer_ids = sorted(scorer_rows)
    for left_index, left_id in enumerate(scorer_ids):
        for right_id in scorer_ids[left_index + 1:]:
            common = sorted(set(scorer_rows[left_id]) & set(scorer_rows[right_id]))
            if not common:
                continue
            left_passes = [bool(scorer_rows[left_id][key].get("passed")) for key in common]
            right_passes = [bool(scorer_rows[right_id][key].get("passed")) for key in common]
            agreement.append({
                "left_scorer_id": left_id,
                "right_scorer_id": right_id,
                "count": len(common),
                "agreement_rate": sum(
                    left == right
                    for left, right in zip(left_passes, right_passes, strict=True)
                ) / len(common),
                "cohen_kappa": _cohen_kappa(left_passes, right_passes),
            })
    return {
        "human_review": {
            "available": bool(human),
            "reviewed_attempt_count": len(human),
            "review_count": len(reviews),
            "reason": None if human else "No completed attempt has a human review",
        },
        "human_calibration": calibration,
        "scorer_agreement": agreement,
    }


def _slice_values(attempts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return bounded subject, split, and tag slice identities."""
    values: set[tuple[str, str]] = set()
    for attempt in attempts:
        for dimension in ("subject", "split"):
            value = str(attempt.get(dimension) or "").strip()
            if value:
                values.add((dimension, value))
        for tag in attempt.get("tags") or []:
            if str(tag).strip():
                values.add(("tag", str(tag).strip()))
    return sorted(values)[:MAX_SLICES]


def _slice_report(
    attempts: list[dict[str, Any]],
    scores_by_attempt: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return bounded per-slice arm outcomes for diagnosis."""
    reports: list[dict[str, Any]] = []
    for dimension, value in _slice_values(attempts):
        selected = [
            attempt
            for attempt in attempts
            if (
                value in (attempt.get("tags") or [])
                if dimension == "tag"
                else str(attempt.get(dimension) or "") == value
            )
        ]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in selected:
            grouped[str(attempt["arm_id"])].append(attempt)
        arms = []
        for arm_id, arm_attempts in sorted(grouped.items()):
            scores: dict[str, list[float]] = defaultdict(list)
            for attempt in arm_attempts:
                for score in scores_by_attempt.get(str(attempt["id"]), []):
                    if score.get("status") == "scored" and score.get("score") is not None:
                        scores[str(score["scorer_id"])].append(float(score["score"]))
            arms.append({
                "arm_id": arm_id,
                "arm_name": arm_attempts[0].get("arm_name"),
                "attempt_count": len(arm_attempts),
                "failure_rate": sum(
                    attempt.get("status") != "completed" for attempt in arm_attempts
                ) / len(arm_attempts),
                "scorers": [
                    {
                        "scorer_id": scorer_id,
                        **_mean_interval(
                            score_values,
                            seed_key=f"slice:{dimension}:{value}:{arm_id}:{scorer_id}",
                            lower=0.0,
                            upper=1.0,
                        ),
                    }
                    for scorer_id, score_values in sorted(scores.items())
                ],
            })
        reports.append({
            "dimension": dimension,
            "value": value,
            "attempt_count": len(selected),
            "arms": arms,
        })
    return reports


def build_run_report(
    run: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate one run without treating excluded attempts as zero scores."""
    selected_filters = {
        key: value for key, value in (filters or {}).items() if value
    }
    all_latest = _latest_attempts(run.get("attempts") or [])
    latest = [
        attempt
        for attempt in all_latest
        if _matches_filters(attempt, selected_filters)
    ]
    latest_ids = {str(attempt["id"]) for attempt in latest}
    score_rows = [
        score
        for score in run.get("scores") or []
        if str(score["attempt_id"]) in latest_ids
        and (
            not selected_filters.get("scorer_id")
            or score.get("scorer_id") == selected_filters["scorer_id"]
        )
    ]
    scores_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in score_rows:
        scores_by_attempt[str(score["attempt_id"])].append(score)

    configuration = run.get("test_configuration") or {}
    practical_difference = float(
        configuration.get("practical_difference", 0.01)
    )
    # The declared infrastructure exclusion policy. Only a predeclared
    # failure category can leave the unconditional denominator, and
    # every exclusion carries the policy reason.
    exclusion_policy = configuration.get("infrastructure_exclusions") or {}
    excluded_categories = set(exclusion_policy.get("categories") or [])
    exclusion_reason = str(
        exclusion_policy.get("reason") or "predeclared infrastructure policy"
    )
    repetitions = int(configuration.get("repetitions") or 1)
    arm_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in latest:
        arm_attempts[str(attempt["arm_id"])].append(attempt)
    all_filtered_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in run.get("attempts") or []:
        if _matches_filters(attempt, selected_filters):
            all_filtered_attempts[str(attempt["arm_id"])].append(attempt)
    arms: list[dict[str, Any]] = []
    error_categories: list[dict[str, Any]] = []
    for arm_id, attempts in arm_attempts.items():
        first = attempts[0]
        completed = [
            attempt for attempt in attempts if attempt.get("status") == "completed"
        ]
        failures = [
            attempt for attempt in attempts if attempt.get("status") != "completed"
        ]
        categories: dict[str, int] = defaultdict(int)
        for attempt in failures:
            categories[str(attempt.get("failure_category") or attempt.get("status") or "unknown")] += 1
        for category, count in sorted(categories.items()):
            error_categories.append({
                "arm_id": arm_id,
                "arm_name": first.get("arm_name"),
                "category": category,
                "count": count,
                "rate": count / len(attempts),
            })
        # Denominators over the arm's planned repetition slots. A slot
        # is one (case, repetition) pair; retries stay inside it.
        slot_items = {str(attempt["dataset_item_id"]) for attempt in attempts}
        planned = len(slot_items) * repetitions
        slots_present = {
            (str(attempt["dataset_item_id"]),
             int(attempt.get("repeat_index") or 1))
            for attempt in attempts
        }
        missing = max(planned - len(slots_present), 0)
        admitted = sum(
            attempt.get("status") != "queued" for attempt in attempts
        )
        exclusions = [
            {
                "dataset_item_id": str(attempt["dataset_item_id"]),
                "repeat_index": int(attempt.get("repeat_index") or 1),
                "category": str(attempt.get("failure_category")),
                "reason": exclusion_reason,
            }
            for attempt in failures
            if str(attempt.get("failure_category")) in excluded_categories
        ]
        excluded_slot_count = len(exclusions)
        # Every planned non-infrastructure-excluded slot counts. An
        # agent failure, a timeout, or a treatment-caused budget stop
        # stays a task failure with zero success.
        unconditional_denominator = max(planned - excluded_slot_count, 0)
        denominators = {
            "planned": planned,
            "admitted": admitted,
            "missing": missing,
            "completed": len(completed),
            "failed": len(failures) - excluded_slot_count,
            "excluded": excluded_slot_count,
            "exclusions": exclusions,
            "unconditional_denominator": unconditional_denominator,
        }
        # Resources count every attempt and retry, not only the
        # current completed attempts.
        arm_all = all_filtered_attempts.get(arm_id, attempts)
        resource_totals = {
            "attempt_count": len(arm_all),
            "cost_usd": sum(
                float(attempt["total_cost_usd"])
                for attempt in arm_all
                if attempt.get("total_cost_usd") is not None
            ),
            "tokens": sum(
                int(attempt["total_tokens"])
                for attempt in arm_all
                if attempt.get("total_tokens") is not None
            ),
            "duration_ms": sum(
                int(attempt["duration_ms"])
                for attempt in arm_all
                if attempt.get("duration_ms") is not None
            ),
        }
        score_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in attempts:
            for score in scores_by_attempt.get(str(attempt["id"]), []):
                score_groups[str(score["scorer_id"])].append(score)
        scorer_metrics = []
        for scorer_id, scores in sorted(score_groups.items()):
            scored = [
                score
                for score in scores
                if score.get("status") == "scored" and score.get("score") is not None
            ]
            values = [float(score["score"]) for score in scored]
            passed_count = sum(bool(score.get("passed")) for score in scored)
            scorer_metrics.append({
                "scorer_id": scorer_id,
                "scorer_name": scores[0].get("scorer_name"),
                "scorer_version": scores[0].get("scorer_version"),
                **_mean_interval(
                    values,
                    seed_key=f"arm:{arm_id}:score:{scorer_id}",
                    lower=0.0,
                    upper=1.0,
                ),
                "passed": passed_count,
                "failed": sum(not bool(score.get("passed")) for score in scored),
                "excluded": sum(score.get("status") != "scored" for score in scores),
                # Unconditional success spans every planned slot the
                # policy did not exclude; a failed attempt scores zero.
                "unconditional_success_rate": (
                    passed_count / unconditional_denominator
                    if unconditional_denominator
                    else None
                ),
                # Conditional answer quality covers completed scored
                # attempts only.
                "conditional_success_rate": (
                    passed_count / len(scored) if scored else None
                ),
            })
        arms.append({
            "arm_id": arm_id,
            "arm_name": first.get("arm_name"),
            "arm_slug": first.get("arm_slug"),
            "runtime_id": first.get("runtime_id"),
            "attempt_count": len(attempts),
            "completed_count": len(completed),
            "failure_count": len(failures),
            "failure_rate": len(failures) / len(attempts) if attempts else None,
            "cost_usd": _metric([
                float(attempt["total_cost_usd"])
                for attempt in completed
                if attempt.get("total_cost_usd") is not None
            ], f"arm:{arm_id}:cost"),
            "duration_ms": _metric([
                float(attempt["duration_ms"])
                for attempt in completed
                if attempt.get("duration_ms") is not None
            ], f"arm:{arm_id}:duration"),
            "tokens": _metric([
                float(attempt["total_tokens"])
                for attempt in completed
                if attempt.get("total_tokens") is not None
            ], f"arm:{arm_id}:tokens"),
            "denominators": denominators,
            "resource_totals": resource_totals,
            "scorers": scorer_metrics,
        })

    sorted_arms = sorted(arms, key=lambda item: str(item["arm_slug"]))
    attempt_by_arm_key: dict[
        str,
        dict[tuple[str, int], dict[str, Any]],
    ] = defaultdict(dict)
    for attempt in latest:
        key = (
            str(attempt["dataset_item_id"]),
            int(attempt.get("repeat_index") or 1),
        )
        attempt_by_arm_key[str(attempt["arm_id"])][key] = attempt

    comparisons: list[dict[str, Any]] = []
    item_differences: list[dict[str, Any]] = []
    statistical_metrics: list[dict[str, Any]] = []
    for left_index, left_arm in enumerate(sorted_arms):
        for right_arm in sorted_arms[left_index + 1:]:
            left_items = attempt_by_arm_key[str(left_arm["arm_id"])]
            right_items = attempt_by_arm_key[str(right_arm["arm_id"])]
            common_keys = sorted(set(left_items) & set(right_items))
            scorer_deltas: dict[str, list[float]] = defaultdict(list)
            for key in common_keys:
                left_attempt = left_items[key]
                right_attempt = right_items[key]
                left_scores = {
                    str(score["scorer_id"]): score
                    for score in scores_by_attempt.get(str(left_attempt["id"]), [])
                    if score.get("status") == "scored" and score.get("score") is not None
                }
                right_scores = {
                    str(score["scorer_id"]): score
                    for score in scores_by_attempt.get(str(right_attempt["id"]), [])
                    if score.get("status") == "scored" and score.get("score") is not None
                }
                for scorer_id in sorted(set(left_scores) & set(right_scores)):
                    left_score = float(left_scores[scorer_id]["score"])
                    right_score = float(right_scores[scorer_id]["score"])
                    delta = right_score - left_score
                    scorer_deltas[scorer_id].append(delta)
                    item_differences.append({
                        "left_arm_id": left_arm["arm_id"],
                        "right_arm_id": right_arm["arm_id"],
                        "scorer_id": scorer_id,
                        "dataset_item_id": key[0],
                        "item_key": left_attempt.get("item_key"),
                        "repeat_index": key[1],
                        "subject": left_attempt.get("subject"),
                        "split": left_attempt.get("split"),
                        "tags": left_attempt.get("tags") or [],
                        "left_score": left_score,
                        "right_score": right_score,
                        "delta": delta,
                    })
            comparison = {
                "left_arm_id": left_arm["arm_id"],
                "left_arm_name": left_arm["arm_name"],
                "left_arm_slug": left_arm["arm_slug"],
                "right_arm_id": right_arm["arm_id"],
                "right_arm_name": right_arm["arm_name"],
                "right_arm_slug": right_arm["arm_slug"],
                "matched_attempts": len(common_keys),
                "scorers": [],
            }
            for scorer_id, deltas in sorted(scorer_deltas.items()):
                metric = {
                    "scorer_id": scorer_id,
                    **_paired_effect(
                        deltas,
                        seed_key=(
                            f"comparison:{left_arm['arm_id']}:"
                            f"{right_arm['arm_id']}:{scorer_id}"
                        ),
                        practical_difference=practical_difference,
                    ),
                }
                comparison["scorers"].append(metric)
                statistical_metrics.append(metric)
            comparisons.append(comparison)

    adjusted = _holm_adjust([
        metric.get("p_value_raw") for metric in statistical_metrics
    ])
    for metric, adjusted_value in zip(statistical_metrics, adjusted, strict=True):
        metric["p_value_adjusted"] = adjusted_value
        metric["classification"] = _classification(metric)

    sorted_differences = sorted(
        item_differences,
        key=lambda item: (-abs(float(item["delta"])), str(item["dataset_item_id"])),
    )
    warnings = []
    if any(int(metric.get("count") or 0) < 5 for metric in statistical_metrics):
        warnings.append(
            "One or more paired comparisons use fewer than five scored pairs"
        )
    if not statistical_metrics:
        warnings.append("No paired scorer comparison is available")
    scoring_status = str(run.get("scoring_status") or "pending")
    analysis_status = str(run.get("analysis_status") or "pending")
    if analysis_status == "blocked":
        warnings.append(
            "A required scorer failed, so no valid analysis exists"
        )
    diagnostics = _review_diagnostics(
        score_rows,
        [
            review
            for review in run.get("human_reviews") or []
            if str(review["attempt_id"]) in latest_ids
        ],
    )
    report = {
        "schema_version": "2",
        "analysis": {
            "version": ANALYSIS_VERSION,
            "confidence_level": CONFIDENCE_LEVEL,
            "interval_method": "deterministic_bca_bootstrap",
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "paired_test": "exact_two_sided_sign_test",
            "multiple_comparison_method": "holm_bonferroni",
            "family_alpha": ALPHA,
            "practical_difference": practical_difference,
        },
        "interval_method": "deterministic_bca_bootstrap_95",
        "run": {
            "id": run["id"],
            "status": run["status"],
            "test_id": run["test_id"],
            "test_revision_id": run["test_revision_id"],
            "test_configuration_checksum": run.get("test_configuration_checksum"),
            "dataset_id": run.get("dataset_id"),
            "dataset_checksum": run.get("dataset_checksum"),
            "execution_plan_checksum": run.get("execution_plan_checksum"),
        },
        "filters": selected_filters,
        "latest_attempt_count": len(latest),
        "prior_attempt_count": len(run.get("attempts") or []) - len(all_latest),
        "arms": sorted_arms,
        "comparisons": comparisons,
        "diagnostics": {
            "error_categories": error_categories,
            "slices": _slice_report(latest, scores_by_attempt),
            "item_differences": sorted_differences[:MAX_ITEM_DIFFERENCES],
            "item_difference_count": len(sorted_differences),
            "item_differences_truncated": len(sorted_differences) > MAX_ITEM_DIFFERENCES,
            **diagnostics,
        },
        "warnings": warnings,
        "complete": run.get("status") == "completed",
        "scoring": {
            "scoring_status": scoring_status,
            "analysis_status": analysis_status,
        },
        "analysis_valid": analysis_status != "blocked",
        "denominators": {
            "policy": {
                "excluded_categories": sorted(excluded_categories),
                "reason": exclusion_reason,
            },
            "planned": sum(
                arm["denominators"]["planned"] for arm in sorted_arms
            ),
            "admitted": sum(
                arm["denominators"]["admitted"] for arm in sorted_arms
            ),
            "missing": sum(
                arm["denominators"]["missing"] for arm in sorted_arms
            ),
            "excluded": sum(
                arm["denominators"]["excluded"] for arm in sorted_arms
            ),
            "completed": sum(
                arm["denominators"]["completed"] for arm in sorted_arms
            ),
            "failed": sum(
                arm["denominators"]["failed"] for arm in sorted_arms
            ),
        },
    }
    return {**report, "report_checksum": content_checksum(report)}


def report_csv_rows(
    run: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return one current-attempt CSV row per scorer result."""
    selected_filters = {
        key: value for key, value in (filters or {}).items() if value
    }
    latest = [
        attempt
        for attempt in _latest_attempts(run.get("attempts") or [])
        if _matches_filters(attempt, selected_filters)
    ]
    scores_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in run.get("scores") or []:
        scores_by_attempt[str(score["attempt_id"])].append(score)
    reviews_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in run.get("human_reviews") or []:
        reviews_by_attempt[str(review["attempt_id"])].append(review)
    rows: list[dict[str, Any]] = []
    for attempt in latest:
        attempt_scores = scores_by_attempt.get(str(attempt["id"]))
        scores: list[dict[str, Any] | None] = [*attempt_scores] if attempt_scores else [None]
        reviews = reviews_by_attempt.get(str(attempt["id"]), [])
        for score in scores:
            if (
                score
                and selected_filters.get("scorer_id")
                and score.get("scorer_id") != selected_filters["scorer_id"]
            ):
                continue
            rows.append({
                "run_id": run["id"],
                "test_revision_id": run["test_revision_id"],
                "dataset_checksum": run.get("dataset_checksum"),
                "arm": attempt.get("arm_name"),
                "runtime_id": attempt.get("runtime_id"),
                "item_key": attempt.get("item_key"),
                "subject": attempt.get("subject"),
                "split": attempt.get("split"),
                "repeat_index": attempt.get("repeat_index"),
                "retry_index": attempt.get("retry_index"),
                "attempt_status": attempt.get("status"),
                "failure_category": attempt.get("failure_category"),
                "scorer_id": score.get("scorer_id") if score else None,
                "scorer_version": score.get("scorer_version") if score else None,
                "score_status": score.get("status") if score else None,
                "score": score.get("score") if score else None,
                "passed": score.get("passed") if score else None,
                "human_review_count": len(reviews),
                "human_score": (
                    fmean(float(review["score"]) for review in reviews)
                    if reviews
                    else None
                ),
                "cost_usd": attempt.get("total_cost_usd"),
                "duration_ms": attempt.get("duration_ms"),
                "tokens": attempt.get("total_tokens"),
                "task_id": attempt.get("task_id"),
                "attempt_snapshot_checksum": attempt.get("snapshot_checksum"),
            })
    return rows
