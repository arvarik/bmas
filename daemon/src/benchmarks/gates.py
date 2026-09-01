"""Evaluate immutable benchmark baselines and regression rules.

A final gate decision is terminal: it evaluates only terminal
candidates with a valid analysis, and it never changes. An unsaved
preview inspects an active candidate without creating a decision, so
an early preview can never block a later final decision.

Two runs are gate-compatible only when their invariant digests match.
The invariant digest covers the cases, the scorers, the environments,
the tools, the outcome mapping set, and the statistics. Runtime,
model, prompt, and configuration stay outside the digest as declared
treatments, and an observed treatment difference that the baseline
declaration does not allow fails before rule evaluation.

A comparison rule declares its effect direction and practical size and
reads corrected significance, so a statistically clear regression can
never pass an improvement rule. A narrow display exception can excuse
one unavailable secondary display metric; it can never cover primary
cases, scorers, units, outcomes, estimands, or missingness.
"""

from __future__ import annotations

import math
from typing import Any

from benchmarks.analysis import build_run_report
from benchmarks.provenance import content_checksum

RULE_OPERATORS = {"gte", "lte", "max_drop", "max_increase_ratio"}

GATE_MODES = ("preview", "final")

# The states in which a run can never change again.
TERMINAL_RUN_STATUSES = {"completed", "partial", "failed", "cancelled"}

# The experimental axes a treatment declaration can allow. These stay
# outside the invariant digest.
TREATMENT_AXES = ("runtime", "model", "prompt", "configuration")

# Invariant concerns a display exception can never cover.
PROTECTED_EXCEPTION_TARGETS = (
    "cases",
    "scorers",
    "units",
    "outcomes",
    "estimands",
    "missingness",
)

EFFECT_DIRECTIONS = ("improvement", "reduction")

INVARIANT_DIGEST_DOMAIN = "benchmark-gate-invariant"


class GateCompatibilityError(ValueError):
    """The candidate and baseline are not gate-compatible."""


class GateTerminalityError(ValueError):
    """A final gate needs terminal runs with a valid analysis."""
ANALYSIS_METHODS = {
    "point_estimate",
    "lower_confidence_bound",
    "upper_confidence_bound",
    "holm_sign_test",
}


