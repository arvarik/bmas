"""Tests for benchmark comparisons, regression gates, and qualification."""

import pytest

from benchmarks.analysis import build_run_report, report_csv_rows, safe_csv_cell
from benchmarks.gates import evaluate_gate, validate_rules
from benchmarks.qualification import qualify_runtime


def _attempt(identifier, arm_id, arm_name, item, repeat, retry, status="completed"):
    return {
        "id": identifier,
        "trial_id": f"trial-{arm_id}-{item}",
        "dataset_item_id": item,
        "item_key": item,
        "subject": "math",
        "split": "test",
        "tags": ["smoke"],
        "arm_id": arm_id,
        "arm_name": arm_name,
        "arm_slug": arm_id,
        "runtime_id": arm_id,
        "repeat_index": repeat,
        "retry_index": retry,
        "status": status,
        "total_cost_usd": 0.01,
        "total_tokens": 100,
        "duration_ms": 1000,
        "task_id": f"task-{identifier}",
        "snapshot_checksum": f"checksum-{identifier}",
    }


def _run(identifier="run-one", status="completed"):
    attempts = [
        _attempt("left-old", "left", "Left", "one", 1, 0, "failed"),
        _attempt("left-new", "left", "Left", "one", 1, 1),
        _attempt("right-one", "right", "Right", "one", 1, 0),
    ]
    scores = [
        {"id": "s-old", "attempt_id": "left-old", "scorer_id": "exact", "scorer_name": "Exact", "scorer_version": "1", "status": "excluded", "score": None, "passed": None},
        {"id": "s-left", "attempt_id": "left-new", "scorer_id": "exact", "scorer_name": "Exact", "scorer_version": "1", "status": "scored", "score": 0.0, "passed": 0},
        {"id": "s-right", "attempt_id": "right-one", "scorer_id": "exact", "scorer_name": "Exact", "scorer_version": "1", "status": "scored", "score": 1.0, "passed": 1},
    ]
    return {
        "id": identifier,
        "status": status,
        "test_id": "test-one",
        "test_revision_id": "revision-one",
        "test_configuration_checksum": "test-checksum",
        "dataset_id": "dataset-one",
        "dataset_checksum": "dataset-checksum",
        "execution_plan_checksum": "plan-checksum",
        "attempts": attempts,
        "scores": scores,
    }


def test_report_uses_latest_retries_and_paired_deltas():
    report = build_run_report(_run(), {"subject": "math", "tag": "smoke"})

    assert report["latest_attempt_count"] == 2
    assert report["prior_attempt_count"] == 1
    assert report["comparisons"][0]["scorers"][0]["mean"] == 1.0
    assert report["comparisons"][0]["scorers"][0]["wins"] == 1
    assert len(report_csv_rows(_run())) == 2


def test_csv_export_protects_spreadsheet_formula_cells():
    assert safe_csv_cell("=IMPORTXML('bad')") == "'=IMPORTXML('bad')"
    assert safe_csv_cell("math") == "math"


def test_gate_marks_incomplete_runs_indeterminate():
    rules = [{
        "id": "score",
        "metric": "arm.right.score.exact",
        "operator": "gte",
        "value": 0.9,
    }]
    report = evaluate_gate(_run("baseline"), _run("candidate", "partial"), rules)
    assert report["status"] == "indeterminate"


def test_gate_rejects_duplicate_rule_identifiers():
    with pytest.raises(ValueError, match="unique"):
        validate_rules([
            {"id": "same", "metric": "arm.a.failure_rate", "operator": "lte", "value": 0.1},
            {"id": "same", "metric": "arm.b.failure_rate", "operator": "lte", "value": 0.1},
        ])


def test_gate_rejects_an_empty_rule_set():
    with pytest.raises(ValueError, match="at least one"):
        validate_rules([])


@pytest.mark.asyncio
async def test_classic_runtime_has_a_truthful_provisional_qualification():
    report = await qualify_runtime("classic")
    assert report["status"] == "provisional"
    assert report["evidence_status"] == "not_run"
    assert all(check["status"] == "passed" for check in report["checks"])
