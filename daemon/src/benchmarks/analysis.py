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

ANALYSIS_VERSION = "3"
ANALYSIS_RNG = "bmas-analysis-rng"
CONFIDENCE_LEVEL = 0.95
ALPHA = 1 - CONFIDENCE_LEVEL
BOOTSTRAP_RESAMPLES = 999
SIGN_FLIP_RESAMPLES = 999
# At or below this paired case count the sign-flip test enumerates
# every sign pattern exactly instead of sampling.
EXACT_ENUMERATION_LIMIT = 12
WILSON_LABEL = "unclustered_slot_diagnostic"
MAX_ITEM_DIFFERENCES = 500
MAX_SLICES = 100
_NORMAL = NormalDist()
_WORD_MASK = 2**64 - 1


class AnalysisRandom:
    """The named deterministic analysis generator: ``bmas-analysis-rng``.

    The generator is SplitMix64 with unbiased rejection sampling for
    bounded draws. Every step uses exact 64-bit integer arithmetic, so
    any language reproduces the same candidates, rejections, indexes,
    and draws from the same seed.
    """

    def __init__(self, seed: int) -> None:
        self._state = seed & _WORD_MASK

    def next_raw(self) -> int:
        """Return the next raw 64-bit draw."""
        self._state = (self._state + 0x9E3779B97F4A7C15) & _WORD_MASK
        mixed = self._state
        mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & _WORD_MASK
        mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & _WORD_MASK
        return mixed ^ (mixed >> 31)

    def uniform_index(self, bound: int) -> int:
        """Return one unbiased index in ``[0, bound)`` by rejection."""
        if bound <= 0:
            raise ValueError("The draw bound must be positive")
        limit = (2**64 // bound) * bound
        while True:
            draw = self.next_raw()
            if draw < limit:
                return draw % bound

    def coin(self) -> bool:
        """Return one unbiased sign-flip bit."""
        return bool(self.next_raw() & 1)


def analysis_rng(seed_key: str) -> AnalysisRandom:
    """Return the named generator seeded from one stable identity."""
    payload = f"{ANALYSIS_RNG}:{ANALYSIS_VERSION}:{seed_key}"
    seed = int.from_bytes(
        hashlib.sha256(payload.encode()).digest()[:8], "big",
    )
    return AnalysisRandom(seed)


def wilson_interval(successes: int, total: int) -> dict[str, Any]:
    """Return one Wilson score interval as a slot-level diagnostic.

    The interval is an unclustered descriptive diagnostic only: it
    ignores case and family clustering, it never enters a primary
    gate, and it stays defined for all-success and all-failure
    samples.
    """
    if total <= 0:
        return {
            "successes": 0,
            "total": 0,
            "rate": None,
            "ci_low": None,
            "ci_high": None,
            "label": WILSON_LABEL,
        }
    z = _NORMAL.inv_cdf(1 - ALPHA / 2)
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            rate * (1 - rate) / total + z * z / (4 * total * total),
        )
        / denominator
    )
    return {
        "successes": successes,
        "total": total,
        "rate": rate,
        "ci_low": max(0.0, center - margin),
        "ci_high": min(1.0, center + margin),
        "label": WILSON_LABEL,
    }


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