def _metric_map(report: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for arm in report.get("arms") or []:
        slug = str(arm["arm_slug"])
        if arm.get("failure_rate") is not None:
            metrics[f"arm.{slug}.failure_rate"] = float(arm["failure_rate"])
        for name in ("cost_usd", "duration_ms", "tokens"):
            values = arm.get(name) or {}
            for statistic in ("mean", "total", "p50", "p95", "ci_low", "ci_high"):
                value = values.get(statistic)
                if value is not None:
                    metrics[f"arm.{slug}.{name}.{statistic}"] = float(value)
        for scorer in arm.get("scorers") or []:
            if scorer.get("mean") is not None:
                metrics[f"arm.{slug}.score.{scorer['scorer_id']}"] = float(scorer["mean"])
                metrics[f"arm.{slug}.score.{scorer['scorer_id']}.mean"] = float(scorer["mean"])
            for statistic in ("ci_low", "ci_high"):
                value = scorer.get(statistic)
                if value is not None:
                    metrics[
                        f"arm.{slug}.score.{scorer['scorer_id']}.{statistic}"
                    ] = float(value)
    for comparison in report.get("comparisons") or []:
        pair = (
            f"comparison.{comparison['left_arm_slug']}."
            f"{comparison['right_arm_slug']}"
        )
        for scorer in comparison.get("scorers") or []:
            base = f"{pair}.score.{scorer['scorer_id']}"
            if scorer.get("mean") is not None:
                metrics[base] = float(scorer["mean"])
            for statistic in (
                "mean",
                "ci_low",
                "ci_high",
                "probability_of_superiority",
                "standardized_paired_effect",
                "p_value_raw",
                "p_value_adjusted",
            ):
                value = scorer.get(statistic)
                if value is not None:
                    metrics[f"{base}.{statistic}"] = float(value)
    return metrics


def _resolved_metric(rule: dict[str, Any]) -> str:
    """Resolve one rule method into an exact report metric path."""
    metric = str(rule["metric"])
    method = str(rule.get("analysis_method") or "point_estimate")
    if method == "point_estimate":
        return metric
    suffix = {
        "lower_confidence_bound": "ci_low",
        "upper_confidence_bound": "ci_high",
        "holm_sign_test": "p_value_adjusted",
    }[method]
    if method in {"lower_confidence_bound", "upper_confidence_bound"} and metric.endswith(
        ".mean"
    ):
        return f"{metric.rsplit('.', 1)[0]}.{suffix}"
    return f"{metric}.{suffix}"


def primary_scorer_id(run: dict[str, Any]) -> str | None:
    """Return the run's primary metric: the first required scorer."""
    links = sorted(
        (
            link
            for link in run.get("revision_scorers") or []
            if bool(link.get("required", True))
        ),
        key=lambda link: (int(link.get("sort_order") or 0), str(link["id"])),
    )
    return str(links[0]["id"]) if links else None


def invariant_digest(run: dict[str, Any]) -> str:
    """Digest the gate-invariant identity of one run.

    The digest covers the cases, the scorers, the environments, the
    tools, the outcome mapping set, and the statistics. Runtime,
    model, prompt, and configuration treatments stay outside it.
    """
    configuration = run.get("test_configuration") or {}
    scorers = sorted(
        (
            {
                "scorer_id": str(link["id"]),
                "version": str(link.get("version") or ""),
                "configuration_checksum": str(
                    link.get("configuration_checksum") or "",
                ),
                "required": bool(link.get("required", True)),
            }
            for link in run.get("revision_scorers") or []
        ),
        key=lambda link: str(link["scorer_id"]),
    )
    return content_checksum({
        "domain": INVARIANT_DIGEST_DOMAIN,
        "cases": {
            "dataset_id": run.get("dataset_id"),
            "dataset_checksum": run.get("dataset_checksum"),
        },
        "scorers": scorers,
        "primary_metric": primary_scorer_id(run),
        "environments": configuration.get("environments") or [],
        "tools": configuration.get("tools") or [],
        "outcome_mapping_set": configuration.get("outcome_mappings") or {},
        "statistics": {
            "practical_difference": configuration.get(
                "practical_difference", 0.01,
            ),
            "repetitions": configuration.get("repetitions"),
        },
    })


def _arm_treatment_values(run: dict[str, Any], field: str) -> list[Any]:
    values = []
    for arm in run.get("arms") or []:
        configuration = arm.get("configuration") or {}
        effective = configuration.get("effective_configuration") or {}
        overrides = configuration.get("submission_overrides") or {}
        values.append(
            effective.get(field, overrides.get(field)),
        )
    return sorted(values, key=repr)


def observed_treatments(
    baseline_run: dict[str, Any], candidate_run: dict[str, Any],
) -> list[str]:
    """List every treatment axis that differs between two runs."""
    observed: list[str] = []
    baseline_runtimes = sorted(
        str(arm.get("runtime_id")) for arm in baseline_run.get("arms") or []
    )
    candidate_runtimes = sorted(
        str(arm.get("runtime_id")) for arm in candidate_run.get("arms") or []
    )
    if baseline_runtimes != candidate_runtimes:
        observed.append("runtime")
    for axis, field in (("model", "model"), ("prompt", "prompt")):
        if _arm_treatment_values(
            baseline_run, field,
        ) != _arm_treatment_values(candidate_run, field):
            observed.append(axis)
    if str(baseline_run.get("test_configuration_checksum")) != str(
        candidate_run.get("test_configuration_checksum"),
    ):
        observed.append("configuration")
    return observed


def validate_treatment_declaration(declaration: list[str]) -> None:
    """Reject an unknown or duplicate treatment axis."""
    seen: set[str] = set()
    for axis in declaration:
        if axis not in TREATMENT_AXES:
            raise ValueError(
                f"Unknown treatment axis: {axis!r}. The allowed axes are "
                f"{', '.join(TREATMENT_AXES)}."
            )
        if axis in seen:
            raise ValueError(f"Duplicate treatment axis: {axis!r}")
        seen.add(axis)


def check_compatibility(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    treatment_declaration: list[str],
) -> dict[str, Any]:
    """Reject an incompatible candidate before rule evaluation.

    The check compares the invariant components one by one, so a
    rejection names the exact incompatibility, then requires equal
    invariant digests and only declared treatment differences.
    """
    if str(baseline_run.get("dataset_checksum")) != str(
        candidate_run.get("dataset_checksum"),
    ):
        raise GateCompatibilityError(
            "The candidate uses a different dataset checksum"
        )
    baseline_scorers = {
        (str(link["id"]), str(link.get("configuration_checksum") or ""))
        for link in baseline_run.get("revision_scorers") or []
    }
    candidate_scorers = {
        (str(link["id"]), str(link.get("configuration_checksum") or ""))
        for link in candidate_run.get("revision_scorers") or []
    }
    if baseline_scorers != candidate_scorers:
        raise GateCompatibilityError(
            "The candidate uses a different scorer digest"
        )
    if primary_scorer_id(baseline_run) != primary_scorer_id(candidate_run):
        raise GateCompatibilityError(
            "The candidate uses a different primary metric"
        )
    baseline_digest = invariant_digest(baseline_run)
    candidate_digest = invariant_digest(candidate_run)
    observed = observed_treatments(baseline_run, candidate_run)
    undeclared = [
        axis for axis in observed if axis not in treatment_declaration
    ]
    if undeclared:
        raise GateCompatibilityError(
            "The candidate changes undeclared treatments: "
            f"{', '.join(sorted(undeclared))}"
        )
    if baseline_digest != candidate_digest:
        raise GateCompatibilityError(
            "The candidate invariant digest does not match the baseline"
        )
    return {
        "baseline_invariant_digest": baseline_digest,
        "candidate_invariant_digest": candidate_digest,
        "observed_treatments": observed,
    }


def validate_display_exceptions(
    exceptions: list[dict[str, Any]],
    *,
    primary_metric: str | None,
) -> None:
    """Validate narrow display exceptions or fail closed.

    Each exception names its scope, author, expiry, and reason. An
    exception can cover only one unavailable secondary display metric.
    It can never cover primary cases, scorers, units, outcomes,
    estimands, or missingness.
    """
    for exception in exceptions:
        for field in ("scope", "author", "expires_at", "reason"):
            if not exception.get(field):
                raise ValueError(
                    f"A display exception requires {field}"
                )
        scope = str(exception["scope"])
        for target in PROTECTED_EXCEPTION_TARGETS:
            if scope == target or scope.startswith(f"{target}"):
                raise ValueError(
                    "A display exception can never cover "
                    f"{target}; it covers secondary display metrics only"
                )
        if not scope.startswith("secondary_display:"):
            raise ValueError(
                "A display exception scope names one secondary display "
                "metric as secondary_display:<metric>"
            )
        metric = scope.split(":", 1)[1]
        if primary_metric and f".score.{primary_metric}" in metric:
            raise ValueError(
                "A display exception can never cover the primary metric"
            )


def _exception_covers(
    exceptions: list[dict[str, Any]], metric: str, now: str,
) -> dict[str, Any] | None:
    for exception in exceptions:
        scope = str(exception.get("scope") or "")
        if scope == f"secondary_display:{metric}" and (
            str(exception.get("expires_at") or "") > now
        ):
            return exception
    return None


def validate_rules(rules: list[dict[str, Any]]) -> None:
    """Reject ambiguous gate rules before a baseline becomes immutable."""
    if not rules:
        raise ValueError("A regression baseline needs at least one rule")
    identifiers: set[str] = set()
    for rule in rules:
        rule_id = str(rule.get("id") or "").strip()
        metric = str(rule.get("metric") or "").strip()
        operator = str(rule.get("operator") or "")
        method = str(rule.get("analysis_method") or "point_estimate")
        value = rule.get("value")
        if not rule_id or rule_id in identifiers:
            raise ValueError("Each regression rule needs a unique identifier")
        if not metric.startswith(("arm.", "comparison.")):
            raise ValueError(f"Regression rule {rule_id} has an invalid metric")
        if method not in ANALYSIS_METHODS:
            raise ValueError(f"Regression rule {rule_id} has an invalid analysis method")
        if method == "holm_sign_test" and not metric.startswith("comparison."):
            raise ValueError(
                f"Regression rule {rule_id} needs a comparison metric for a sign test"
            )
        if method in {"lower_confidence_bound", "upper_confidence_bound"} and not (
            ".score." in metric or metric.endswith(".mean")
        ):
            raise ValueError(
                f"Regression rule {rule_id} selects a metric without a confidence bound"
            )
        if operator not in RULE_OPERATORS:
            raise ValueError(f"Regression rule {rule_id} has an invalid operator")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"Regression rule {rule_id} needs a finite value")
        if operator == "max_increase_ratio" and float(value) < 0:
            raise ValueError(f"Regression rule {rule_id} needs a nonnegative ratio")
        if ".p_value_raw" in metric:
            raise ValueError(
                f"Regression rule {rule_id} must read corrected "
                "significance, never a raw p-value"
            )
        direction = rule.get("direction")
        if metric.startswith("comparison."):
            # A comparison rule declares its effect direction and reads
            # the corrected paired analysis, so direction-aware guards
            # can reject a statistically clear regression.
            if direction not in EFFECT_DIRECTIONS:
                raise ValueError(
                    f"Regression rule {rule_id} needs an effect direction: "
                    f"{' or '.join(EFFECT_DIRECTIONS)}"
                )
            practical_size = rule.get("practical_size")
            if practical_size is not None and (
                not isinstance(practical_size, (int, float))
                or float(practical_size) < 0
            ):
                raise ValueError(
                    f"Regression rule {rule_id} needs a nonnegative "
                    "practical size"
                )
        elif direction is not None and direction not in EFFECT_DIRECTIONS:
            raise ValueError(
                f"Regression rule {rule_id} has an invalid direction"
            )
        identifiers.add(rule_id)


