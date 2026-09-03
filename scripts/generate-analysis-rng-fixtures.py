#!/usr/bin/env python3
"""Generate the cross-language ``bmas-analysis-rng`` and bootstrap fixtures.

The script is an independent reference implementation: it imports
nothing from the daemon source tree and derives every value from the
published specification text. It publishes, for every supported
algorithm version, random candidates with their rejections and
accepted indexes at seed boundaries and non-power-of-two case counts,
sign-flip bits, and one weighted case bootstrap oracle with unequal
families, unequal case weights, missing slots, and duplicate draws.
The oracle freezes every replicate draw, every family aggregate, and
the combined estimate with exact rational arithmetic. Every supported
implementation must reproduce the file byte for byte.

Run from the repository root:

    python3 scripts/generate-analysis-rng-fixtures.py
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

FIXTURE_VERSION = "2"
ALGORITHM = "bmas-analysis-rng"
ALGORITHM_VERSIONS = (1, 2)
IMPLEMENTATIONS = {1: "sha-256-rejection", 2: "keyed-counter-splitmix64"}
WORD = 2**64
HALF_WORD = 2**32
MASK64 = WORD - 1
MASK32 = HALF_WORD - 1

GOLDEN_GAMMA = 0x9E3779B97F4A7C15
MIX_MULTIPLIER_ONE = 0xBF58476D1CE4E5B9
MIX_MULTIPLIER_TWO = 0x94D049BB133111EB
COUNTER_STEP = 0xD1B54A32D192ED03

OUTPUT = Path(__file__).resolve().parents[1] / (
    "daemon/tests/fixtures/analysis_rng.json"
)


# ── Version 1: SHA-256 rejection sampling ────────────────────────────


def sha_candidate(
    seed: int, input_digest: bytes, replicate: int,
    family: str, draw_index: int, counter: int,
) -> int:
    payload = (
        ALGORITHM.encode("utf-8") + b"\x00"
        + (1).to_bytes(4, "big")
        + seed.to_bytes(8, "big")
        + input_digest
        + replicate.to_bytes(4, "big")
        + hashlib.sha256(family.encode("utf-8")).digest()
        + draw_index.to_bytes(4, "big")
        + counter.to_bytes(4, "big")
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


# ── Version 2: the keyed counter under the SplitMix64 finalizer ──────


def family_key(seed: int, input_digest: bytes, family: str) -> int:
    payload = (
        ALGORITHM.encode("utf-8") + b"\x00"
        + (2).to_bytes(4, "big")
        + seed.to_bytes(8, "big")
        + input_digest
        + hashlib.sha256(family.encode("utf-8")).digest()
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def mix64(state: int) -> int:
    z = state & MASK64
    z = ((z ^ (z >> 30)) * MIX_MULTIPLIER_ONE) & MASK64
    z = ((z ^ (z >> 27)) * MIX_MULTIPLIER_TWO) & MASK64
    return z ^ (z >> 31)


def keyed_candidate(
    seed: int, input_digest: bytes, replicate: int,
    family: str, draw_index: int, counter: int,
) -> int:
    key = family_key(seed, input_digest, family)
    word = (replicate << 32) | (draw_index >> 1)
    state = (key + word * GOLDEN_GAMMA + counter * COUNTER_STEP) & MASK64
    mixed = mix64(state)
    return (mixed >> 32) if draw_index % 2 == 0 else (mixed & MASK32)


def keyed_sign_flip(
    seed: int, input_digest: bytes, replicate: int, case_index: int,
) -> bool:
    key = family_key(seed, input_digest, "sign-flip")
    word = (replicate << 32) | (case_index >> 6)
    mixed = mix64((key + word * GOLDEN_GAMMA) & MASK64)
    return bool((mixed >> (case_index & 63)) & 1)


# ── Shared rejection sampling ────────────────────────────────────────


def candidate_width(version: int) -> int:
    return WORD if version == 1 else HALF_WORD


def rejection_limit(version: int, count: int) -> int:
    width = candidate_width(version)
    return width - (width % count)


def reference_candidate(
    version: int, seed: int, input_digest: bytes, replicate: int,
    family: str, draw_index: int, counter: int,
) -> int:
    if version == 1:
        return sha_candidate(
            seed, input_digest, replicate, family, draw_index, counter,
        )
    return keyed_candidate(
        seed, input_digest, replicate, family, draw_index, counter,
    )


def reference_draw(
    version: int, seed: int, input_digest: bytes, replicate: int,
    family: str, draw_index: int, count: int,
) -> dict:
    limit = rejection_limit(version, count)
    counter = 0
    candidates = []
    while True:
        value = reference_candidate(
            version, seed, input_digest, replicate, family, draw_index,
            counter,
        )
        candidates.append(value)
        if value < limit:
            return {
                "index": value % count,
                "candidates": [str(c) for c in candidates],
                "rejections": counter,
            }
        counter += 1


def reference_sign_flip(
    version: int, seed: int, input_digest: bytes, replicate: int,
    case_index: int,
) -> bool:
    if version == 1:
        return bool(reference_draw(
            version, seed, input_digest, replicate, "sign-flip", case_index,
            2,
        )["index"])
    return keyed_sign_flip(seed, input_digest, replicate, case_index)


def rng_vectors(version: int, input_digest: bytes) -> list[dict]:
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
        (7, 3, "algebra", 65, 3),
    ):
        drawn = reference_draw(
            version, seed, input_digest, replicate, family, draw_index,
            count,
        )
        vectors.append({
            "seed": str(seed),
            "replicate_index": replicate,
            "family_id": family,
            "draw_index": draw_index,
            "case_count": count,
            "limit": str(rejection_limit(version, count)),
            **drawn,
        })
    return vectors


def sign_flip_vectors(version: int, input_digest: bytes) -> list[dict]:
    vectors = []
    for seed, replicate, case_count in (
        (7, 0, 5), (7, 1, 70), (0, 3, 130), (2**64 - 1, 2, 64),
    ):
        vectors.append({
            "seed": str(seed),
            "replicate_index": replicate,
            "case_count": case_count,
            "flips": [
                reference_sign_flip(
                    version, seed, input_digest, replicate, case_index,
                )
                for case_index in range(case_count)
            ],
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


def oracle(version: int, input_digest: bytes, seed: int, replicates: int) -> dict:
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
                    version, seed, input_digest, replicate, family,
                    draw_index, len(case_ids),
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
        "input_digest": input_digest.hex(),
        "versions": [
            {
                "algorithm_version": version,
                "implementation": IMPLEMENTATIONS[version],
                "candidate_width": str(candidate_width(version)),
                "rng_vectors": rng_vectors(version, input_digest),
                "sign_flip_vectors": sign_flip_vectors(version, input_digest),
                "weighted_bootstrap_oracle": oracle(
                    version, input_digest, 7, 6,
                ),
            }
            for version in ALGORITHM_VERSIONS
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
