"""Scorer plugins: every documented class with evidence validation.

Deterministic comparisons cover exact, normalized exact, numeric
tolerance, multiple choice, and structured assertions with every
supported configuration value. The environment scorer follows final
state and ignores final prose. The trajectory scorer detects loops,
false completion, forgotten constraints, and recovery with evidence
references. Judges receive blind, order-randomized candidates, and a
judge fault produces a scorer failure, never a fabricated score.
Missing evidence returns one clear unavailable result.
"""

from __future__ import annotations

import pytest

from benchmarks import scorer_plugins
from benchmarks.scorer_plugins import (
    CompositeScorer,
    DeterministicAnswerScorer,
    FinalStateVerifier,
    HumanReviewScorer,
    RubricJudgeScorer,
    ScorerPluginError,
    TrajectoryScorer,
    build_judge_request,
)


def _deterministic(evidence, configuration):
    return DeterministicAnswerScorer().score(evidence, configuration)


# ── Deterministic comparisons ────────────────────────────────────────


def test_exact_comparison():
    passed = _deterministic(
        {"final_output": "42", "reference_answer": "42"},
        {"comparison": "exact"},
    )
    failed = _deterministic(
        {"final_output": " 42", "reference_answer": "42"},
        {"comparison": "exact"},
    )
    assert passed["passed"] is True
    assert passed["dimensions"] == [
        {"name": "accuracy", "value": 1.0, "category": None},
    ]
    assert failed["passed"] is False


def test_normalized_exact_comparison():
    result = _deterministic(
        {"final_output": "  The   ANSWER is 42  ",
         "reference_answer": "the answer is 42"},
        {"comparison": "normalized_exact"},
    )
    assert result["passed"] is True


@pytest.mark.parametrize(
    ("output", "configuration", "expected"),
    [
        ("42.05", {"absolute_tolerance": 0.1}, True),
        ("42.5", {"absolute_tolerance": 0.1}, False),
        ("43.0", {"relative_tolerance": 0.05}, True),
        ("46.0", {"relative_tolerance": 0.05}, False),
        ("not a number", {"absolute_tolerance": 1.0}, False),
    ],
)
def test_numeric_tolerance_comparison(output, configuration, expected):
    result = _deterministic(
        {"final_output": output, "reference_answer": "42"},
        {"comparison": "numeric_tolerance", **configuration},
    )
    assert result["passed"] is expected


def test_multiple_choice_comparison():
    configuration = {
        "comparison": "multiple_choice", "choices": ["A", "B", "C", "D"],
    }
    correct = _deterministic(
        {"final_output": "The answer is B.", "reference_answer": "B"},
        configuration,
    )
    wrong = _deterministic(
        {"final_output": "I pick C", "reference_answer": "B"},
        configuration,
    )
    none_found = _deterministic(
        {"final_output": "no idea", "reference_answer": "B"},
        configuration,
    )
    assert correct["passed"] is True
    assert correct["dimensions"][0]["category"] == "B"
    assert wrong["passed"] is False
    assert none_found["explanation"] == "no_choice_found"
    with pytest.raises(ScorerPluginError, match="choices"):
        _deterministic(
            {"final_output": "A", "reference_answer": "A"},
            {"comparison": "multiple_choice"},
        )


def test_structured_assertions_comparison():
    output = '{"result": {"total": 42, "unit": "count"}, "items": [1, 2]}'
    configuration = {
        "comparison": "structured_assertions",
        "assertions": [
            {"pointer": "/result/total", "operator": "eq", "value": 42},
            {"pointer": "/result/unit", "operator": "contains",
             "value": "count"},
            {"pointer": "/items/1", "operator": "eq", "value": 2},
            {"pointer": "/result", "operator": "exists"},
            {"pointer": "/missing", "operator": "ne", "value": 1},
        ],
    }
    result = _deterministic({"final_output": output}, configuration)
    assert result["passed"] is False
    assert result["dimensions"][0]["value"] == pytest.approx(0.8)
    assert "/missing" in result["explanation"]

    not_json = _deterministic(
        {"final_output": "plain text"}, configuration,
    )
    assert not_json["explanation"] == "output_not_json"


def test_unknown_comparison_rejects():
    with pytest.raises(ScorerPluginError, match="Unknown comparison"):
        _deterministic(
            {"final_output": "42", "reference_answer": "42"},
            {"comparison": "fuzzy_vibes"},
        )


