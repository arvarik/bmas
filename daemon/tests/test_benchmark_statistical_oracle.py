"""Compare the statistical engine against the versioned oracle fixtures.

The fixtures live in ``tests/fixtures/statistical_oracle.json``. An
independent reference implementation generated them from first
principles: exact rational arithmetic for estimates and enumerated
tests, and a separate implementation of the published
``bmas-analysis-rng`` specification for every sampled draw. The tests
here assert exact structural values and use tolerances only for
floating-point differences.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.analysis import (
    ANALYSIS_RNG,
    AnalysisRandom,
    analysis_rng,
    build_run_report,
    case_outcomes,
    mcnemar_exact,
    outcome_slots,
    paired_case_comparison,
    wilson_interval,
)
from benchmarks.repository import BenchmarkConflict, _frozen_estimand

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/statistical_oracle.json").read_text(),
)
TOLERANCE = 1e-9


def _cases(entries: list[dict], side: str) -> dict[str, dict]:
    return {
        entry["item_key"]: {
            "item_key": entry["item_key"],
            "family": entry["family"],
            "fractional": float(entry[side]),
            "binary": bool(entry[f"{side}_binary"]),
        }
        for entry in entries
    }


def _estimand(entries: list[dict]) -> dict:
    return {
        "family_weights": {
            entry["family"]: entry["family_weight"] for entry in entries
        },
        "case_weights": {
            entry["item_key"]: entry["weight"] for entry in entries
        },
        "binary_reduction": "strict_majority",
        "min_family_cases": 5,
    }


def _close(actual, expected) -> None:
    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected, abs=TOLERANCE)


@pytest.mark.parametrize(
    "fixture",
    FIXTURES["fixtures"],
    ids=[fixture["name"] for fixture in FIXTURES["fixtures"]],
)
def test_the_engine_matches_the_independent_reference(fixture):
    entries = fixture["entries"]
    result = paired_case_comparison(
        _cases(entries, "left"),
        _cases(entries, "right"),
        estimand=_estimand(entries),
        seed_key=f"oracle:{fixture['name']}",
        practical_difference=0.01,
        bootstrap_resamples=FIXTURES["resamples"],
        sign_flip_resamples=FIXTURES["resamples"],
    )
    expected = fixture["expected"]
    assert result["count"] == expected["count"]
    assert result["wins"] == expected["wins"]
    assert result["ties"] == expected["ties"]
    assert result["losses"] == expected["losses"]
    _close(result["mean"], expected["mean"])
    _close(result["p_value_raw"], expected["p_value"])
    assert result["sign_flip"]["mode"] == expected["sign_flip_mode"]
    _close(result["mcnemar"]["p_value"], expected["mcnemar_p_value"])
    assert result["removed_zero_weight_cases"] == (
        expected["removed_zero_weight_cases"]
    )
    # The cluster and resampling order is the sorted family list.
    assert result["families"] == expected["families"]
    assert result["small_families"] == expected["small_families"]
    interval = expected["interval"]
    assert result["interval_status"] == interval["interval_status"]
    _close(result["ci_low"], interval["ci_low"])
    _close(result["ci_high"], interval["ci_high"])
    _close(result["standard_error"], interval["standard_error"])
    assert result["rng"] == FIXTURES["rng"] == ANALYSIS_RNG
    assert result["unit"] == "case"


def test_the_bootstrap_stores_and_reproduces_every_draw():
    block = FIXTURES["weighted_bootstrap_draws"]
    entries = block["entries"]
    result = paired_case_comparison(
        _cases(entries, "left"),
        _cases(entries, "right"),
        estimand=_estimand(entries),
        seed_key=block["seed_key"],
        practical_difference=0.01,
        bootstrap_resamples=block["resamples"],
        sign_flip_resamples=block["resamples"],
        record_draws=True,
    )
    assert result["bootstrap_draws"] == block["expected"]["draws"]
    _close(result["ci_low"], block["expected"]["ci_low"])
    _close(result["ci_high"], block["expected"]["ci_high"])


def test_case_weights_never_change_the_draw_probabilities():
    block = FIXTURES["weighted_bootstrap_draws"]
    entries = block["entries"]
    weighted = paired_case_comparison(
        _cases(entries, "left"),
        _cases(entries, "right"),
        estimand=_estimand(entries),
        seed_key=block["seed_key"],
        practical_difference=0.01,
        bootstrap_resamples=block["resamples"],
        sign_flip_resamples=block["resamples"],
        record_draws=True,
    )
    unweighted = paired_case_comparison(
        _cases(entries, "left"),
        _cases(entries, "right"),
        estimand={**_estimand(entries), "case_weights": {}},
        seed_key=block["seed_key"],
        practical_difference=0.01,
        bootstrap_resamples=block["resamples"],
        sign_flip_resamples=block["resamples"],
        record_draws=True,
    )
    # Identical draws under different weight vectors prove that the
    # algorithm draws cases uniformly and applies each declared case
    # weight exactly once, in the aggregation.
    assert weighted["bootstrap_draws"] == unweighted["bootstrap_draws"]
    assert weighted["mean"] != unweighted["mean"]


def test_the_stored_draws_are_uniform_within_each_family():
    block = FIXTURES["weighted_bootstrap_draws"]
    draws = block["expected"]["draws"]
    counts: dict[str, int] = {}
    for replicate in draws:
        for key in replicate:
            counts[key] = counts.get(key, 0) + 1
    weights = {
        entry["item_key"]: entry["weight"] for entry in block["entries"]
    }
    # The heavy case (weight 5) draws no more often than its uniform
    # share allows; every case appears.
    alpha_counts = [counts[f"case-{i:02d}"] for i in range(3)]
    assert all(count > 0 for count in alpha_counts)
    assert max(alpha_counts) <= 3 * min(alpha_counts)
    assert weights["case-00"] == 5.0


def test_wilson_intervals_stay_defined_at_the_boundaries():
    expected = FIXTURES["wilson"]
    for name, successes, total in (
        ("all_success", 10, 10),
        ("all_failure", 0, 10),
        ("seven_of_ten", 7, 10),
    ):
        interval = wilson_interval(successes, total)
        _close(interval["rate"], expected[name]["rate"])
        _close(interval["ci_low"], expected[name]["ci_low"])
        _close(interval["ci_high"], expected[name]["ci_high"])
        # The label marks an unclustered diagnostic; it never enters a
        # primary gate.
        assert interval["label"] == "unclustered_slot_diagnostic"
    assert wilson_interval(10, 10)["ci_low"] > 0.6
    assert wilson_interval(0, 10)["ci_high"] < 0.4


def test_the_named_generator_matches_the_reference_sequences():
    reference = FIXTURES["rng_reference"]
    rng = analysis_rng(reference["seed_key"])
    assert [rng.next_raw() for _ in range(8)] == reference["raw_sequence"]
    bounded = analysis_rng(f"{reference['seed_key']}-bounded")
    assert [
        bounded.uniform_index(reference["bounded_bound"])
        for _ in range(16)
    ] == reference["bounded_sequence"]


def test_the_generator_behaves_at_seed_and_bound_boundaries():
    zero = AnalysisRandom(0)
    wrapped = AnalysisRandom(2**64)
    assert zero.next_raw() == wrapped.next_raw()
    top = AnalysisRandom(2**64 - 1)
    assert 0 <= top.next_raw() <= 2**64 - 1
    with pytest.raises(ValueError):
        AnalysisRandom(1).uniform_index(0)
    # A non-power-of-two bound stays unbiased through rejection and
    # covers the full range.
    rng = AnalysisRandom(12345)
    draws = {rng.uniform_index(7) for _ in range(400)}
    assert draws == set(range(7))


def test_mcnemar_requires_the_predeclared_binary_reduction():
    with pytest.raises(ValueError, match="binary"):
        mcnemar_exact([0.5, 1.0], [1.0, 0.0])
    result = mcnemar_exact([True, True, False], [False, False, False])
    assert result["reduction"] == "predeclared_binary"
    assert result["p_value"] == pytest.approx(0.5)


def test_zero_sum_weight_vectors_reject_before_admission():
    items = [
        {"id": "item-a", "item_key": "a", "subject": "one"},
        {"id": "item-b", "item_key": "b", "subject": "one"},
    ]
    with pytest.raises(BenchmarkConflict, match="zero"):
        _frozen_estimand(
            {"statistics": {"case_weights": {"a": 0, "b": 0}}},
            items=items,
        )
    with pytest.raises(BenchmarkConflict, match="zero"):
        _frozen_estimand(
            {"statistics": {"family_weights": {"one": 0}}},
            items=items,
        )
    frozen = _frozen_estimand(
        {"statistics": {"binary_reduction": "at_least_k", "at_least_k": 2}},
        items=items,
    )
    assert frozen["binary_reduction"] == "at_least_k"
    assert frozen["at_least_k"] == 2
    with pytest.raises(BenchmarkConflict):
        _frozen_estimand(
            {"statistics": {"binary_reduction": "at_least_k"}},
            items=items,
        )


def _slot_attempt(
    identifier: str,
    item: str,
    repeat: int,
    retry: int,
    status: str = "completed",
    category: str | None = None,
) -> dict:
    return {
        "id": identifier,
        "dataset_item_id": item,
        "item_key": item,
        "subject": "math",
        "repeat_index": repeat,
        "retry_index": retry,
        "status": status,
        "failure_category": category,
    }


def test_slots_seal_with_the_first_valid_substantive_outcome():
    slots = outcome_slots(
        [
            # An excluded infrastructure failure never seals; the
            # retry fills the slot inside its original position.
            _slot_attempt("a-infra", "one", 1, 0, "failed",
                          "infrastructure"),
            _slot_attempt("a-retry", "one", 1, 1),
            # A substantive failure seals; the prohibited retry cannot
            # replace it.
            _slot_attempt("b-fail", "two", 1, 0, "failed", "execution"),
            _slot_attempt("b-late", "two", 1, 1),
        ],
        excluded_categories={"infrastructure"},
    )
    infrastructure_slot = slots[("one", 1)]
    assert infrastructure_slot["sealed"]["id"] == "a-retry"
    assert infrastructure_slot["retry_attempts"] == 1
    sealed_slot = slots[("two", 1)]
    assert sealed_slot["sealed"]["id"] == "b-fail"
    assert sealed_slot["post_seal_attempts"] == 1


def test_repetitions_nest_inside_each_case_with_declared_reductions():
    attempts = [
        _slot_attempt(f"case-one-{repeat}", "one", repeat, 0)
        for repeat in (1, 2, 3)
    ]
    scores = {
        "case-one-1": [{"scorer_id": "exact", "status": "scored",
                        "score": 1.0, "passed": 1}],
        "case-one-2": [{"scorer_id": "exact", "status": "scored",
                        "score": 1.0, "passed": 1}],
        "case-one-3": [{"scorer_id": "exact", "status": "scored",
                        "score": 0.0, "passed": 0}],
    }
    slots = outcome_slots(attempts, excluded_categories=set())
    strict = case_outcomes(
        slots, scores, "exact",
        {"binary_reduction": "strict_majority", "family_field": "subject"},
    )
    assert strict["one"]["slot_values"] == [1.0, 1.0, 0.0]
    assert strict["one"]["fractional"] == pytest.approx(2 / 3)
    assert strict["one"]["binary"] is True
    everything = case_outcomes(
        slots, scores, "exact",
        {"binary_reduction": "all", "family_field": "subject"},
    )
    assert everything["one"]["binary"] is False
    at_least = case_outcomes(
        slots, scores, "exact",
        {"binary_reduction": "at_least_k", "at_least_k": 2,
         "family_field": "subject"},
    )
    assert at_least["one"]["binary"] is True


def _report_run(attempts: list[dict], scores: list[dict]) -> dict:
    for attempt in attempts:
        attempt.setdefault("trial_id", f"trial-{attempt['arm_id']}")
        attempt.setdefault("split", "test")
        attempt.setdefault("tags", [])
        attempt.setdefault("total_cost_usd", 0.01)
        attempt.setdefault("total_tokens", 10)
        attempt.setdefault("duration_ms", 100)
        attempt.setdefault("task_id", f"task-{attempt['id']}")
        attempt.setdefault("snapshot_checksum", "checksum")
    return {
        "id": "run-oracle",
        "status": "completed",
        "test_id": "test-oracle",
        "test_revision_id": "revision-oracle",
        "test_configuration_checksum": "checksum",
        "test_configuration": {"practical_difference": 0.01,
                               "repetitions": 1},
        "dataset_id": "dataset-oracle",
        "dataset_checksum": "dataset-checksum",
        "execution_plan_checksum": "plan-checksum",
        "attempts": attempts,
        "scores": scores,
        "human_reviews": [],
    }


def _arm_attempt(arm: str, item: str, **overrides) -> dict:
    attempt = {
        "id": f"{arm}-{item}",
        "arm_id": arm,
        "arm_name": arm.title(),
        "arm_slug": arm,
        "runtime_id": "classic",
        "dataset_item_id": item,
        "item_key": item,
        "subject": overrides.pop("subject", "math"),
        "repeat_index": 1,
        "retry_index": 0,
        "status": "completed",
        "failure_category": None,
    }
    attempt.update(overrides)
    return attempt


def _score(attempt_id: str, value: float) -> dict:
    return {
        "id": f"score-{attempt_id}",
        "attempt_id": attempt_id,
        "scorer_id": "exact",
        "scorer_name": "Exact",
        "scorer_version": "1",
        "status": "scored",
        "score": value,
        "passed": int(value >= 0.5),
    }


def test_a_missing_arm_case_leaves_the_pairing_not_the_report():
    attempts = [
        _arm_attempt("left", "one"),
        _arm_attempt("left", "two"),
        _arm_attempt("right", "one"),
    ]
    scores = [
        _score("left-one", 0.0),
        _score("left-two", 1.0),
        _score("right-one", 1.0),
    ]
    report = build_run_report(_report_run(attempts, scores))
    metric = report["comparisons"][0]["scorers"][0]
    # Only the case present in both arms pairs; the unpaired case
    # stays in its own arm's metrics.
    assert metric["count"] == 1
    assert metric["paired_cases"] == ["one"]
    left_arm = next(
        arm for arm in report["arms"] if arm["arm_slug"] == "left"
    )
    assert left_arm["scorers"][0]["count"] == 2


def test_undefined_kappa_returns_none_beside_raw_agreement():
    attempts = [
        _arm_attempt("left", "one"),
        _arm_attempt("left", "two"),
    ]
    scores = [
        _score("left-one", 1.0),
        _score("left-two", 1.0),
    ]
    run = _report_run(attempts, scores)
    run["human_reviews"] = [
        {"id": "review-1", "attempt_id": "left-one", "reviewer_id": "r1",
         "score": 1.0, "passed": 1},
        {"id": "review-2", "attempt_id": "left-two", "reviewer_id": "r1",
         "score": 1.0, "passed": 1},
    ]
    report = build_run_report(run)
    calibration = report["diagnostics"]["human_calibration"][0]
    # Every judgment agrees on one class: agreement is defined, kappa
    # is not, and the undefined kappa stays None instead of a number.
    assert calibration["agreement_rate"] == 1.0
    assert calibration["cohen_kappa"] is None


def test_one_pass_and_one_fail_review_ties_until_adjudication():
    attempts = [_arm_attempt("left", "one")]
    scores = [_score("left-one", 1.0)]
    run = _report_run(attempts, scores)
    run["human_reviews"] = [
        {"id": "review-1", "attempt_id": "left-one", "reviewer_id": "r1",
         "score": 1.0, "passed": 1},
        {"id": "review-2", "attempt_id": "left-one", "reviewer_id": "r2",
         "score": 0.0, "passed": 0},
    ]
    tied = build_run_report(run)
    state = tied["diagnostics"]["review_states"][0]
    assert state["state"] == "tie"
    assert state["adjudicated"] is False
    assert tied["diagnostics"]["human_review"]["reviewed_attempt_count"] == 0
    run["human_reviews"].append(
        {"id": "review-3", "attempt_id": "left-one", "reviewer_id": "r3",
         "score": 1.0, "passed": 1},
    )
    resolved = build_run_report(run)
    state = resolved["diagnostics"]["review_states"][0]
    assert state["state"] == "passed"
    assert state["adjudicated"] is True


def test_holm_correction_spans_every_comparison_family():
    attempts = []
    scores = []
    for index in range(6):
        for arm, value in (("left", 0.0), ("middle", 1.0), ("right", 1.0)):
            item = f"item-{index}"
            attempt = _arm_attempt(arm, item)
            attempts.append(attempt)
            scores.append(_score(attempt["id"], value))
    report = build_run_report(_report_run(attempts, scores))
    metrics = [
        scorer
        for comparison in report["comparisons"]
        for scorer in comparison["scorers"]
    ]
    assert len(metrics) == 3
    raw = [metric["p_value_raw"] for metric in metrics]
    adjusted = [metric["p_value_adjusted"] for metric in metrics]
    # Holm adjusts across the whole family: no adjusted value falls
    # below its raw value, and the identical null comparison stays 1.
    for raw_value, adjusted_value in zip(raw, adjusted, strict=True):
        assert adjusted_value >= raw_value
    identical = next(
        metric
        for comparison, metric in [
            (comparison, scorer)
            for comparison in report["comparisons"]
            for scorer in comparison["scorers"]
        ]
        if metric["ties"] == 6
    )
    assert identical["p_value_adjusted"] == 1.0