def outcome_slots(
    attempts: Iterable[dict[str, Any]],
    excluded_categories: set[str],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Group one arm's attempts into sealed outcome slots.

    One slot exists for each case and planned repetition. Retries stay
    inside their original slot, ordered by retry index. The first
    valid substantive outcome seals the slot, and a later or duplicate
    attempt can never replace the sealed outcome. An excluded
    infrastructure failure never seals, so an infrastructure retry can
    still fill the slot.
    """
    ordered = sorted(
        attempts,
        key=lambda attempt: (
            str(attempt["dataset_item_id"]),
            int(attempt.get("repeat_index") or 1),
            int(attempt.get("retry_index") or 0),
            str(attempt["id"]),
        ),
    )
    slots: dict[tuple[str, int], dict[str, Any]] = {}
    for attempt in ordered:
        key = (
            str(attempt["dataset_item_id"]),
            int(attempt.get("repeat_index") or 1),
        )
        slot = slots.setdefault(
            key,
            {
                "attempts": [],
                "sealed": None,
                "post_seal_attempts": 0,
                "retry_attempts": 0,
            },
        )
        if slot["attempts"]:
            slot["retry_attempts"] += 1
        slot["attempts"].append(attempt)
        status = str(attempt.get("status"))
        category = str(attempt.get("failure_category") or "")
        substantive = status == "completed" or (
            status in {"failed", "cancelled"}
            and category not in excluded_categories
        )
        if slot["sealed"] is not None:
            if substantive:
                slot["post_seal_attempts"] += 1
            continue
        if substantive:
            slot["sealed"] = attempt
    return slots


def _slot_representatives(
    attempts: Iterable[dict[str, Any]],
    excluded_categories: set[str],
) -> list[dict[str, Any]]:
    """Return one representative attempt per arm, case, and repetition.

    A sealed slot returns its sealed outcome. An unsealed slot returns
    its latest attempt for progress and denominator accounting only.
    """
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_arm[str(attempt["arm_id"])].append(attempt)
    representatives: list[dict[str, Any]] = []
    for arm_attempts in by_arm.values():
        for slot in outcome_slots(arm_attempts, excluded_categories).values():
            representatives.append(slot["sealed"] or slot["attempts"][-1])
    return representatives


def _binary_case_reduction(
    passes: list[bool], estimand: dict[str, Any],
) -> bool:
    """Reduce repetition slot passes into one predeclared binary case."""
    reduction = str(estimand.get("binary_reduction") or "strict_majority")
    if reduction == "all":
        return all(passes)
    if reduction == "at_least_k":
        threshold = int(estimand.get("at_least_k") or 1)
        return sum(passes) >= threshold
    return sum(passes) * 2 > len(passes)


def case_outcomes(
    slots: dict[tuple[str, int], dict[str, Any]],
    scores_by_attempt: dict[str, list[dict[str, Any]]],
    scorer_id: str,
    estimand: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Nest sealed repetition slots into per-case outcomes.

    A sealed completed slot contributes its scored value; a sealed
    substantive failure contributes zero, the unconditional outcome. A
    case reduces its repetitions through the predeclared fractional
    mean and the predeclared binary reduction.
    """
    per_case: dict[str, dict[str, Any]] = {}
    for (item_id, _repeat), slot in sorted(slots.items()):
        sealed = slot["sealed"]
        if sealed is None:
            continue
        scores = {
            str(score["scorer_id"]): score
            for score in scores_by_attempt.get(str(sealed["id"]), [])
            if score.get("status") == "scored"
            and score.get("score") is not None
        }
        if str(sealed.get("status")) == "completed":
            score = scores.get(scorer_id)
            if score is None:
                continue
            value = float(score["score"])
            passed = bool(score.get("passed"))
        else:
            value = 0.0
            passed = False
        case = per_case.setdefault(
            item_id,
            {
                "item_key": str(sealed.get("item_key") or item_id),
                "family": str(
                    sealed.get(
                        str(estimand.get("family_field") or "subject"),
                    )
                    or "default",
                ),
                "slot_values": [],
                "slot_passes": [],
                "retry_attempts": 0,
            },
        )
        case["slot_values"].append(value)
        case["slot_passes"].append(passed)
        case["retry_attempts"] += int(slot["retry_attempts"])
    for case in per_case.values():
        case["fractional"] = fmean(case["slot_values"])
        case["binary"] = _binary_case_reduction(
            case["slot_passes"], estimand,
        )
    return per_case


def mcnemar_exact(
    left_passes: list[Any], right_passes: list[Any],
) -> dict[str, Any]:
    """Return the exact McNemar test over paired binary case outcomes.

    The test accepts only booleans from a predeclared binary case
    reduction; a fractional case outcome fails closed.
    """
    for value in [*left_passes, *right_passes]:
        if not isinstance(value, bool):
            raise ValueError(
                "McNemar accepts only predeclared binary case outcomes"
            )
    if len(left_passes) != len(right_passes):
        raise ValueError("McNemar needs one pair per case")
    discordant_left = sum(
        left and not right
        for left, right in zip(left_passes, right_passes, strict=True)
    )
    discordant_right = sum(
        right and not left
        for left, right in zip(left_passes, right_passes, strict=True)
    )
    total = discordant_left + discordant_right
    if total == 0:
        p_value = None
    else:
        tail = sum(
            math.comb(total, index)
            for index in range(min(discordant_left, discordant_right) + 1)
        ) / 2**total
        p_value = min(1.0, 2 * tail)
    return {
        "method": "mcnemar_exact_binomial",
        "reduction": "predeclared_binary",
        "discordant_left": discordant_left,
        "discordant_right": discordant_right,
        "p_value": p_value,
    }


def _weighted_theta(
    entries: list[dict[str, Any]],
) -> float | None:
    """Aggregate case deltas with each weight applied exactly once."""
    families: dict[str, dict[str, float]] = {}
    for entry in entries:
        family = families.setdefault(
            entry["family"],
            {"weight": entry["family_weight"], "numerator": 0.0,
             "denominator": 0.0},
        )
        family["numerator"] += entry["weight"] * entry["delta"]
        family["denominator"] += entry["weight"]
    total_weight = 0.0
    total = 0.0
    for family in families.values():
        if family["denominator"] <= 0 or family["weight"] <= 0:
            continue
        total += family["weight"] * (
            family["numerator"] / family["denominator"]
        )
        total_weight += family["weight"]
    if total_weight <= 0:
        return None
    return total / total_weight


def paired_case_comparison(
    left_cases: dict[str, dict[str, Any]],
    right_cases: dict[str, dict[str, Any]],
    *,
    estimand: dict[str, Any],
    seed_key: str,
    practical_difference: float,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    sign_flip_resamples: int = SIGN_FLIP_RESAMPLES,
    record_draws: bool = False,
) -> dict[str, Any]:
    """Compare two arms at the case level under the frozen estimand.

    Cases pair on their identifiers. The interval comes from a
    family-stratified case bootstrap with uniform draws, and each
    declared case weight applies exactly once during aggregation. The
    significance test is a paired sign-flip randomization over the
    same weighted statistic, exact by enumeration for small case
    counts. McNemar runs beside it, only on the predeclared binary
    case reduction.
    """
    family_weights = {
        str(name): float(value)
        for name, value in (estimand.get("family_weights") or {}).items()
    }
    case_weights = {
        str(name): float(value)
        for name, value in (estimand.get("case_weights") or {}).items()
    }
    common = sorted(set(left_cases) & set(right_cases))
    entries: list[dict[str, Any]] = []
    removed_zero_weight: list[str] = []
    for item_id in common:
        left = left_cases[item_id]
        right = right_cases[item_id]
        key = str(left["item_key"])
        weight = case_weights.get(key, 1.0)
        family = str(left["family"])
        family_weight = family_weights.get(family, 1.0)
        if weight <= 0 or family_weight <= 0:
            removed_zero_weight.append(key)
            continue
        entries.append({
            "item_id": item_id,
            "item_key": key,
            "family": family,
            "weight": weight,
            "family_weight": family_weight,
            "delta": float(right["fractional"]) - float(left["fractional"]),
            "left_binary": bool(left["binary"]),
            "right_binary": bool(right["binary"]),
        })
    deltas = [entry["delta"] for entry in entries]
    count = len(entries)
    theta = _weighted_theta(entries)
    wins = sum(delta > 0 for delta in deltas)
    ties = sum(delta == 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    families = sorted({entry["family"] for entry in entries})
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_family[entry["family"]].append(entry)
    min_family_cases = int(estimand.get("min_family_cases") or 5)
    small_families = sorted(
        family
        for family, members in by_family.items()
        if len(members) < min_family_cases
    )

    interval_status = "estimated"
    ci_low: float | None = None
    ci_high: float | None = None
    standard_error: float | None = None
    draws: list[list[str]] = []
    if count == 0 or theta is None:
        interval_status = "no_data"
    elif count == 1:
        interval_status = "insufficient_sample"
    else:
        rng = analysis_rng(f"bootstrap:{seed_key}")
        replicates: list[float] = []
        for _ in range(bootstrap_resamples):
            resample: list[dict[str, Any]] = []
            drawn: list[str] = []
            for family in families:
                members = by_family[family]
                for _ in range(len(members)):
                    # Uniform draws only. The declared case weight
                    # never changes the draw probability; it applies
                    # once in the aggregation below.
                    member = members[rng.uniform_index(len(members))]
                    resample.append(member)
                    drawn.append(member["item_key"])
            replicate = _weighted_theta(resample)
            if replicate is not None:
                replicates.append(replicate)
            if record_draws:
                draws.append(drawn)
        if len({format(value, ".17g") for value in replicates}) <= 1:
            # Every stratified replicate is identical, so the
            # resampling distribution is a point mass and the interval
            # collapses onto the estimate.
            interval_status = "degenerate_bootstrap"
            if replicates:
                ci_low = min(replicates)
                ci_high = max(replicates)
                standard_error = 0.0
        else:
            ci_low = _percentile(replicates, ALPHA / 2)
            ci_high = _percentile(replicates, 1 - ALPHA / 2)
            standard_error = stdev(replicates)

    sign_flip: dict[str, Any] = {
        "method": "paired_sign_flip",
        "statistic": "family_stratified_weighted_mean",
        "p_value": None,
        "mode": None,
        "resamples": None,
    }
    flips: list[list[bool]] = []
    if count >= 1 and theta is not None:
        observed = abs(theta)

        def _flipped(pattern: list[bool]) -> float | None:
            flipped_entries = [
                {**entry, "delta": -entry["delta"] if flip else entry["delta"]}
                for entry, flip in zip(entries, pattern, strict=True)
            ]
            return _weighted_theta(flipped_entries)

        if count <= EXACT_ENUMERATION_LIMIT:
            at_least = 0
            total_patterns = 2**count
            for mask in range(total_patterns):
                pattern = [bool(mask >> bit & 1) for bit in range(count)]
                value = _flipped(pattern)
                if value is not None and abs(value) >= observed - 1e-12:
                    at_least += 1
            sign_flip.update({
                "p_value": at_least / total_patterns,
                "mode": "exact_enumeration",
                "resamples": total_patterns,
            })
        else:
            rng = analysis_rng(f"sign-flip:{seed_key}")
            at_least = 0
            for _ in range(sign_flip_resamples):
                pattern = [rng.coin() for _ in range(count)]
                value = _flipped(pattern)
                if value is not None and abs(value) >= observed - 1e-12:
                    at_least += 1
                if record_draws:
                    flips.append(pattern)
            sign_flip.update({
                "p_value": (1 + at_least) / (sign_flip_resamples + 1),
                "mode": "monte_carlo",
                "resamples": sign_flip_resamples,
            })

    mcnemar = mcnemar_exact(
        [entry["left_binary"] for entry in entries],
        [entry["right_binary"] for entry in entries],
    )
    deviation = stdev(deltas) if count >= 2 else None
    result = {
        "count": count,
        "mean": theta,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "standard_error": standard_error,
        "interval_status": interval_status,
        "interval_method": "family_stratified_weighted_case_bootstrap",
        "rng": ANALYSIS_RNG,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "probability_of_superiority": (
            (wins + 0.5 * ties) / count if count else None
        ),
        "standardized_paired_effect": (
            fmean(deltas) / deviation
            if deviation is not None and deviation > 0
            else None
        ),
        "p_value_raw": sign_flip["p_value"],
        "p_value_adjusted": None,
        "sign_flip": sign_flip,
        "mcnemar": mcnemar,
        "practical_difference": practical_difference,
        "sample_guidance": _sample_guidance(deltas, practical_difference),
        "direction": "right_minus_left",
        "unit": "case",
        "families": families,
        "small_families": small_families,
        "small_cluster_policy": {
            "min_family_cases": min_family_cases,
            "policy": "flagged_not_dropped",
            "exact_enumeration_limit": EXACT_ENUMERATION_LIMIT,
        },
        "removed_zero_weight_cases": sorted(removed_zero_weight),
        "paired_cases": [entry["item_key"] for entry in entries],
    }
    if record_draws:
        result["bootstrap_draws"] = draws
        result["sign_flip_patterns"] = flips
    return result


def _matches_filters(attempt: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Return true when one attempt matches all report filters."""
    if filters.get("subject") and attempt.get("subject") != filters["subject"]:
        return False
    if filters.get("split") and attempt.get("split") != filters["split"]:
        return False
    return not (
        filters.get("tag") and filters["tag"] not in (attempt.get("tags") or [])
    )



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
        # Chance agreement is total, so kappa is zero over zero:
        # undefined. The raw agreement rate beside it stays defined.
        return None
    return (observed - expected) / (1 - expected)


def _review_diagnostics(
    score_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare automatic scorers with human judgments when reviews exist."""
    review_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        review_groups[str(review["attempt_id"])].append(review)
    # Review states: an even split is a tie that needs adjudication;
    # an odd vote after disagreement records an adjudicated result.
    review_states: list[dict[str, Any]] = []
    human: dict[str, dict[str, Any]] = {}
    for attempt_id, items in sorted(review_groups.items()):
        pass_votes = sum(bool(item["passed"]) for item in items)
        fail_votes = len(items) - pass_votes
        if pass_votes == fail_votes:
            state = "tie"
        elif pass_votes > fail_votes:
            state = "passed"
        else:
            state = "failed"
        review_states.append({
            "attempt_id": attempt_id,
            "review_count": len(items),
            "pass_votes": pass_votes,
            "fail_votes": fail_votes,
            "state": state,
            "adjudicated": state != "tie" and min(pass_votes, fail_votes) > 0,
        })
        if state == "tie":
            # A tied review carries no resolved judgment; it stays out
            # of the calibration until adjudication resolves it.
            continue
        human[attempt_id] = {
            "score": fmean(float(item["score"]) for item in items),
            "passed": state == "passed",
            "review_count": len(items),
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
        "review_states": review_states,
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


def _run_estimand(run: dict[str, Any]) -> dict[str, Any]:
    """Read the frozen estimand from the run plan, with a legacy fill.

    A run created after the estimand freeze carries the complete
    estimand in its execution plan. A legacy run derives the same
    defaults from its configuration, marked as derived.
    """
    plan = run.get("execution_plan") or {}
    if isinstance(plan, dict) and isinstance(plan.get("estimand"), dict):
        return dict(plan["estimand"])
    configuration = run.get("test_configuration") or {}
    statistics = configuration.get("statistics") or {}
    return {
        "target_population": "declared dataset cases",
        "primary_estimand": "paired-difference-in-weighted-case-means",
        "family_field": str(statistics.get("family_field") or "subject"),
        "family_weights": statistics.get("family_weights") or {},
        "case_weights": statistics.get("case_weights") or {},
        "binary_reduction": str(
            statistics.get("binary_reduction") or "strict_majority",
        ),
        "at_least_k": statistics.get("at_least_k"),
        "fractional_reduction": "mean",
        "min_family_cases": int(statistics.get("min_family_cases") or 5),
        "derived_from_configuration": True,
    }


def build_run_report(
    run: dict[str, Any],
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate one run without treating excluded attempts as zero scores."""
    selected_filters = {
        key: value for key, value in (filters or {}).items() if value
    }
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
    estimand = _run_estimand(run)
    # Every terminal reason resolves through the pinned mapping set.
    # An unknown reason or a stale mapping rejects before any number
    # computes.
    from benchmarks.outcome_mappings import validate_run_outcome_contract

    validate_run_outcome_contract(run)
    mapping_set = (run.get("execution_plan") or {}).get(
        "outcome_mapping_set",
    ) or {}
    # One representative per arm, case, and repetition slot: the
    # sealed first valid substantive outcome, or the latest in-flight
    # attempt for progress accounting. A late or duplicate attempt
    # never replaces a sealed slot outcome.
    all_sealed = _slot_representatives(
        run.get("attempts") or [], excluded_categories,
    )
    latest = [
        attempt
        for attempt in all_sealed
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
    arm_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in latest:
        arm_attempts[str(attempt["arm_id"])].append(attempt)
    all_filtered_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in run.get("attempts") or []:
        if _matches_filters(attempt, selected_filters):
            all_filtered_attempts[str(attempt["arm_id"])].append(attempt)
    # Sealed outcome slots and nested case outcomes per arm and
    # scorer. Retries and duplicates stay inside their slots, so the
    # case level is the statistical unit for every paired comparison.
    slots_by_arm: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    cases_by_arm: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for arm_id, arm_all in all_filtered_attempts.items():
        slots_by_arm[arm_id] = outcome_slots(arm_all, excluded_categories)
    for arm_id, slots in slots_by_arm.items():
        scorer_ids = {
            str(score["scorer_id"])
            for slot in slots.values()
            if slot["sealed"] is not None
            for score in scores_by_attempt.get(str(slot["sealed"]["id"]), [])
        }
        cases_by_arm[arm_id] = {
            scorer_id: case_outcomes(
                slots, scores_by_attempt, scorer_id, estimand,
            )
            for scorer_id in sorted(scorer_ids)
        }
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
            arm_cases = cases_by_arm.get(arm_id, {}).get(scorer_id, {})
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
                # A descriptive slot-level diagnostic only: it ignores
                # clustering and never enters a primary gate.
                "wilson_unclustered_diagnostic": wilson_interval(
                    sum(bool(case["binary"]) for case in arm_cases.values()),
                    len(arm_cases),
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
            left_cases_by_scorer = cases_by_arm.get(
                str(left_arm["arm_id"]), {},
            )
            right_cases_by_scorer = cases_by_arm.get(
                str(right_arm["arm_id"]), {},
            )
            shared_scorers = sorted(
                set(scorer_deltas)
                | (set(left_cases_by_scorer) & set(right_cases_by_scorer)),
            )
            for scorer_id in shared_scorers:
                metric = {
                    "scorer_id": scorer_id,
                    **paired_case_comparison(
                        left_cases_by_scorer.get(scorer_id, {}),
                        right_cases_by_scorer.get(scorer_id, {}),
                        estimand=estimand,
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
            "paired_interval_method": (
                "family_stratified_weighted_case_bootstrap"
            ),
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "paired_test": "paired_sign_flip",
            "sign_flip_resamples": SIGN_FLIP_RESAMPLES,
            "exact_enumeration_limit": EXACT_ENUMERATION_LIMIT,
            "rng": ANALYSIS_RNG,
            "multiple_comparison_method": "holm_bonferroni",
            "family_alpha": ALPHA,
            "practical_difference": practical_difference,
            "statistical_unit": "case",
            "estimand": estimand,
            "outcome_mapping_set": {
                "digest": str(mapping_set.get("digest") or ""),
                "contract_version": str(
                    mapping_set.get("contract_version") or "",
                ),
            },
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
        "prior_attempt_count": len(run.get("attempts") or []) - len(all_sealed),
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