def test_missing_evidence_returns_unavailable():
    result = _deterministic({}, {"comparison": "exact"})
    assert result["status"] == "unavailable"
    assert result["missing_evidence"] == [
        "final_output", "reference_answer",
    ]
    assert result["passed"] is None
    assert result["dimensions"] == []


# ── Environment and final-state verification ─────────────────────────


def test_environment_scorer_follows_final_state_not_prose():
    verifier = FinalStateVerifier()
    # Changed prose, unchanged state: the scorer still passes.
    prose_changed = verifier.score(
        {
            "final_state": {"state": {"file": "created"}},
            "expected_final_state": {"file": "created"},
            "final_output": "I totally failed at everything.",
        },
        {},
    )
    assert prose_changed["passed"] is True
    # Changed state, unchanged prose: the scorer detects the failure.
    state_changed = verifier.score(
        {
            "final_state": {"state": {"file": "missing"}},
            "expected_final_state": {"file": "created"},
            "final_output": "Task complete, everything worked.",
        },
        {},
    )
    assert state_changed["passed"] is False
    assert "file" in state_changed["explanation"]


def test_final_state_verifier_needs_no_reference_answer():
    verifier = FinalStateVerifier()
    result = verifier.score(
        {
            "final_state": {"state": {"count": 3}},
            "expected_final_state": {"count": 3},
        },
        {},
    )
    assert result["passed"] is True
    missing = verifier.score({"final_state": {"state": {}}}, {})
    assert missing["status"] == "unavailable"
    assert missing["missing_evidence"] == ["expected_final_state"]


# ── Trajectory scoring ───────────────────────────────────────────────


def _trajectory(events, configuration=None):
    return TrajectoryScorer().score(
        {"trace_events": events}, configuration or {},
    )


def test_trajectory_detects_loops():
    events = [
        {"kind": "action", "action": "retry_fetch"} for _ in range(4)
    ]
    result = _trajectory(events, {"loop_threshold": 3})
    dimensions = {
        dimension["name"]: dimension["value"]
        for dimension in result["dimensions"]
    }
    assert dimensions["loop_free"] == 0.0
    assert result["passed"] is False
    assert result["evidence_marks"]["loop"]


def test_trajectory_detects_false_completion():
    events = [
        {"kind": "action", "action": "write"},
        {"kind": "completion_claim"},
    ]
    result = _trajectory(events)
    dimensions = {
        dimension["name"]: dimension["value"]
        for dimension in result["dimensions"]
    }
    assert dimensions["no_false_completion"] == 0.0
    assert result["evidence_marks"]["false_completion"] == [1]


def test_trajectory_detects_forgotten_constraints():
    events = [
        {"kind": "constraint_declared", "constraint": "stay-readonly"},
        {"kind": "constraint_violated", "constraint": "stay-readonly"},
    ]
    result = _trajectory(events)
    dimensions = {
        dimension["name"]: dimension["value"]
        for dimension in result["dimensions"]
    }
    assert dimensions["constraints_kept"] == 0.0
    assert result["evidence_marks"]["forgotten_constraint"] == [1]


def test_trajectory_detects_successful_recovery():
    events = [
        {"kind": "failure"},
        {"kind": "action", "action": "fix"},
        {"kind": "verified_success"},
        {"kind": "completion_claim"},
    ]
    result = _trajectory(events)
    dimensions = {
        dimension["name"]: dimension["value"]
        for dimension in result["dimensions"]
    }
    assert dimensions["recovered_from_failure"] == 1.0
    assert dimensions["no_false_completion"] == 1.0
    assert result["passed"] is True


def test_trajectory_without_trace_is_unavailable():
    result = TrajectoryScorer().score({}, {})
    assert result["status"] == "unavailable"


# ── Judge scoring with blind identity ────────────────────────────────


CANDIDATES = [
    {"candidate_id": "run-classic", "content": "Answer alpha",
     "runtime": "classic"},
    {"candidate_id": "run-patchboard", "content": "Answer beta",
     "runtime": "patchboard"},
    {"candidate_id": "run-solo", "content": "Answer gamma",
     "runtime": "solo"},
]


