"""Analytics views: every chart traces to named analysis fields.

Each view states its denominator and exclusions where it counts,
labels every interval with its unit and method, and reads only from
the frozen report, the frozen input, and stored records.
"""

from __future__ import annotations

from test_frozen_analysis import oracle_spec

from benchmarks import analytics_views, judge_calibration
from benchmarks.frozen_analysis import compute_report
from benchmarks.resource_ledger import ledger_entry, summarize
from core.money import Money


def _report():
    _, spec, frozen = oracle_spec()
    entries = [
        ledger_entry(
            run_id="run-frozen", resource_class="runtime", provider="p",
            service="s", region="r", quantity=10, unit="tokens",
            pricing_version="v", estimate=Money("USD", 100),
            actual=Money("USD", 120), actual_provider_text="0.00000012",
            now="2026-09-01T00:00:00Z",
        ),
    ]
    summary = summarize(entries, currency="USD")
    report = compute_report(
        spec, frozen, ledger_summary=summary,
        latency_ms_by_arm={"left": [100, 200, 300], "right": [90, 95, 400]},
    )
    return report, frozen


def test_every_section_traces_to_named_fields():
    report, frozen = _report()
    overview = analytics_views.overview(report, frozen_input=frozen)
    assert [section["view"] for section in overview["sections"]] == [
        "unconditional_success_funnel",
        "primary_metric_with_uncertainty",
        "cost_latency_pareto",
        "paired_case_differences",
        "horizon_degradation_curves",
        "failure_recovery_matrix",
        "memory_cascade_diagnostics",
        "human_and_judge_calibration",
    ]
    for section in overview["sections"]:
        assert section["source_fields"], section["view"]
    assert overview["estimand"] == (
        "family-balanced-unconditional-task-success"
    )
    assert overview["replay"]["claim"] == "analysis_not_replayable"


def test_funnel_states_denominator_and_exclusions():
    report, _ = _report()
    funnel = analytics_views.success_funnel(report)
    assert funnel["denominator"] == "arms.*.unconditional_denominator"
    assert funnel["exclusions"] == "arms.*.counts.excluded"
    left = next(row for row in funnel["rows"] if row["arm"] == "left")
    assert left["planned"] == 24
    assert left["excluded"] == 1
    assert left["denominator"] == 23


def test_primary_metric_states_unit_and_method():
    report, _ = _report()
    view = analytics_views.primary_metric(report)
    row = view["rows"][0]
    assert row["unit"] == "case"
    assert row["method"].startswith("family_stratified_weighted_case")
    assert row["multiplicity_family"] == "primary"
    assert row["gate"] in ("passed", "failed", "indeterminate")


def test_pareto_marks_the_frontier_and_money_contract():
    report, _ = _report()
    view = analytics_views.pareto(
        report, {"left": {"currency": "USD", "amount_nanos": 5}},
    )
    assert view["cost_contract"] == "Money(currency, amount_nanos)"
    frontier = [point["arm"] for point in view["points"] if point["frontier"]]
    assert frontier


def test_paired_differences_and_horizon_curves():
    report, frozen = _report()
    differences = analytics_views.paired_differences(report)
    assert {row["family"] for row in differences["rows"]} == {
        "algebra", "geometry",
    }
    assert differences["unit"] == "case"
    horizons = analytics_views.horizon_curves(
        report, {"a1": "short", "g7": "long"}, frozen,
    )
    groups = {(row["arm"], row["horizon"]) for row in horizons["rows"]}
    assert ("left", "short") in groups
    assert ("right", "long") in groups
    assert "infrastructure" in horizons["exclusions"]


def test_failure_matrix_and_memory_diagnostics():
    classifications = [
        {"attempt_id": "attempt-a",
         "classes": [{"family": "long_horizon", "name": "memory"}]},
        {"attempt_id": "attempt-b",
         "classes": [{"family": "long_horizon", "name": "memory"}]},
    ]
    trajectories = [
        {"attempt_id": "attempt-a", "dimensions": [
            {"name": "recovered_from_failure", "value": 1.0},
            {"name": "constraints_kept", "value": 1.0},
            {"name": "loop_free", "value": 0.0},
            {"name": "no_false_completion", "value": 1.0},
        ]},
        {"attempt_id": "attempt-b", "dimensions": [
            {"name": "recovered_from_failure", "value": 0.0},
            {"name": "constraints_kept", "value": 0.0},
            {"name": "loop_free", "value": 1.0},
            {"name": "no_false_completion", "value": 0.0},
        ]},
    ]
    matrix = analytics_views.failure_recovery_matrix(
        classifications, trajectories,
    )
    assert matrix["rows"] == [
        {"class": "long_horizon/memory", "recovered": 1, "not_recovered": 1},
    ]
    diagnostics = analytics_views.memory_cascade_diagnostics(trajectories)
    assert diagnostics["constraint_retention_rate"] == 0.5
    assert diagnostics["loop_rate"] == 0.5
    assert diagnostics["false_completion_rate"] == 0.5


def test_calibration_view_reads_stored_records():
    labels = judge_calibration.pinned_label_set("labels", "1", [
        {"item_id": f"item-{index}", "label": "pass" if index % 2 else "fail"}
        for index in range(8)
    ])
    record = judge_calibration.calibrate(
        judge_id="judge-a", judge_version="1", judge_model="judge",
        prompt_digest="a" * 64, scorer_id="scorer-a", scorer_version="1",
        label_set=labels,
        judge_outputs={item["item_id"]: item["label"]
                       for item in labels["items"]},
        candidate_models=["model-a"],
    )
    panel = judge_calibration.adjudicate([
        {"reviewer": "a", "passed": True},
        {"reviewer": "b", "passed": True},
    ])
    view = analytics_views.calibration_view([record], [panel])
    assert view["judges"][0]["state"] == "current"
    assert view["judges"][0]["kappa_defined"] is True
    assert view["panels"][0]["decision"] == "passed"
