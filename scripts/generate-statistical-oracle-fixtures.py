#!/usr/bin/env python3
"""Generate the versioned statistical oracle fixtures.

The script is an independent reference implementation: it imports
nothing from the daemon source tree. It computes every expected value
from first principles with exact rational arithmetic where possible:
the family-stratified weighted estimate, the exact sign-flip
enumeration, the Monte Carlo sign-flip and stratified bootstrap from
the published ``bmas-analysis-rng`` specification (SplitMix64 with
rejection sampling), the exact McNemar binomial, and the Wilson score
interval. The output file is a committed, versioned fixture; the
daemon test suite compares the live implementation against it with
tolerances only for floating-point differences.

Run from the repository root:

    python3 scripts/generate-statistical-oracle-fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from fractions import Fraction
from pathlib import Path

FIXTURE_VERSION = "1"
ANALYSIS_VERSION = "3"
RNG_NAME = "bmas-analysis-rng"
ALPHA = Fraction(5, 100)
EXACT_ENUMERATION_LIMIT = 12
WORD_MASK = 2**64 - 1

OUTPUT = Path(__file__).resolve().parents[1] / (
    "daemon/tests/fixtures/statistical_oracle.json"
)


class ReferenceRandom:
    """An independent SplitMix64 implementation of the published spec."""

    def __init__(self, seed: int) -> None:
        self.state = seed & WORD_MASK

    def raw(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & WORD_MASK
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & WORD_MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & WORD_MASK
        return value ^ (value >> 31)

    def bounded(self, bound: int) -> int:
        threshold = (2**64 // bound) * bound
        while True:
            candidate = self.raw()
            if candidate < threshold:
                return candidate % bound

    def coin(self) -> bool:
        return bool(self.raw() & 1)


def rng_for(seed_key: str) -> ReferenceRandom:
    payload = f"{RNG_NAME}:{ANALYSIS_VERSION}:{seed_key}"
    seed = int.from_bytes(
        hashlib.sha256(payload.encode()).digest()[:8], "big",
    )
    return ReferenceRandom(seed)


def theta_of(entries: list[dict]) -> Fraction | None:
    """The family-stratified weighted mean with weights applied once."""
    families: dict[str, dict[str, Fraction]] = {}
    for entry in entries:
        family = families.setdefault(
            entry["family"],
            {
                "weight": Fraction(str(entry["family_weight"])),
                "numerator": Fraction(0),
                "denominator": Fraction(0),
            },
        )
        weight = Fraction(str(entry["weight"]))
        family["numerator"] += weight * Fraction(str(entry["delta"]))
        family["denominator"] += weight
    total = Fraction(0)
    total_weight = Fraction(0)
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


def exact_sign_flip(entries: list[dict]) -> Fraction | None:
    observed = theta_of(entries)
    if observed is None or not entries:
        return None
    count = len(entries)
    at_least = 0
    for mask in range(2**count):
        flipped = [
            {
                **entry,
                "delta": -Fraction(str(entry["delta"]))
                if mask >> bit & 1
                else Fraction(str(entry["delta"])),
            }
            for bit, entry in enumerate(entries)
        ]
        value = theta_of(flipped)
        if value is not None and abs(value) >= abs(observed):
            at_least += 1
    return Fraction(at_least, 2**count)


def monte_carlo_sign_flip(
    entries: list[dict], seed_key: str, resamples: int,
) -> Fraction | None:
    observed = theta_of(entries)
    if observed is None:
        return None
    rng = rng_for(f"sign-flip:{seed_key}")
    at_least = 0
    for _ in range(resamples):
        pattern = [rng.coin() for _ in entries]
        flipped = [
            {
                **entry,
                "delta": -Fraction(str(entry["delta"]))
                if flip
                else Fraction(str(entry["delta"])),
            }
            for entry, flip in zip(entries, pattern, strict=True)
        ]
        value = theta_of(flipped)
        # The float implementation compares against an epsilon; exact
        # rational magnitudes make the same decision away from ties.
        if value is not None and abs(value) >= abs(observed):
            at_least += 1
    return Fraction(1 + at_least, resamples + 1)


def percentile(values: list[float], fraction: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    index = (len(ordered) - 1) * min(max(fraction, 0.0), 1.0)
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return ordered[low]
    weight = index - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def stratified_bootstrap(
    entries: list[dict],
    seed_key: str,
    resamples: int,
    *,
    record_draws: bool = False,
) -> dict:
    families: dict[str, list[dict]] = {}
    for entry in entries:
        families.setdefault(entry["family"], []).append(entry)
    ordered_families = sorted(families)
    rng = rng_for(f"bootstrap:{seed_key}")
    replicates: list[float] = []
    draws: list[list[str]] = []
    for _ in range(resamples):
        resample: list[dict] = []
        drawn: list[str] = []
        for family in ordered_families:
            members = families[family]
            for _ in range(len(members)):
                member = members[rng.bounded(len(members))]
                resample.append(member)
                drawn.append(member["item_key"])
        value = theta_of(resample)
        if value is not None:
            replicates.append(float(value))
        if record_draws:
            draws.append(drawn)
    distinct = {format(value, ".17g") for value in replicates}
    if len(distinct) <= 1:
        interval = {
            "interval_status": "degenerate_bootstrap",
            "ci_low": min(replicates) if replicates else None,
            "ci_high": max(replicates) if replicates else None,
            "standard_error": 0.0 if replicates else None,
        }
    else:
        interval = {
            "interval_status": "estimated",
            "ci_low": percentile(replicates, float(ALPHA) / 2),
            "ci_high": percentile(replicates, 1 - float(ALPHA) / 2),
            "standard_error": statistics.stdev(replicates),
        }
    if record_draws:
        interval["draws"] = draws
    return interval


def mcnemar(entries: list[dict]) -> float | None:
    left_only = sum(
        1
        for entry in entries
        if entry["left_binary"] and not entry["right_binary"]
    )
    right_only = sum(
        1
        for entry in entries
        if entry["right_binary"] and not entry["left_binary"]
    )
    total = left_only + right_only
    if total == 0:
        return None
    tail = Fraction(
        sum(
            math.comb(total, index)
            for index in range(min(left_only, right_only) + 1)
        ),
        2**total,
    )
    return float(min(Fraction(1), 2 * tail))


def wilson(successes: int, total: int) -> dict:
    if total <= 0:
        return {"rate": None, "ci_low": None, "ci_high": None}
    z = statistics.NormalDist().inv_cdf(1 - float(ALPHA) / 2)
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
        / denominator
    )
    return {
        "rate": rate,
        "ci_low": max(0.0, center - margin),
        "ci_high": min(1.0, center + margin),
    }


def entry(
    index: int,
    family: str,
    left: float,
    right: float,
    *,
    weight: float = 1.0,
    family_weight: float = 1.0,
) -> dict:
    return {
        "item_key": f"case-{index:02d}",
        "family": family,
        "weight": weight,
        "family_weight": family_weight,
        "left": left,
        "right": right,
        "left_binary": left >= 0.5,
        "right_binary": right >= 0.5,
        "delta": right - left,
    }


def build_cases() -> list[dict]:
    cases: list[dict] = []

    def add(name: str, entries: list[dict], *, note: str) -> None:
        cases.append({"name": name, "note": note, "entries": entries})

    add(
        "equal_paired_binary_outcomes",
        [entry(i, "one", 1.0 if i % 2 else 0.0, 1.0 if i % 2 else 0.0)
         for i in range(6)],
        note="Every pair agrees, so the difference is exactly zero.",
    )
    add(
        "one_clear_binary_improvement",
        [entry(i, "one", 0.0, 1.0) for i in range(8)],
        note="Every case flips from failure to success.",
    )
    add(
        "one_clear_binary_regression",
        [entry(i, "one", 1.0, 0.0) for i in range(8)],
        note="Every case flips from success to failure.",
    )
    add(
        "all_successes",
        [entry(i, "one", 1.0, 1.0) for i in range(5)],
        note="Both arms solve every case; McNemar has no discordant pair.",
    )
    add(
        "all_failures",
        [entry(i, "one", 0.0, 0.0) for i in range(5)],
        note="Both arms fail every case; McNemar has no discordant pair.",
    )
    add(
        "unequal_task_family_sizes",
        [entry(0, "small", 0.0, 1.0), entry(1, "small", 0.0, 0.5)]
        + [entry(2 + i, "large", 0.5, 0.75) for i in range(6)],
        note="Two cases in one family, six in the other, equal family "
             "weights.",
    )
    add(
        "unequal_family_weights_with_equal_case_weights",
        [entry(0, "alpha", 0.0, 1.0, family_weight=3.0),
         entry(1, "alpha", 0.0, 1.0, family_weight=3.0),
         entry(2, "beta", 0.0, 0.25),
         entry(3, "beta", 0.0, 0.25)],
        note="The alpha family carries triple weight in aggregation.",
    )
    add(
        "custom_case_weights_inside_one_family",
        [entry(0, "one", 0.0, 1.0, weight=4.0),
         entry(1, "one", 0.0, 0.5),
         entry(2, "one", 0.0, 0.0)],
        note="One case dominates through its declared weight.",
    )
    add(
        "zero_case_weight_removed_and_renormalized",
        [entry(0, "one", 0.0, 1.0),
         entry(1, "one", 0.0, 0.5),
         entry(2, "one", 1.0, 0.0, weight=0.0)],
        note="The zero-weight case leaves the estimand and the "
             "remaining weights renormalize.",
    )
    add(
        "weighted_family_with_fewer_than_five_usable_items",
        [entry(0, "tiny", 0.0, 1.0), entry(1, "tiny", 0.0, 0.75),
         entry(2, "tiny", 0.25, 0.75)]
        + [entry(3 + i, "large", 0.5, 0.5) for i in range(6)],
        note="The tiny family flags the small-cluster policy.",
    )
    add(
        "fractional_case_outcomes",
        [entry(0, "one", 0.25, 0.75), entry(1, "one", 0.5, 0.5),
         entry(2, "one", 0.1, 0.4), entry(3, "one", 0.9, 0.6),
         entry(4, "one", 0.3, 0.8)],
        note="Fractional outcomes use the paired sign-flip, never "
             "McNemar.",
    )
    add(
        "monte_carlo_regime_above_the_enumeration_limit",
        [entry(i, "one", 0.0, 0.6 + 0.02 * (i % 5)) for i in range(15)],
        note="Fifteen cases exceed the exact enumeration limit, so the "
             "sign-flip samples with the named generator.",
    )
    return cases


def main() -> None:
    resamples = 199
    fixtures = []
    for case in build_cases():
        entries = case["entries"]
        included = [
            item
            for item in entries
            if item["weight"] > 0 and item["family_weight"] > 0
        ]
        removed = sorted(
            item["item_key"]
            for item in entries
            if item["weight"] <= 0 or item["family_weight"] <= 0
        )
        theta = theta_of(included)
        count = len(included)
        if count <= EXACT_ENUMERATION_LIMIT:
            p_value = exact_sign_flip(included)
            mode = "exact_enumeration"
        else:
            p_value = monte_carlo_sign_flip(
                included, f"oracle:{case['name']}", resamples,
            )
            mode = "monte_carlo"
        interval = stratified_bootstrap(
            included, f"oracle:{case['name']}", resamples,
        )
        families = sorted({item["family"] for item in included})
        family_sizes = {
            family: sum(1 for item in included if item["family"] == family)
            for family in families
        }
        fixtures.append({
            "name": case["name"],
            "note": case["note"],
            "target_population": "declared dataset cases",
            "primary_estimand": "paired-difference-in-weighted-case-means",
            "entries": entries,
            "expected": {
                "count": count,
                "mean": float(theta) if theta is not None else None,
                "wins": sum(item["delta"] > 0 for item in included),
                "ties": sum(item["delta"] == 0 for item in included),
                "losses": sum(item["delta"] < 0 for item in included),
                "p_value": float(p_value) if p_value is not None else None,
                "sign_flip_mode": mode,
                "mcnemar_p_value": mcnemar(included),
                "removed_zero_weight_cases": removed,
                "families": families,
                "family_sizes": family_sizes,
                "small_families": sorted(
                    family
                    for family, size in family_sizes.items()
                    if size < 5
                ),
                "interval": interval,
            },
        })

    draw_entries = [
        entry(0, "alpha", 0.0, 1.0, weight=5.0),
        entry(1, "alpha", 0.0, 0.5),
        entry(2, "alpha", 0.5, 0.5),
        entry(3, "beta", 0.25, 0.75),
        entry(4, "beta", 0.75, 0.25, weight=2.0),
    ]
    draw_interval = stratified_bootstrap(
        draw_entries, "oracle:weighted_bootstrap_draws", 25,
        record_draws=True,
    )
    wilson_cases = {
        "all_success": wilson(10, 10),
        "all_failure": wilson(0, 10),
        "seven_of_ten": wilson(7, 10),
    }
    rng_probe = rng_for("oracle:rng-probe")
    rng_bounded = rng_for("oracle:rng-probe-bounded")
    document = {
        "fixture_version": FIXTURE_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "rng": RNG_NAME,
        "resamples": resamples,
        "fixtures": fixtures,
        "weighted_bootstrap_draws": {
            "entries": draw_entries,
            "seed_key": "oracle:weighted_bootstrap_draws",
            "resamples": 25,
            "expected": draw_interval,
        },
        "wilson": wilson_cases,
        "rng_reference": {
            "seed_key": "oracle:rng-probe",
            "raw_sequence": [rng_probe.raw() for _ in range(8)],
            "bounded_bound": 7,
            "bounded_sequence": [rng_bounded.bounded(7) for _ in range(16)],
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT} with {len(fixtures)} fixtures")


if __name__ == "__main__":
    main()
