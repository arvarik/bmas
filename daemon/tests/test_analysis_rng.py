"""The portable ``bmas-analysis-rng``: cross-language derivation.

The published fixtures pin candidates, rejections, and accepted
indexes at seed boundaries and non-power-of-two case counts. The
daemon reproduces every vector and every bootstrap draw byte for
byte, rejection counting follows the specification, and the
algorithm version lives in metadata, never in an identifier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks import analysis_rng
from benchmarks.analysis_rng import (
    RNG_ALGORITHM,
    RNG_ALGORITHM_VERSION,
    AnalysisRngError,
    candidate,
    draw,
    family_digest,
    rejection_limit,
    replicate_draws,
    sign_flip,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "analysis_rng.json").read_text(),
)
INPUT_DIGEST = bytes.fromhex(FIXTURES["input_digest"])


def test_fixture_pins_the_algorithm_identity():
    assert FIXTURES["algorithm"] == RNG_ALGORITHM
    assert FIXTURES["algorithm_version"] == RNG_ALGORITHM_VERSION
    # No version token lives inside the algorithm identifier.
    assert not any(character.isdigit() for character in RNG_ALGORITHM)


@pytest.mark.parametrize(
    "vector", FIXTURES["rng_vectors"],
    ids=[f"seed{v['seed'][:4]}-r{v['replicate_index']}-{v['family_id']}"
         f"-d{v['draw_index']}-n{v['case_count']}"
         for v in FIXTURES["rng_vectors"]],
)
def test_every_vector_matches_the_reference(vector):
    result = draw(
        master_seed=int(vector["seed"]),
        input_digest=INPUT_DIGEST,
        replicate_index=vector["replicate_index"],
        family_id=vector["family_id"],
        draw_index=vector["draw_index"],
        case_count=vector["case_count"],
    )
    assert [str(c) for c in result["candidates"]] == vector["candidates"]
    assert result["index"] == vector["index"]
    assert result["rejections"] == vector["rejections"]
    assert str(rejection_limit(vector["case_count"])) == vector["limit"]


def test_bootstrap_draws_match_the_oracle():
    oracle = FIXTURES["weighted_bootstrap_oracle"]
    for record in oracle["replicate_records"]:
        for family, expected in record["draws"].items():
            case_ids = sorted(oracle["cases"][family]["weights"])
            indexes = replicate_draws(
                master_seed=int(oracle["seed"]),
                input_digest=INPUT_DIGEST,
                replicate_index=record["replicate_index"],
                family_id=family,
                case_count=len(case_ids),
            )
            assert [case_ids[index] for index in indexes] == expected


def test_rejection_increments_the_counter_only_after_rejection(monkeypatch):
    first = candidate(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=0,
        family_id_digest=family_digest("algebra"), draw_index=0, counter=0,
    )
    # Force the first candidate to reject by lowering the limit.
    monkeypatch.setattr(
        analysis_rng, "rejection_limit", lambda count: first,
    )
    result = draw(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=0,
        family_id="algebra", draw_index=0, case_count=7,
    )
    assert result["rejections"] == 1
    assert result["candidates"][0] == first
    assert result["candidates"][1] < first
    assert result["index"] == result["candidates"][1] % 7


def test_seed_boundaries_and_non_power_of_two_counts():
    for seed in (0, 2**64 - 1):
        for count in (3, 5, 7, 13, 1000):
            result = draw(
                master_seed=seed, input_digest=INPUT_DIGEST,
                replicate_index=0, family_id="f", draw_index=0,
                case_count=count,
            )
            assert 0 <= result["index"] < count
    with pytest.raises(AnalysisRngError, match="64-bit"):
        candidate(
            master_seed=2**64, input_digest=INPUT_DIGEST,
            replicate_index=0, family_id_digest=family_digest("f"),
            draw_index=0, counter=0,
        )
    with pytest.raises(AnalysisRngError, match="at least one"):
        draw(
            master_seed=1, input_digest=INPUT_DIGEST, replicate_index=0,
            family_id="f", draw_index=0, case_count=0,
        )


def test_every_input_field_changes_the_candidate():
    base = {
        "master_seed": 7, "input_digest": INPUT_DIGEST,
        "replicate_index": 0, "family_id_digest": family_digest("algebra"),
        "draw_index": 0, "counter": 0,
    }
    reference = candidate(**base)
    for name, value in (
        ("master_seed", 8),
        ("input_digest", bytes(32)),
        ("replicate_index", 1),
        ("family_id_digest", family_digest("geometry")),
        ("draw_index", 1),
        ("counter", 1),
    ):
        assert candidate(**{**base, name: value}) != reference
    assert candidate(**base, algorithm_version=2) != reference


def test_sign_flip_bits_repeat_exactly():
    bits = [
        sign_flip(master_seed=3, input_digest=INPUT_DIGEST,
                  replicate_index=0, case_index=index)
        for index in range(32)
    ]
    again = [
        sign_flip(master_seed=3, input_digest=INPUT_DIGEST,
                  replicate_index=0, case_index=index)
        for index in range(32)
    ]
    assert bits == again
    assert len(set(bits)) == 2


def test_implementation_digest_is_stable():
    assert analysis_rng.implementation_digest() == (
        analysis_rng.implementation_digest()
    )
