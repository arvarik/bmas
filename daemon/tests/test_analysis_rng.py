"""The portable ``bmas-analysis-rng``: cross-language derivation.

The published fixtures pin candidates, rejections, accepted indexes,
and sign-flip bits at seed boundaries and non-power-of-two case
counts for every supported algorithm version. The daemon reproduces
every vector and every bootstrap draw byte for byte, rejection
counting follows the specification, and the algorithm version lives
in metadata, never in an identifier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks import analysis_rng
from benchmarks.analysis_rng import (
    RNG_ALGORITHM,
    RNG_ALGORITHM_VERSION,
    SUPPORTED_ALGORITHM_VERSIONS,
    AnalysisRngError,
    candidate,
    draw,
    family_digest,
    implementation_for,
    rejection_limit,
    replicate_draws,
    replicate_sign_flips,
    sign_flip,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "analysis_rng.json").read_text(),
)
INPUT_DIGEST = bytes.fromhex(FIXTURES["input_digest"])
VERSIONS = {int(entry["algorithm_version"]): entry
            for entry in FIXTURES["versions"]}


def _vector_cases() -> list[tuple[int, dict]]:
    return [
        (version, vector)
        for version, entry in sorted(VERSIONS.items())
        for vector in entry["rng_vectors"]
    ]


def test_fixture_pins_the_algorithm_identity():
    assert FIXTURES["algorithm"] == RNG_ALGORITHM
    assert sorted(VERSIONS) == list(SUPPORTED_ALGORITHM_VERSIONS)
    assert max(VERSIONS) == RNG_ALGORITHM_VERSION
    for version, entry in VERSIONS.items():
        assert entry["implementation"] == implementation_for(version)
    # No version token lives inside the algorithm identifier.
    assert not any(character.isdigit() for character in RNG_ALGORITHM)


@pytest.mark.parametrize(
    ("version", "vector"), _vector_cases(),
    ids=[f"v{version}-seed{v['seed'][:4]}-r{v['replicate_index']}"
         f"-{v['family_id']}-d{v['draw_index']}-n{v['case_count']}"
         for version, v in _vector_cases()],
)
def test_every_vector_matches_the_reference(version, vector):
    result = draw(
        master_seed=int(vector["seed"]),
        input_digest=INPUT_DIGEST,
        replicate_index=vector["replicate_index"],
        family_id=vector["family_id"],
        draw_index=vector["draw_index"],
        case_count=vector["case_count"],
        algorithm_version=version,
    )
    assert [str(c) for c in result["candidates"]] == vector["candidates"]
    assert result["index"] == vector["index"]
    assert result["rejections"] == vector["rejections"]
    assert str(rejection_limit(vector["case_count"], version)) == (
        vector["limit"]
    )


@pytest.mark.parametrize("version", sorted(VERSIONS))
def test_bootstrap_draws_match_the_oracle(version):
    oracle = VERSIONS[version]["weighted_bootstrap_oracle"]
    for record in oracle["replicate_records"]:
        for family, expected in record["draws"].items():
            case_ids = sorted(oracle["cases"][family]["weights"])
            indexes = replicate_draws(
                master_seed=int(oracle["seed"]),
                input_digest=INPUT_DIGEST,
                replicate_index=record["replicate_index"],
                family_id=family,
                case_count=len(case_ids),
                algorithm_version=version,
            )
            assert [case_ids[index] for index in indexes] == expected


@pytest.mark.parametrize("version", sorted(VERSIONS))
def test_sign_flip_bits_match_the_reference(version):
    for vector in VERSIONS[version]["sign_flip_vectors"]:
        bits = replicate_sign_flips(
            master_seed=int(vector["seed"]),
            input_digest=INPUT_DIGEST,
            replicate_index=vector["replicate_index"],
            case_count=vector["case_count"],
            algorithm_version=version,
        )
        assert bits == vector["flips"]
        for case_index, expected in enumerate(vector["flips"]):
            assert sign_flip(
                master_seed=int(vector["seed"]),
                input_digest=INPUT_DIGEST,
                replicate_index=vector["replicate_index"],
                case_index=case_index,
                algorithm_version=version,
            ) is expected


@pytest.mark.parametrize("version", sorted(VERSIONS))
def test_rejection_increments_the_counter_only_after_rejection(
    monkeypatch, version,
):
    first = candidate(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=0,
        family_id_digest=family_digest("algebra"), draw_index=0, counter=0,
        algorithm_version=version,
    )
    # Force the first candidate to reject by lowering the limit.
    monkeypatch.setattr(
        analysis_rng, "rejection_limit",
        lambda count, algorithm_version=version: first,
    )
    result = draw(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=0,
        family_id="algebra", draw_index=0, case_count=7,
        algorithm_version=version,
    )
    assert result["rejections"] >= 1
    assert result["candidates"][0] == first
    assert all(value >= first for value in result["candidates"][:-1])
    assert result["candidates"][-1] < first
    assert result["rejections"] == len(result["candidates"]) - 1
    assert result["index"] == result["candidates"][-1] % 7


def test_keyed_counter_pairs_share_one_word_and_retry_separately():
    """Even and odd draws read the two halves of one mixed word."""
    key = analysis_rng.family_key(
        master_seed=7, input_digest=INPUT_DIGEST,
        family_id_digest=family_digest("algebra"), algorithm_version=2,
    )
    mixed = analysis_rng.mix64(analysis_rng.counter_state(
        key, analysis_rng.bootstrap_word(3, 8), 0,
    ))
    even = candidate(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=3,
        family_id_digest=family_digest("algebra"), draw_index=8, counter=0,
        algorithm_version=2,
    )
    odd = candidate(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=3,
        family_id_digest=family_digest("algebra"), draw_index=9, counter=0,
        algorithm_version=2,
    )
    assert even == mixed >> 32
    assert odd == mixed & 0xFFFFFFFF
    retried = candidate(
        master_seed=7, input_digest=INPUT_DIGEST, replicate_index=3,
        family_id_digest=family_digest("algebra"), draw_index=9, counter=1,
        algorithm_version=2,
    )
    assert retried != odd
    assert rejection_limit(3, 2) == 2**32 - (2**32 % 3)


def test_seed_boundaries_and_non_power_of_two_counts():
    for version in SUPPORTED_ALGORITHM_VERSIONS:
        for seed in (0, 2**64 - 1):
            for count in (3, 5, 7, 13, 1000):
                result = draw(
                    master_seed=seed, input_digest=INPUT_DIGEST,
                    replicate_index=0, family_id="f", draw_index=0,
                    case_count=count, algorithm_version=version,
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
    with pytest.raises(AnalysisRngError, match="Unsupported"):
        draw(
            master_seed=1, input_digest=INPUT_DIGEST, replicate_index=0,
            family_id="f", draw_index=0, case_count=3, algorithm_version=3,
        )


@pytest.mark.parametrize("version", SUPPORTED_ALGORITHM_VERSIONS)
def test_every_input_field_changes_the_candidate(version):
    base = {
        "master_seed": 7, "input_digest": INPUT_DIGEST,
        "replicate_index": 0, "family_id_digest": family_digest("algebra"),
        "draw_index": 0, "counter": 0, "algorithm_version": version,
    }
    reference = candidate(**base)
    variations = {
        "master_seed": 8,
        "input_digest": bytes(32),
        "replicate_index": 1,
        "family_id_digest": family_digest("geometry"),
        "draw_index": 1,
        "counter": 1,
    }
    for field, value in variations.items():
        changed = candidate(**{**base, field: value})
        assert changed != reference, field


def test_sign_flips_use_the_reserved_family_and_stay_reproducible():
    for version in SUPPORTED_ALGORITHM_VERSIONS:
        first = [
            sign_flip(
                master_seed=7, input_digest=INPUT_DIGEST, replicate_index=2,
                case_index=index, algorithm_version=version,
            )
            for index in range(200)
        ]
        again = replicate_sign_flips(
            master_seed=7, input_digest=INPUT_DIGEST, replicate_index=2,
            case_count=200, algorithm_version=version,
        )
        assert first == again
        assert any(first) and not all(first)


def test_implementation_digest_pins_the_source():
    digest = analysis_rng.implementation_digest()
    assert len(digest) == 64
    assert digest == analysis_rng.implementation_digest()
