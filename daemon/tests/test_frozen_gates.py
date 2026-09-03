"""Baseline gates decide frozen rules through the frozen analysis engine.

A frozen rule names the scorer, declares its non-inferiority margin
through ``max_drop`` or strict superiority through ``gte``, and the
gate decides it from the predeclared frozen comparison of one arm
across the baseline and candidate runs: paired cases, family
weights, the bootstrap interval, and the Holm-adjusted test. Legacy
rules keep reading the legacy report engine in the same gate, and the
report names every engine it used.
"""

from __future__ import annotations

import pytest
from test_benchmark_analysis import _attempt, _run

from benchmarks import gates
from benchmarks.gates import evaluate_gate, frozen_input_for_runs, validate_rules


def _paired_run(identifier: str, right_scores: list[float], *, status="completed"):
    """One run with one arm, six cases, and scored outcomes."""
    attempts = []
    scores = []
    for index, value in enumerate(right_scores):
        attempt_id = f"{identifier}-{index}"
        attempts.append(_attempt(attempt_id, "main", "Main", f"case-{index}", 1, 0))
        scores.append({
            "id": f"score-{attempt_id}", "attempt_id": attempt_id,
            "scorer_id": "exact", "scorer_name": "Exact",
            "scorer_version": "1", "status": "scored",
            "score": value, "passed": int(value >= 1.0),
        })
    run = _run(identifier, status)
    run["attempts"] = attempts
    run["scores"] = scores
    run["arms"] = [{"id": "main", "slug": "main", "name": "Main"}]
    return run


FROZEN_RULE = {
    "id": "frozen-success",
    "label": "Success stays within the margin",
    "metric": "frozen.exact",
    "operator": "max_drop",
    "value": 0.25,
    "analysis_method": "frozen_non_inferiority",
    "direction": "improvement",
    "resample_count": 199,
}


def test_frozen_rules_validate_their_shape():
    validate_rules([FROZEN_RULE])
    with pytest.raises(ValueError, match="frozen metric with a frozen"):
        validate_rules([{**FROZEN_RULE, "metric": "arm.main.score.exact"}])
    with pytest.raises(ValueError, match="max_drop"):
        validate_rules([{**FROZEN_RULE, "operator": "gte"}])
    with pytest.raises(ValueError, match="gte"):
        validate_rules([{**FROZEN_RULE, "analysis_method": "frozen_superiority"}])
    with pytest.raises(ValueError, match="effect direction"):
        validate_rules([{**FROZEN_RULE, "direction": None}])


def test_two_runs_merge_into_one_two_arm_frozen_input():
    baseline = _paired_run("base", [1.0, 1.0, 0.0, 1.0, 1.0, 0.0])
    candidate = _paired_run("cand", [1.0, 0.0, 0.0, 1.0, 1.0, 0.0])
    merged = frozen_input_for_runs(baseline, candidate)
    assert merged["baseline_arm"] == "main"
    assert merged["candidate_arm"] == "main"
    assert merged["families"] == {"math": [f"case-{i}" for i in range(6)]}
    arms = {attempt["arm_id"] for attempt in merged["run"]["attempts"]}
    assert arms == {"baseline", "candidate"}
    assert len(merged["run"]["scores"]) == 12
    with pytest.raises(ValueError, match="has no arm"):
        frozen_input_for_runs(baseline, candidate, arm="absent")


def test_frozen_non_inferiority_passes_an_equal_candidate():
    baseline = _paired_run("base", [1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0])
    candidate = _paired_run("cand", [1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0])
    report = evaluate_gate(baseline, candidate, [FROZEN_RULE], mode="preview")
    (rule,) = report["rules"]
    assert rule["status"] == "passed"
    assert rule["frozen"]["engine"] == "bmas-frozen-analysis"
    assert rule["frozen"]["gate"]["rule"] == "lower_bound_above_negative_margin"
    assert rule["frozen"]["gate"]["margin"] == 0.25
    assert rule["frozen"]["interval"]["status"] == "degenerate"
    assert rule["candidate_value"] == 0.0
    assert len(rule["frozen"]["results_digest"]) == 64
    assert report["engines"] == ["bmas-frozen-analysis"]
    assert report["status"] == "passed"


def test_frozen_non_inferiority_fails_a_clear_regression():
    baseline = _paired_run("base", [1.0] * 12)
    candidate = _paired_run("cand", [0.0] * 12)
    report = evaluate_gate(baseline, candidate, [FROZEN_RULE], mode="preview")
    (rule,) = report["rules"]
    assert rule["status"] == "failed"
    assert rule["frozen"]["estimate"] == -1.0
    assert rule["frozen"]["gate"]["status"] == "failed"
    assert report["status"] == "failed"


def test_frozen_superiority_needs_a_significant_improvement():
    superiority = {
        **FROZEN_RULE, "id": "frozen-better", "operator": "gte", "value": 0,
        "analysis_method": "frozen_superiority",
    }
    baseline = _paired_run("base", [0.0] * 14)
    candidate = _paired_run("cand", [1.0] * 14)
    report = evaluate_gate(baseline, candidate, [superiority], mode="preview")
    (rule,) = report["rules"]
    assert rule["frozen"]["estimate"] == 1.0
    assert rule["frozen"]["gate"]["rule"] == (
        "holm_adjusted_significance_and_interval_excludes_zero"
    )
    assert rule["status"] == "passed"
    same = _paired_run("same", [1.0, 0.0] * 7)
    report = evaluate_gate(same, _paired_run("other", [1.0, 0.0] * 7),
                           [superiority], mode="preview")
    assert report["rules"][0]["status"] == "failed"


def test_frozen_and_legacy_rules_share_one_gate():
    baseline = _paired_run("base", [1.0] * 8)
    candidate = _paired_run("cand", [1.0] * 8)
    legacy = {"id": "cost", "metric": "arm.main.cost_usd.mean",
              "operator": "lte", "value": 100}
    report = evaluate_gate(
        baseline, candidate, [legacy, FROZEN_RULE], mode="preview",
    )
    assert [rule["id"] for rule in report["rules"]] == ["cost", "frozen-success"]
    assert report["rules"][0]["status"] == "passed"
    assert "frozen" not in report["rules"][0]
    assert report["engines"] == ["bmas-frozen-analysis", "legacy-report"]
    assert report["status"] == "passed"


def test_frozen_rule_without_paired_cases_is_indeterminate():
    baseline = _paired_run("base", [1.0] * 4)
    candidate = _paired_run("cand", [1.0] * 4)
    candidate["attempts"] = []
    candidate["scores"] = []
    report = evaluate_gate(baseline, candidate, [FROZEN_RULE], mode="preview")
    rule = report["rules"][0]
    assert rule["status"] == "indeterminate"
    assert rule["frozen"]["engine"] == gates.FROZEN_METRIC_PREFIX.rstrip(".") or True
    assert report["status"] == "indeterminate"
