#!/usr/bin/env python3
"""Generate the cross-language ``bmas-analysis-rng`` and bootstrap fixtures.

The script is an independent reference implementation: it imports
nothing from the daemon source tree and derives every value from the
published specification text. It publishes random candidates with
their rejections and accepted indexes at seed boundaries and
non-power-of-two case counts, and one weighted case bootstrap oracle
with unequal families, unequal case weights, missing slots, and
duplicate draws. The oracle freezes every replicate draw, every
family aggregate, and the combined estimate with exact rational
arithmetic. Every supported implementation must reproduce the file
byte for byte.

Run from the repository root:

    python3 scripts/generate-analysis-rng-fixtures.py
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

FIXTURE_VERSION = "1"
ALGORITHM = "bmas-analysis-rng"
ALGORITHM_VERSION = 1
WORD = 2**64

OUTPUT = Path(__file__).resolve().parents[1] / (
    "daemon/tests/fixtures/analysis_rng.json"
)


def reference_candidate(
    seed: int, input_digest: bytes, replicate: int,
    family: str, draw_index: int, counter: int,
) -> int:
    payload = (
        ALGORITHM.encode("utf-8") + b"\x00"
        + ALGORITHM_VERSION.to_bytes(4, "big")
        + seed.to_bytes(8, "big")
        + input_digest
        + replicate.to_bytes(4, "big")
        + hashlib.sha256(family.encode("utf-8")).digest()
        + draw_index.to_bytes(4, "big")
        + counter.to_bytes(4, "big")
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def reference_draw(
    seed: int, input_digest: bytes, replicate: int,
    family: str, draw_index: int, count: int,
) -> dict:
    limit = WORD - (WORD % count)
    counter = 0
    candidates = []
    while True:
        value = reference_candidate(
            seed, input_digest, replicate, family, draw_index, counter,
        )
        candidates.append(value)
        if value < limit:
            return {
                "index": value % count,
                "candidates": [str(c) for c in candidates],
                "rejections": counter,
            }
        counter += 1


def rng_vectors(input_digest: bytes) -> list[dict]:
    vectors = []
    for seed, replicate, family, draw_index, count in (
        (0, 0, "algebra", 0, 7),
        (7, 0, "algebra", 0, 7),
        (7, 1, "algebra", 0, 7),
        (7, 0, "geometry", 0, 7),
        (7, 0, "algebra", 3, 7),
        (7, 0, "algebra", 0, 5),
        (7, 0, "algebra", 0, 13),
        (2**64 - 1, 0, "algebra", 0, 7),
        (2**64 - 1, 4, "geometry", 2, 3),
    ):
        drawn = reference_draw(
            seed, input_digest, replicate, family, draw_index, count,
        )
        vectors.append({
            "seed": str(seed),
            "replicate_index": replicate,
            "family_id": family,
            "draw_index": draw_index,
            "case_count": count,
            "limit": str(WORD - (WORD % count)),
            **drawn,
        })
    return vectors


# ── The weighted case bootstrap oracle ───────────────────────────────


def oracle_cases() -> dict:
    """Two unequal families with unequal case weights and missing slots.

    Each case carries paired slot values for the left and right arm.
    ``None`` marks an infrastructure-missing slot; the paired slot
    leaves both arms before case reduction.
    """
    return {
        "algebra": {
            "weights": {"a1": 1, "a2": 2, "a3": 1, "a4": 3, "a5": 1},
            "slots": {
                "a1": [(0, 1), (1, 1)],
                "a2": [(0, 0), (0, 1)],
                "a3": [(1, 1), (None, 0)],
                "a4": [(0, 1), (0, 1)],
                "a5": [(1, 0), (1, 0)],
            },
        },
        "geometry": {
            "weights": {"g1": 1, "g2": 1, "g3": 2, "g4": 1, "g5": 1,
                        "g6": 1, "g7": 4},
            "slots": {
                "g1": [(0, 1), (0, 0)],
                "g2": [(1, 1), (1, 1)],
                "g3": [(0, 0), (1, None)],
                "g4": [(0, 1), (0, 1)],
                "g5": [(1, 0), (0, 0)],
                "g6": [(0, 1), (1, 1)],
                "g7": [(0, 0), (0, 1)],
            },
        },
    }


FAMILY_WEIGHTS = {"algebra": Fraction(2), "geometry": Fraction(1)}


def reduce_case(slots: list[tuple]) -> tuple[Fraction, int]:
    """Mean paired delta over slots where both arms observed."""
    usable = [(left, right) for left, right in slots
              if left is not None and right is not None]
    if not usable:
        raise ValueError("The oracle keeps every case usable")
    delta = sum(Fraction(right - left) for left, right in usable)
    return delta / len(usable), len(usable)


def family_aggregate(members: list[tuple[str, Fraction, Fraction]]) -> Fraction:
    numerator = sum(weight * delta for _, weight, delta in members)
    denominator = sum(weight for _, weight, _ in members)
    return numerator / denominator


def oracle(input_digest: bytes, seed: int, replicates: int) -> dict:
    cases = oracle_cases()
    families = sorted(cases)
    reduced = {
        family: {
            case_id: reduce_case(slots)
            for case_id, slots in sorted(cases[family]["slots"].items())
        }
        for family in families
    }
    total_family_weight = sum(FAMILY_WEIGHTS[f] for f in families)
    point_family = {}
    for family in families:
        members = [
            (case_id, Fraction(cases[family]["weights"][case_id]),
             reduced[family][case_id][0])
            for case_id in sorted(reduced[family])
        ]
        point_family[family] = family_aggregate(members)
    point = sum(
        FAMILY_WEIGHTS[f] * point_family[f] for f in families
    ) / total_family_weight
    replicate_records = []
    for replicate in range(replicates):
        aggregates = {}
        draws = {}
        for family in families:
            case_ids = sorted(reduced[family])
            drawn_ids = [
                case_ids[reference_draw(
                    seed, input_digest, replicate, family, draw_index,
                    len(case_ids),
                )["index"]]
                for draw_index in range(len(case_ids))
            ]
            draws[family] = drawn_ids
            members = [
                (case_id, Fraction(cases[family]["weights"][case_id]),
                 reduced[family][case_id][0])
                for case_id in drawn_ids
            ]
            aggregates[family] = family_aggregate(members)
        combined = sum(
            FAMILY_WEIGHTS[f] * aggregates[f] for f in families
        ) / total_family_weight
        replicate_records.append({
            "replicate_index": replicate,
            "draws": draws,
            "family_aggregates": {
                f: str(aggregates[f]) for f in families
            },
            "combined": str(combined),
        })
    return {
        "seed": str(seed),
        "replicates": replicates,
        "family_weights": {f: str(FAMILY_WEIGHTS[f]) for f in families},
        "cases": {
            family: {
                "weights": cases[family]["weights"],
                "slots": {
                    case_id: [list(pair) for pair in slots]
                    for case_id, slots in sorted(
                        cases[family]["slots"].items(),
                    )
                },
            }
            for family in families
        },
        "reduced_case_deltas": {
            family: {
                case_id: {"delta": str(value), "usable_slots": count}
                for case_id, (value, count) in sorted(reduced[family].items())
            }
            for family in families
        },
        "point_family_aggregates": {
            f: str(point_family[f]) for f in families
        },
        "point_estimate": str(point),
        "replicate_records": replicate_records,
    }


def main() -> None:
    input_digest = hashlib.sha256(b"bmas-analysis-rng-fixture-input").digest()
    document = {
        "fixture_version": FIXTURE_VERSION,
        "algorithm": ALGORITHM,
        "algorithm_version": ALGORITHM_VERSION,
        "input_digest": input_digest.hex(),
        "rng_vectors": rng_vectors(input_digest),
        "weighted_bootstrap_oracle": oracle(input_digest, 7, 6),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
