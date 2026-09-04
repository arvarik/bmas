"""The vectorized frozen-analysis engine equals the reference engine.

The vectorized engine and the reference engine compute equal
replicate estimates, intervals, sign-flip tests, and result digests
for every frozen specification, so the engine choice never changes
a number. The vectorized engine refuses the SHA-256 derivation of
algorithm version 1, the host self-check guards the sequential
accumulation claim, and the snapshot records the vector toolchain.
"""

from __future__ import annotations

import random

import pytest
from test_frozen_analysis import comparison, run_from_slots

from benchmarks import analysis_engine, frozen_analysis
from benchmarks.analysis_engine import ENGINE_REFERENCE, ENGINE_VECTORIZED
from benchmarks.frozen_analysis import (
    FrozenAnalysisError,
    compute_report,
    freeze_input,
    freeze_specification,
    pair_cases,
    resolve_engine,
    sign_flip_test,
    weighted_estimate,
)

pytestmark = pytest.mark.skipif(
    not analysis_engine.available(),
    reason="the vectorized engine needs NumPy",
)


def _build(
    *, count: int, replicates: int, families: int = 1, seed: int = 7,
    weighted: bool = False, repetitions: int = 1,
) -> tuple[dict, dict]:
    generator = random.Random(seed)
    slots: dict[str, dict[str, list]] = {}
    for family_index in range(families):
        cases = {}
        for case_index in range(count // families):
            cases[f"c{family_index}-{case_index}"] = [
                (generator.choice([0, 1]), generator.choice([0, 1]))
                for _ in range(generator.choice(range(1, repetitions + 1)))
            ]
        slots[f"f{family_index}"] = cases
    run, family_ids = run_from_slots(slots)
    overrides = {}
    if weighted:
        overrides["case_weights"] = {
            case_id: generator.choice([0.0, 0.5, 1.0, 2.0, 3.0])
            for ids in family_ids.values() for case_id in ids
        }
        overrides["family_weights"] = {
            family: generator.choice([1.0, 2.0, 5.0]) for family in family_ids
        }
    spec = freeze_specification(
        families=family_ids, scorer_id="exact", master_seed=seed,
        comparison_family={"family_id": "engine",
                           "comparisons": [comparison()]},
        resample_count=replicates, **overrides,
    )
    frozen = freeze_input(run, spec, planned_repetitions=repetitions)
    return spec, frozen


@pytest.mark.parametrize(
    ("count", "replicates", "families", "weighted"),
    [
        (30, 57, 1, False),
        (40, 101, 3, True),
        (13, 999, 1, False),
        (200, 250, 2, True),
        (64, 1, 2, True),
        (1000, 40, 4, True),
    ],
)
def test_both_engines_produce_equal_reports(
    count, replicates, families, weighted,
):
    spec, frozen = _build(
        count=count, replicates=replicates, families=families,
        seed=count + replicates, weighted=weighted, repetitions=2,
    )
    reference = compute_report(spec, frozen, engine=ENGINE_REFERENCE)
    vectorized = compute_report(spec, frozen, engine=ENGINE_VECTORIZED)
    assert vectorized["results_digest"] == reference["results_digest"]
    assert vectorized["comparisons"][0]["interval"] == (
        reference["comparisons"][0]["interval"]
    )
    assert vectorized["comparisons"][0]["test"] == (
        reference["comparisons"][0]["test"]
    )
    assert vectorized["comparisons"][0]["gate"] == (
        reference["comparisons"][0]["gate"]
    )


def test_auto_selection_prefers_the_vectorized_engine_for_version_two():
    spec, frozen = _build(count=20, replicates=30)
    assert resolve_engine(spec) == ENGINE_VECTORIZED
    assert resolve_engine(spec, record_draws=True) == ENGINE_REFERENCE
    legacy_spec, _ = _build(count=20, replicates=30)
    legacy_spec = freeze_specification(
        families={"f0": legacy_spec["case_ids"]["f0"]}, scorer_id="exact",
        master_seed=7, comparison_family=spec["comparison_family"],
        resample_count=30, algorithm_version=1,
    )
    assert resolve_engine(legacy_spec) == ENGINE_REFERENCE
    with pytest.raises(FrozenAnalysisError, match="never vectorizes"):
        resolve_engine(legacy_spec, requested=ENGINE_VECTORIZED)
    with pytest.raises(FrozenAnalysisError, match="Unknown analysis engine"):
        resolve_engine(spec, requested="abacus")
    assert compute_report(spec, frozen)["results_digest"] == (
        compute_report(spec, frozen, engine=ENGINE_REFERENCE)["results_digest"]
    )


def test_sign_flip_exceedances_match_the_reference_count():
    spec, frozen = _build(count=90, replicates=333, families=3, seed=11,
                          weighted=True, repetitions=3)
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    reference = sign_flip_test(
        spec, paired, frozen["input_digest"], engine=ENGINE_REFERENCE,
    )
    vectorized = sign_flip_test(
        spec, paired, frozen["input_digest"], engine=ENGINE_VECTORIZED,
    )
    assert reference["mode"] == "monte_carlo"
    assert vectorized == reference


def test_vectorized_draws_match_the_reference_draws():
    """The gathered indexes equal the scalar derivation, draw for draw."""
    from benchmarks import analysis_rng

    digest = bytes(range(32))
    key = analysis_rng.family_key(
        master_seed=99, input_digest=digest,
        family_id_digest=analysis_rng.family_digest("f"),
        algorithm_version=2,
    )
    for count in (1, 2, 3, 7, 64, 65, 1000):
        limit = analysis_rng.rejection_limit(count, 2)
        indexes = analysis_engine.draw_indexes(key, count, 5, 9, limit)
        assert indexes.shape == (count, 4)
        for column, replicate in enumerate(range(5, 9)):
            expected = analysis_rng.replicate_draws(
                master_seed=99, input_digest=digest,
                replicate_index=replicate, family_id="f", case_count=count,
                algorithm_version=2,
            )
            assert indexes[:, column].tolist() == expected


def test_vectorized_rejections_retry_with_the_counter(monkeypatch):
    """A forced low limit rejects candidates and the retries converge."""
    from benchmarks import analysis_rng

    digest = bytes(32)
    key = analysis_rng.family_key(
        master_seed=3, input_digest=digest,
        family_id_digest=analysis_rng.family_digest("f"),
        algorithm_version=2,
    )
    low_limit = 2**31
    indexes = analysis_engine.draw_indexes(key, 50, 0, 6, low_limit)
    scalar = []
    for replicate in range(6):
        column = []
        for draw_index in range(50):
            counter = 0
            while True:
                value = analysis_rng.candidate(
                    master_seed=3, input_digest=digest,
                    replicate_index=replicate,
                    family_id_digest=analysis_rng.family_digest("f"),
                    draw_index=draw_index, counter=counter,
                    algorithm_version=2,
                )
                if value < low_limit:
                    column.append(value % 50)
                    break
                counter += 1
        scalar.append(column)
    for column in range(6):
        assert indexes[:, column].tolist() == scalar[column]


def test_zero_weight_families_and_sparse_weights_stay_equal():
    """Zero case weights can zero a replicate; both engines skip alike."""
    run, families = run_from_slots({
        "f": {f"c{index}": [(0, 1 if index % 2 else 0)] for index in range(6)},
        "g": {f"g{index}": [(1, 0)] for index in range(6)},
    })
    weights = {f"c{index}": 0.0 for index in range(6)}
    weights["c1"] = 1.0
    weights.update({f"g{index}": 1.0 for index in range(6)})
    spec = freeze_specification(
        families=families, scorer_id="exact", master_seed=5,
        comparison_family={"family_id": "engine",
                           "comparisons": [comparison()]},
        resample_count=300, case_weights=weights, min_family_cases=1,
    )
    frozen = freeze_input(run, spec, planned_repetitions=1)
    reference = compute_report(spec, frozen, engine=ENGINE_REFERENCE)
    vectorized = compute_report(spec, frozen, engine=ENGINE_VECTORIZED)
    assert vectorized["results_digest"] == reference["results_digest"]
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    assert weighted_estimate(paired)["estimate"] is not None


def test_engine_digests_record_the_vector_toolchain():
    digests = frozen_analysis.engine_digests()
    versions = digests["toolchain_versions"]
    assert versions["statistics"] == "binary64-sequential-summation"
    assert versions["vector_engine"].startswith("numpy-")
    assert versions["engine"] == ENGINE_VECTORIZED
    assert analysis_engine.numpy_version() in versions["vector_engine"]


def test_sequential_accumulation_matches_the_reference_sum():
    values = [random.Random(1).uniform(-1e6, 1e6) for _ in range(5000)]
    total = 0.0
    for value in values:
        total += value
    assert analysis_engine.sequential_sum(values) == total
    assert analysis_engine.available() is True
