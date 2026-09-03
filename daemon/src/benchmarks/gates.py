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


class GateSettlementError(ValueError):
    """A cost-sensitive final gate waits for run cost settlement."""


def rule_is_cost_sensitive(rule: dict[str, Any]) -> bool:
    """Report whether one rule reads a monetary or resource metric."""
    metric = str(rule.get("metric") or "")
    return ".cost_usd" in metric or ".resource" in metric
# The frozen methods read the predeclared non-inferiority or
# superiority decision of the frozen analysis engine instead of the
# legacy report engine. ``max_drop`` with the frozen non-inferiority
# method declares the margin; ``gte`` with the frozen superiority
# method declares strict improvement.
FROZEN_METHODS = {"frozen_non_inferiority", "frozen_superiority"}
FROZEN_METRIC_PREFIX = "frozen."
ANALYSIS_METHODS = {
    "point_estimate",
    "lower_confidence_bound",
    "upper_confidence_bound",
    "holm_sign_test",
    *FROZEN_METHODS,
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
    if method == "point_estimate" or method in FROZEN_METHODS:
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
    plan = run.get("execution_plan") or {}
    mapping_set = plan.get("outcome_mapping_set") or {}
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
        # Only the complete mapping-set digest enters the invariant.
        # The member list and the mapping contents stay outside, so
        # equal sets compare equal whatever member order produced them.
        "outcome_mapping_set": str(mapping_set.get("digest") or ""),
        "statistics": {
            "practical_difference": configuration.get(
                "practical_difference", 0.01,
            ),
            "repetitions": configuration.get("repetitions"),
            "estimand": configuration.get("statistics") or {},
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
        if not metric.startswith(("arm.", "comparison.", FROZEN_METRIC_PREFIX)):
            raise ValueError(f"Regression rule {rule_id} has an invalid metric")
        if method not in ANALYSIS_METHODS:
            raise ValueError(f"Regression rule {rule_id} has an invalid analysis method")
        if metric.startswith(FROZEN_METRIC_PREFIX) != (method in FROZEN_METHODS):
            raise ValueError(
                f"Regression rule {rule_id} pairs a frozen metric with a "
                "frozen analysis method"
            )
        if method == "frozen_non_inferiority" and operator != "max_drop":
            raise ValueError(
                f"Regression rule {rule_id} declares its non-inferiority "
                "margin through the max_drop operator"
            )
        if method == "frozen_superiority" and operator != "gte":
            raise ValueError(
                f"Regression rule {rule_id} declares superiority through "
                "the gte operator"
            )
        if method in FROZEN_METHODS and rule.get("direction") not in (
            EFFECT_DIRECTIONS
        ):
            raise ValueError(
                f"Regression rule {rule_id} needs an effect direction: "
                f"{' or '.join(EFFECT_DIRECTIONS)}"
            )
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
        if ".wilson" in metric:
            raise ValueError(
                f"Regression rule {rule_id} reads a Wilson interval; a "
                "Wilson interval is an unclustered slot diagnostic and "
                "never enters a gate"
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


# ── Frozen comparisons across two runs ───────────────────────────────


def _frozen_arm(run: dict[str, Any], requested: str | None) -> str:
    arms = run.get("arms") or []
    slugs = [str(arm.get("slug") or arm.get("arm_slug") or "") for arm in arms]
    if not slugs:
        slugs = sorted({
            str(attempt.get("arm_slug") or attempt.get("arm_id") or "")
            for attempt in run.get("attempts") or []
        })
    if requested:
        if requested not in slugs:
            raise ValueError(
                f"The run {run.get('id')} has no arm {requested!r}"
            )
        return requested
    if not slugs:
        raise ValueError(f"The run {run.get('id')} has no arms")
    return slugs[0]


def _attempt_arm(attempt: dict[str, Any]) -> str:
    return str(attempt.get("arm_slug") or attempt.get("arm_id") or "")


def frozen_input_for_runs(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    *,
    arm: str | None = None,
) -> dict[str, Any]:
    """Merge one arm of two runs into a two-arm run for the frozen engine.

    The baseline run's attempts become the ``baseline`` arm and the
    candidate run's attempts become the ``candidate`` arm. Cases pair
    by dataset item, families follow the item subject, and the
    planned repetitions equal the largest repeat index either run
    planned.
    """
    baseline_arm = _frozen_arm(baseline_run, arm)
    candidate_arm = _frozen_arm(candidate_run, arm)
    attempts: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    families: dict[str, set[str]] = {}
    planned = 1
    for role, run, slug in (
        ("baseline", baseline_run, baseline_arm),
        ("candidate", candidate_run, candidate_arm),
    ):
        kept_ids: set[str] = set()
        for attempt in run.get("attempts") or []:
            if _attempt_arm(attempt) != slug:
                continue
            case_id = str(attempt.get("dataset_item_id") or attempt.get("item_key") or "")
            if not case_id:
                continue
            kept_ids.add(str(attempt["id"]))
            family = str(attempt.get("subject") or "cases")
            families.setdefault(family, set()).add(case_id)
            planned = max(planned, int(attempt.get("repeat_index") or 1))
            attempts.append({**attempt, "arm_id": role,
                             "dataset_item_id": case_id})
        for score in run.get("scores") or []:
            if str(score.get("attempt_id")) in kept_ids:
                scores.append(dict(score))
    return {
        "run": {
            "id": f"gate:{baseline_run.get('id')}:{candidate_run.get('id')}",
            "attempts": attempts,
            "scores": scores,
        },
        "families": {
            family: sorted(case_ids) for family, case_ids in sorted(families.items())
        },
        "planned_repetitions": planned,
        "baseline_arm": baseline_arm,
        "candidate_arm": candidate_arm,
    }


def _frozen_rule_result(
    rule: dict[str, Any],
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
) -> dict[str, Any]:
    """Decide one frozen rule through the frozen analysis engine."""
    import hashlib

    from benchmarks import frozen_analysis

    metric = str(rule["metric"])
    parts = metric[len(FROZEN_METRIC_PREFIX):].split(".")
    scorer_id = parts[0]
    arm = parts[1] if len(parts) > 1 and parts[1] else None
    method = str(rule["analysis_method"])
    direction = (
        "lower_is_better" if rule.get("direction") == "reduction"
        else "higher_is_better"
    )
    merged = frozen_input_for_runs(baseline_run, candidate_run, arm=arm)
    seed_material = (
        f"{baseline_run.get('id')}\x00{candidate_run.get('id')}\x00"
        f"{rule['id']}"
    ).encode("utf-8")
    master_seed = int.from_bytes(
        hashlib.sha256(seed_material).digest()[:8], "big",
    )
    comparison = {
        "comparison_id": str(rule["id"]),
        "metric": scorer_id,
        "baseline_arm": "baseline",
        "candidate_arm": "candidate",
        "direction": direction,
        "hypothesis": (
            "non_inferiority" if method == "frozen_non_inferiority"
            else "superiority"
        ),
        "non_inferiority_margin": (
            float(rule["value"]) if method == "frozen_non_inferiority"
            else None
        ),
        "minimum_usable_cases": int(rule.get("minimum_usable_cases") or 1),
    }
    frozen_block: dict[str, Any]
    status = "indeterminate"
    candidate_value = None
    boundary = None
    present_arms = {
        str(attempt["arm_id"]) for attempt in merged["run"]["attempts"]
    }
    if not merged["families"] or present_arms != {"baseline", "candidate"}:
        frozen_block = {
            "engine": frozen_analysis.ENGINE_NAME,
            "reason": "no paired cases exist between the runs",
        }
    else:
        specification = frozen_analysis.freeze_specification(
            families=merged["families"],
            scorer_id=scorer_id,
            master_seed=master_seed,
            comparison_family={
                "family_id": f"gate-{rule['id']}",
                "comparisons": [comparison],
            },
            resample_count=int(rule.get("resample_count") or 999),
            min_family_cases=1,
            confidence_level=float(rule.get("confidence_level") or 0.95),
        )
        frozen_input = frozen_analysis.freeze_input(
            merged["run"], specification,
            planned_repetitions=int(merged["planned_repetitions"]),
        )
        report = frozen_analysis.compute_report(specification, frozen_input)
        decided = report["comparisons"][0]
        gate = decided["gate"]
        status = str(gate["status"])
        candidate_value = decided["estimate"]
        boundary = gate.get("bound")
        frozen_block = {
            "engine": report["engine"],
            "engine_version": report["engine_version"],
            "specification_digest": specification["specification_digest"],
            "input_digest": frozen_input["input_digest"],
            "results_digest": report["results_digest"],
            "baseline_arm": merged["baseline_arm"],
            "candidate_arm": merged["candidate_arm"],
            "estimate": decided["estimate"],
            "interval": decided["interval"],
            "test": decided["test"],
            "p_value_adjusted": decided["p_value_adjusted"],
            "gate": gate,
            "counts": decided["counts"],
            "statistical_unit": decided["statistical_unit"],
        }
    return {
        "id": rule["id"],
        "label": rule.get("label") or rule["id"],
        "metric": metric,
        "resolved_metric": metric,
        "analysis_method": method,
        "operator": str(rule["operator"]),
        "threshold": float(rule["value"]),
        "direction": rule.get("direction"),
        "practical_size": rule.get("practical_size"),
        "classification": None,
        "baseline_value": None,
        "candidate_value": candidate_value,
        "boundary": boundary,
        "direction_guard": None,
        "display_exception": None,
        "status": status,
        "frozen": frozen_block,
    }


def evaluate_gate(
    baseline_run: dict[str, Any],
    candidate_run: dict[str, Any],
    rules: list[dict[str, Any]],
    *,
    mode: str = "final",
    treatment_declaration: list[str] | None = None,
    display_exceptions: list[dict[str, Any]] | None = None,
    cost_evidence: dict[str, Any] | None = None,
    now: str = "",
) -> dict[str, Any]:
    """Return passed, failed, or indeterminate for a candidate run.

    ``preview`` inspects an active candidate and never persists, so it
    cannot block a later final decision. ``final`` requires terminal
    runs with a valid analysis and stores exactly one terminal
    decision. Compatibility fails before any rule evaluates. A
    cost-sensitive final gate additionally waits for the candidate run
    cost to settle, and an unbounded unknown amount fails every cost
    rule instead of passing it.
    """
    if mode not in GATE_MODES:
        raise ValueError(f"Unknown gate mode: {mode!r}")
    cost_rules = [rule for rule in rules if rule_is_cost_sensitive(rule)]
    unknown_unbounded = bool((cost_evidence or {}).get("unbounded_unknown"))
    if mode == "final" and cost_rules and cost_evidence is not None:
        cost_status = str(cost_evidence.get("cost_status") or "provisional")
        if cost_status != "settled":
            raise GateSettlementError(
                "A cost-sensitive final gate waits for settlement; the "
                f"candidate run cost is {cost_status}"
            )
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
        if str(rule.get("analysis_method") or "") in FROZEN_METHODS:
            results.append(_frozen_rule_result(rule, baseline_run, candidate_run))
            continue
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
        if guard_reason is None and unknown_unbounded and (
            rule_is_cost_sensitive(rule)
        ):
            # An unbounded unknown amount can never pass a cost rule.
            guard_reason = (
                "An unbounded unknown charge blocks a passing cost rule"
            )
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
        "engines": sorted({
            "bmas-frozen-analysis" if "frozen" in result else "legacy-report"
            for result in results
        }),
    }
    return {**report, "report_checksum": content_checksum(report)}