def test_judge_request_blinds_identity_and_randomizes_order():
    built = build_judge_request(
        rubric={"criteria": ["clarity"]},
        candidates=CANDIDATES,
        seed=3,
    )
    rendered = str(built["request"])
    for hidden in ("classic", "patchboard", "solo", "run-", "runtime"):
        assert hidden not in rendered
    labels = [
        candidate["label"] for candidate in built["request"]["candidates"]
    ]
    assert labels == ["candidate-1", "candidate-2", "candidate-3"]
    assert sorted(built["order_mapping"].values()) == [
        "run-classic", "run-patchboard", "run-solo",
    ]
    natural = [
        candidate["content"] for candidate in built["request"]["candidates"]
    ]
    shuffled = any(
        build_judge_request(
            rubric={}, candidates=CANDIDATES, seed=seed,
        )["request"]["candidates"][0]["content"] != natural[0]
        for seed in range(6)
    )
    assert shuffled


def test_judge_order_is_deterministic_per_seed():
    first = build_judge_request(rubric={}, candidates=CANDIDATES, seed=9)
    second = build_judge_request(rubric={}, candidates=CANDIDATES, seed=9)
    assert first["request"] == second["request"]
    assert first["request_digest"] == second["request_digest"]


def test_judge_timeout_is_a_scorer_failure_not_a_score():
    def failing_judge(request):
        raise TimeoutError("judge deadline exceeded")

    scorer = RubricJudgeScorer(failing_judge)
    result = scorer.score(
        {"candidates": CANDIDATES, "rubric": {"criteria": []}},
        {"seed": 1},
    )
    assert result["status"] == "error"
    assert result["passed"] is None
    assert result["dimensions"] == []
    assert "deadline" in result["error"]


def test_judge_success_records_request_and_response_digests():
    def judge(request):
        return {
            "dimensions": [{"name": "clarity", "value": 0.9,
                            "category": None}],
            "passed": True,
            "explanation": "clear",
        }

    result = RubricJudgeScorer(judge).score(
        {"candidates": CANDIDATES, "rubric": {"criteria": ["clarity"]}},
        {"seed": 1},
    )
    assert result["status"] == "scored"
    assert len(result["judge"]["request_digest"]) == 64
    assert len(result["judge"]["response_digest"]) == 64


# ── Human review and composite scoring ───────────────────────────────


def test_human_review_scorer():
    result = HumanReviewScorer().score(
        {"human_review": {"reviewer": "reviewer-a", "passed": True,
                          "notes": "verified by hand"}},
        {},
    )
    assert result["passed"] is True
    assert result["explanation"] == "verified by hand"
    unavailable = HumanReviewScorer().score({}, {})
    assert unavailable["status"] == "unavailable"
    with pytest.raises(ScorerPluginError, match="reviewer"):
        HumanReviewScorer().score({"human_review": {"passed": True}}, {})


def test_composite_scorer_uses_an_explicit_formula():
    child_results = [
        {"dimensions": [{"name": "accuracy", "value": 1.0}]},
        {"dimensions": [{"name": "clarity", "value": 0.5}]},
    ]
    result = CompositeScorer().score(
        {"child_results": child_results},
        {"weights": {"accuracy": 3, "clarity": 1}},
    )
    combined = next(
        dimension["value"]
        for dimension in result["dimensions"]
        if dimension["name"] == "composite"
    )
    assert combined == pytest.approx((3 * 1.0 + 1 * 0.5) / 4)
    assert "weighted_formula" in result["explanation"]
    assert "3*accuracy" in result["explanation"]
    missing_child = CompositeScorer().score(
        {"child_results": child_results},
        {"weights": {"accuracy": 1, "latency": 1}},
    )
    assert missing_child["status"] == "unavailable"
    with pytest.raises(ScorerPluginError, match="weights"):
        CompositeScorer().score({"child_results": child_results}, {})


def test_plugin_factory_covers_every_type():
    for plugin_type in ("deterministic", "final_state", "environment",
                        "trajectory", "human_review", "composite"):
        plugin = scorer_plugins.plugin_for(plugin_type)
        assert plugin.trust_class in scorer_plugins.TRUST_CLASSES
    judge_plugin = scorer_plugins.plugin_for(
        "rubric_judge", judge=lambda request: {},
    )
    assert judge_plugin.trust_class == "sandboxed_wasi"
    with pytest.raises(ScorerPluginError, match="judge transport"):
        scorer_plugins.plugin_for("rubric_judge")
    with pytest.raises(ScorerPluginError, match="Unknown plugin"):
        scorer_plugins.plugin_for("oracle")
