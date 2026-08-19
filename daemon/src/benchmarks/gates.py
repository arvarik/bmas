"""Evaluate immutable benchmark baselines and regression rules."""

from __future__ import annotations

import math
from typing import Any

from benchmarks.analysis import build_run_report
from benchmarks.provenance import content_checksum

RULE_OPERATORS = {"gte", "lte", "max_drop", "max_increase_ratio"}


def _metric_map(report: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for arm in report.get("arms") or []:
        slug = str(arm["arm_slug"])
        if arm.get("failure_rate") is not None:
            metrics[f"arm.{slug}.failure_rate"] = float(arm["failure_rate"])
        for name in ("cost_usd", "duration_ms", "tokens"):
            values = arm.get(name) or {}
            for statistic in ("mean", "total", "p50", "p95"):
                value = values.get(statistic)
                if value is not None:
                    metrics[f"arm.{slug}.{name}.{statistic}"] = float(value)
        for scorer in arm.get("scorers") or []:
            if scorer.get("mean") is not None:
                metrics[f"arm.{slug}.score.{scorer['scorer_id']}"] = float(scorer["mean"])
    return metrics


def validate_rules(rules: list[dict[str, Any]]) -> None:
    """Reject ambiguous gate rules before a baseline becomes immutable."""
    if not rules:
        raise ValueError("A regression baseline needs at least one rule")
    identifiers: set[str] = set()
    for rule in rules:
        rule_id = str(rule.get("id") or "").strip()
        metric = str(rule.get("metric") or "").strip()
        operator = str(rule.get("operator") or "")
        value = rule.get("value")
        if not rule_id or rule_id in identifiers:
            raise ValueError("Each regression rule needs a unique identifier")
        if not metric.startswith("arm."):
            raise ValueError(f"Regression rule {rule_id} has an invalid metric")
        if operator not in RULE_OPERATORS:
            raise ValueError(f"Regression rule {rule_id} has an invalid operator")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"Regression rule {rule_id} needs a finite value")
        if operator == "max_increase_ratio" and float(value) < 0:
            raise ValueError(f"Regression rule {rule_id} needs a nonnegative ratio")
        identifiers.add(rule_id)


def evaluate_gate(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return passed, failed, or indeterminate for a candidate run."""
    validate_rules(rules)
    baseline_report = build_run_report(baseline_run)
    candidate_report = build_run_report(candidate_run)
    baseline_metrics = _metric_map(baseline_report)
    candidate_metrics = _metric_map(candidate_report)
    results: list[dict[str, Any]] = []
    for rule in rules:
        metric = str(rule["metric"])
        operator = str(rule["operator"])
        threshold = float(rule["value"])
        baseline_value = baseline_metrics.get(metric)
        candidate_value = candidate_metrics.get(metric)
        status = "indeterminate"
        boundary: float | None = None
        if candidate_value is not None:
            if operator == "gte":
                boundary = threshold
                status = "passed" if candidate_value >= boundary else "failed"
            elif operator == "lte":
                boundary = threshold
                status = "passed" if candidate_value <= boundary else "failed"
            elif baseline_value is not None and operator == "max_drop":
                boundary = baseline_value - threshold
                status = "passed" if candidate_value >= boundary else "failed"
            elif baseline_value is not None and operator == "max_increase_ratio":
                boundary = baseline_value * (1 + threshold)
                status = "passed" if candidate_value <= boundary else "failed"
        results.append({
            "id": rule["id"],
            "label": rule.get("label") or rule["id"],
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "baseline_value": baseline_value,
            "candidate_value": candidate_value,
            "boundary": boundary,
            "status": status,
        })
    if baseline_run.get("status") != "completed" or candidate_run.get("status") != "completed":
        status = "indeterminate"
        reason = "Both runs must complete before a regression gate can pass"
    elif any(result["status"] == "failed" for result in results):
        status = "failed"
        reason = "One or more regression rules failed"
    elif any(result["status"] == "indeterminate" for result in results):
        status = "indeterminate"
        reason = "One or more regression metrics are unavailable"
    else:
        status = "passed"
        reason = "Every regression rule passed"
    report = {
        "schema_version": "1",
        "status": status,
        "reason": reason,
        "baseline_run_id": baseline_run["id"],
        "candidate_run_id": candidate_run["id"],
        "baseline_report_checksum": baseline_report["report_checksum"],
        "candidate_report_checksum": candidate_report["report_checksum"],
        "rules": results,
    }
    return {**report, "report_checksum": content_checksum(report)}