def _comparison_classification(
    report: dict[str, Any], metric: str,
) -> str | None:
    """Return the corrected classification behind one comparison metric."""
    if not metric.startswith("comparison."):
        return None
    parts = metric.split(".")
    if len(parts) < 5 or parts[3] != "score":
        return None
    left_slug, right_slug, scorer_id = parts[1], parts[2], parts[4]
    for comparison in report.get("comparisons") or []:
        if (
            str(comparison.get("left_arm_slug")) == left_slug
            and str(comparison.get("right_arm_slug")) == right_slug
        ):
            for scorer in comparison.get("scorers") or []:
                if str(scorer.get("scorer_id")) == scorer_id:
                    return scorer.get("classification")
    return None


def _direction_guard(
    rule: dict[str, Any], classification: str | None,
) -> str | None:
    """Fail a direction-aware rule against a clear opposing effect.

    A statistically clear practical regression can never pass an
    improvement rule, and a clear increase can never pass a reduction
    rule, whatever the threshold outcome says.
    """
    direction = rule.get("direction")
    if direction == "improvement" and classification == (
        "meaningful_regression"
    ):
        return "A statistically clear regression cannot pass an improvement rule"
    if direction == "reduction" and classification == (
        "meaningful_improvement"
    ):
        return "A statistically clear increase cannot pass a reduction rule"
    return None


