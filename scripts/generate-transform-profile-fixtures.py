#!/usr/bin/env python3
"""Generate the versioned cross-language transform profile fixtures.

The script is an independent reference for the published
``bmas-transform`` rules: it imports nothing from the daemon source
tree, implements the SHA-256 counter ranking, the stable ordering,
sampling, and split assignment from the specification text, and takes
the number-rendering vectors from RFC 8785 itself. Every supported
implementation must reproduce these fixtures byte for byte; the
daemon test suite is the first consumer.

Run from the repository root:

    python3 scripts/generate-transform-profile-fixtures.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIXTURE_VERSION = "2"
PROFILE_NAME = "bmas-transform"
PROFILE_VERSION = 1

OUTPUT = Path(__file__).resolve().parents[1] / (
    "daemon/tests/fixtures/transform_profile.json"
)

# Number vectors: inputs are binary64 values written as JSON, expected
# strings follow the ECMAScript serialization RFC 8785 requires.
NUMBER_VECTORS = [
    {"value": 0.5, "expected": "0.5"},
    {"value": -0.0, "expected": "0"},
    {"value": 5.0, "expected": "5"},
    {"value": 1e21, "expected": "1e+21"},
    {"value": 1e-7, "expected": "1e-7"},
    {"value": 1e16, "expected": "10000000000000000"},
    {"value": 0.000001, "expected": "0.000001"},
    {"value": 333333333.3333333, "expected": "333333333.3333333"},
    {"value": 9007199254740991, "expected": "9007199254740991"},
]


def reference_case_digest(case: dict) -> bytes:
    """Digest one simple case with an independent canonical form.

    The fixture cases restrict themselves to strings and safe
    integers, where sorted-key compact JSON equals the RFC 8785
    canonical form.
    """
    canonical = json.dumps(
        case, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def reference_rank(
    seed: int, operation_index: int, digest: bytes, counter: int,
) -> bytes:
    payload = (
        PROFILE_NAME.encode("utf-8")
        + b"\x00"
        + PROFILE_VERSION.to_bytes(4, "big")
        + seed.to_bytes(8, "big")
        + operation_index.to_bytes(4, "big")
        + digest
        + counter.to_bytes(4, "big")
    )
    return hashlib.sha256(payload).digest()


_CASE_NAMES = (
    "alpha", "bravo", "charlie", "delta",
    "echo", "foxtrot", "golf", "hotel",
)


def fixture_cases() -> list[dict]:
    return [
        {"case_id": f"case-{name}", "input": f"Question {name}",
         "expected_output": "42", "weight": index}
        for index, name in enumerate(_CASE_NAMES)
    ]


def sample_selection(cases: list[dict], seed: int, count: int) -> list[str]:
    ranked = []
    for ordinal, case in enumerate(cases):
        rank = reference_rank(
            seed, 0, reference_case_digest(case), 0,
        )
        ranked.append((rank, (case["case_id"].encode(), ordinal), case))
    chosen = sorted(ranked, key=lambda entry: (entry[0], entry[1]))[:count]
    selected = {entry[2]["case_id"] for entry in chosen}
    return [case["case_id"] for case in cases if case["case_id"] in selected]


def split_assignment(
    cases: list[dict], seed: int, weights: dict[str, int],
) -> dict[str, str]:
    names = sorted(weights)
    total = sum(weights[name] for name in names)
    boundaries = []
    running = 0
    for name in names:
        running += weights[name]
        boundaries.append((running, name))
    assignment = {}
    for case in cases:
        rank = reference_rank(seed, 1, reference_case_digest(case), 0)
        remainder = int.from_bytes(rank, "big") % total
        assignment[case["case_id"]] = next(
            name for boundary, name in boundaries if remainder < boundary
        )
    return assignment


def main() -> None:
    cases = fixture_cases()
    weights = {"test": 1, "train": 2}
    document = {
        "fixture_version": FIXTURE_VERSION,
        "profile": PROFILE_NAME,
        "profile_version": PROFILE_VERSION,
        "numbers": NUMBER_VECTORS,
        "cases": cases,
        "case_digests": [
            reference_case_digest(case).hex() for case in cases
        ],
        "rank_vectors": [
            {
                "seed": str(seed),
                "operation_index": index,
                "case_id": case["case_id"],
                "counter": counter,
                "rank": reference_rank(
                    seed, index, reference_case_digest(case), counter,
                ).hex(),
            }
            for seed, index, case, counter in (
                (0, 0, cases[0], 0),
                (7, 0, cases[0], 0),
                (7, 1, cases[0], 0),
                (7, 0, cases[1], 0),
                (7, 0, cases[0], 3),
                (2**64 - 1, 0, cases[0], 0),
            )
        ],
        "sample": {
            "seed": "7",
            "count": 3,
            "selected_case_ids": sample_selection(cases, 7, 3),
        },
        "split": {
            "seed": "7",
            "weights": weights,
            "assignment": split_assignment(cases, 7, weights),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