def evaluate_gate(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    rules: list[dict[str, Any]],
    *,
    mode: str = "final",
    treatment_declaration: list[str] | None = None,
    display_exceptions: list[dict[str, Any]] | None = None,
    now: str = "",
) -> dict[str, Any]:
    """Return passed, failed, or indeterminate for a candidate run.

    ``preview`` inspects an active candidate and never persists, so it
    cannot block a later final decision. ``final`` requires terminal
    runs with a valid analysis and stores exactly one terminal
    decision. Compatibility fails before any rule evaluates.
    """
    if mode not in GATE_MODES:
        raise ValueError(f"Unknown gate mode: {mode!r}")
    validate_rules(rules)
    declaration = list(treatment_declaration or [])
    validate_treatment_declaration(declaration)
    exceptions = list(display_exceptions or [])
    validate_display_exceptions(
        exceptions, primary_metric=primary_scorer_id(baseline_run),
    )
    compatibility = check_compatibility(
        baseline_run, candidate_run, declaration,
    )
    if mode == "final":
        for role, run in (("baseline", baseline_run),
                          ("candidate", candidate_run)):
            if str(run.get("status")) not in TERMINAL_RUN_STATUSES:
                raise GateTerminalityError(
                    f"A final gate needs a terminal {role} run; "
                    f"{run.get('id')} is {run.get('status')}"
                )
            if str(run.get("analysis_status") or "valid") == "blocked":
                raise GateTerminalityError(
                    f"The {role} analysis is blocked by a failed required "
                    "scorer, so no valid analysis exists"
                )
    baseline_report = build_run_report(baseline_run)
    candidate_report = build_run_report(candidate_run)
    baseline_metrics = _metric_map(baseline_report)
    candidate_metrics = _metric_map(candidate_report)
    results: list[dict[str, Any]] = []
    for rule in rules:
        metric = str(rule["metric"])
        resolved_metric = _resolved_metric(rule)
        operator = str(rule["operator"])
        threshold = float(rule["value"])
        baseline_value = baseline_metrics.get(resolved_metric)
        candidate_value = candidate_metrics.get(resolved_metric)
        status = "indeterminate"
        boundary: float | None = None
        guard_reason: str | None = None
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
        classification = _comparison_classification(candidate_report, metric)
        guard_reason = _direction_guard(rule, classification)
        if guard_reason is not None:
            status = "failed"
        exception = None
        if status == "indeterminate":
            exception = _exception_covers(exceptions, metric, now)
            if exception is not None:
                status = "waived_display"
        results.append({
            "id": rule["id"],
            "label": rule.get("label") or rule["id"],
            "metric": metric,
            "resolved_metric": resolved_metric,
            "analysis_method": rule.get("analysis_method") or "point_estimate",
            "operator": operator,
            "threshold": threshold,
            "direction": rule.get("direction"),
            "practical_size": rule.get("practical_size"),
            "classification": classification,
            "baseline_value": baseline_value,
            "candidate_value": candidate_value,
            "boundary": boundary,
            "direction_guard": guard_reason,
            "display_exception": (
                {
                    "scope": exception["scope"],
                    "author": exception["author"],
                    "expires_at": exception["expires_at"],
                    "reason": exception["reason"],
                }
                if exception
                else None
            ),
            "status": status,
        })
    if mode == "preview" and (
        baseline_run.get("status") != "completed"
        or str(candidate_run.get("status")) not in TERMINAL_RUN_STATUSES
    ):
        status = "indeterminate"
        reason = (
            "This preview is unsaved; the candidate has not reached a "
            "terminal state"
        )
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
        "schema_version": "2",
        "analysis_version": candidate_report["analysis"]["version"],
        "mode": mode,
        "status": status,
        "reason": reason,
        "baseline_run_id": baseline_run["id"],
        "candidate_run_id": candidate_run["id"],
        "baseline_report_checksum": baseline_report["report_checksum"],
        "candidate_report_checksum": candidate_report["report_checksum"],
        "treatment_declaration": sorted(declaration),
        **compatibility,
        "display_exceptions": exceptions,
        "rules": results,
    }
    return {**report, "report_checksum": content_checksum(report)}
