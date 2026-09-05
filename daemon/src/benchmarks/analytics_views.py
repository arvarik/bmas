"""Resource and reliability analytics views over one frozen report.

Every view traces to named analysis fields, states its denominator
and exclusions, and labels every interval with its statistical unit
and method. The views cover the unconditional success funnel, the
primary metric with uncertainty, the cost and latency Pareto chart,
paired case differences, horizon degradation curves, the failure and
recovery matrix, memory and cascade diagnostics, and human and judge
calibration.
"""

from __future__ import annotations

from typing import Any


def _field(path: str) -> str:
    return path


def success_funnel(report: dict[str, Any]) -> dict[str, Any]:
    """Planned, admitted, observed, and passed slots per arm."""
    rows = []
    for arm_id, arm in sorted(report["arms"].items()):
        counts = arm["counts"]
        rows.append({
            "arm": arm_id,
            "planned": counts["planned"],
            "admitted": counts["admitted"],
            "excluded": counts["excluded"],
            "failed_zero": counts["failed"],
            "missing": counts["missing"],
            "retried": counts["retried"],
            "denominator": arm["unconditional_denominator"],
            "successes": arm["unconditional_successes"],
            "rate": arm["unconditional_success_rate"],
        })
    return {
        "view": "unconditional_success_funnel",
        "rows": rows,
        "denominator": "arms.*.unconditional_denominator",
        "exclusions": "arms.*.counts.excluded",
        "source_fields": [
            _field("arms.*.counts"),
            _field("arms.*.unconditional_successes"),
            _field("arms.*.unconditional_success_rate"),
        ],
    }


def primary_metric(report: dict[str, Any]) -> dict[str, Any]:
    """The primary estimand with its clustered uncertainty."""
    rows = []
    for comparison in report["comparisons"]:
        interval = comparison["interval"]
        rows.append({
            "comparison_id": comparison["comparison_id"],
            "estimate": comparison["estimate"],
            "interval_low": interval.get("low"),
            "interval_high": interval.get("high"),
            "interval_status": interval["status"],
            "unit": comparison["statistical_unit"],
            "method": interval["method"],
            "p_value_adjusted": comparison["p_value_adjusted"],
            "multiplicity_family": comparison["multiplicity_family"],
            "gate": comparison["gate"]["status"],
            "primary_valid": comparison["primary_valid"],
        })
    return {
        "view": "primary_metric_with_uncertainty",
        "estimand": report["primary_estimand"],
        "rows": rows,
        "source_fields": [
            _field("comparisons.*.estimate"),
            _field("comparisons.*.interval"),
            _field("comparisons.*.p_value_adjusted"),
            _field("comparisons.*.gate"),
        ],
    }


def pareto(
    report: dict[str, Any], cost_by_arm: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Quality against cost and latency with the Pareto frontier."""
    points = []
    for arm_id, arm in sorted(report["arms"].items()):
        cost = (cost_by_arm or {}).get(arm_id)
        points.append({
            "arm": arm_id,
            "quality": arm["unconditional_success_rate"],
            "cost": cost,
            "latency_median_ms": arm["latency_ms"]["median_ms"],
        })
    for point in points:
        point["frontier"] = not any(
            other is not point
            and (other["quality"] or 0) >= (point["quality"] or 0)
            and (other["latency_median_ms"] or 0)
            <= (point["latency_median_ms"] or 0)
            and (other["quality"], other["latency_median_ms"])
            != (point["quality"], point["latency_median_ms"])
            for other in points
        )
    return {
        "view": "cost_latency_pareto",
        "points": points,
        "cost_contract": "Money(currency, amount_nanos)",
        "source_fields": [
            _field("arms.*.unconditional_success_rate"),
            _field("arms.*.latency_ms"),
            _field("resources"),
        ],
    }


def paired_differences(report: dict[str, Any]) -> dict[str, Any]:
    """Exact family aggregates and weights per comparison."""
    rows = []
    for comparison in report["comparisons"]:
        for family, aggregate in sorted(
            comparison["family_aggregates"].items(),
        ):
            weights = comparison["weights"][family]
            rows.append({
                "comparison_id": comparison["comparison_id"],
                "family": family,
                "aggregate_delta": aggregate,
                "family_weight": weights["family_weight"],
                "removed_weight": weights["removed_weight"],
                "missing_case_ids": weights["missing_case_ids"],
            })
    return {
        "view": "paired_case_differences",
        "rows": rows,
        "unit": "case",
        "source_fields": [
            _field("comparisons.*.family_aggregates"),
            _field("comparisons.*.weights"),
        ],
    }


def horizon_curves(
    report: dict[str, Any], horizon_by_case: dict[str, str],
    frozen_input: dict[str, Any],
) -> dict[str, Any]:
    """Success by intrinsic horizon group per arm."""
    rows = []
    for arm_id in frozen_input["arms"]:
        groups: dict[str, dict[str, int]] = {}
        for case_id, case_slots in frozen_input["slots"][arm_id].items():
            horizon = str(horizon_by_case.get(case_id) or "unknown")
            bucket = groups.setdefault(horizon, {"slots": 0, "passed": 0})
            for slot in case_slots.values():
                if slot["state"] in ("observed", "failed_zero"):
                    bucket["slots"] += 1
                    bucket["passed"] += 1 if slot["passed"] else 0
        for horizon, bucket in sorted(groups.items()):
            rows.append({
                "arm": arm_id,
                "horizon": horizon,
                "denominator": bucket["slots"],
                "successes": bucket["passed"],
                "rate": (bucket["passed"] / bucket["slots"]
                         if bucket["slots"] else None),
            })
    return {
        "view": "horizon_degradation_curves",
        "rows": rows,
        "denominator": "observed and failed slots per horizon group",
        "exclusions": "infrastructure and unplanned missing slots",
        "source_fields": [_field("frozen_input.slots"), _field("arms.*.counts")],
    }


def failure_recovery_matrix(
    classifications: list[dict[str, Any]],
    trajectory_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Failure family counts crossed with recovery outcomes."""
    matrix: dict[str, dict[str, int]] = {}
    recovered_attempts = {
        str(result.get("attempt_id"))
        for result in trajectory_results
        if any(
            dimension.get("name") == "recovered_from_failure"
            and dimension.get("value") == 1.0
            for dimension in result.get("dimensions") or []
        )
    }
    for record in classifications:
        recovered = str(record.get("attempt_id")) in recovered_attempts
        for cls in record.get("classes") or []:
            row = matrix.setdefault(
                f"{cls['family']}/{cls['name']}",
                {"recovered": 0, "not_recovered": 0},
            )
            row["recovered" if recovered else "not_recovered"] += 1
    return {
        "view": "failure_recovery_matrix",
        "rows": [
            {"class": name, **counts} for name, counts in sorted(matrix.items())
        ],
        "source_fields": [
            _field("failure_classification_records.classes"),
            _field("trajectory.dimensions.recovered_from_failure"),
        ],
    }


def memory_cascade_diagnostics(
    trajectory_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Constraint retention and cascade indicators from trajectories."""
    total = len(trajectory_results)
    kept = 0
    loops = 0
    false_completion = 0
    for result in trajectory_results:
        values = {
            str(dimension.get("name")): dimension.get("value")
            for dimension in result.get("dimensions") or []
        }
        kept += 1 if values.get("constraints_kept") == 1.0 else 0
        loops += 1 if values.get("loop_free") == 0.0 else 0
        false_completion += 1 if values.get("no_false_completion") == 0.0 else 0
    return {
        "view": "memory_cascade_diagnostics",
        "trajectories": total,
        "constraint_retention_rate": kept / total if total else None,
        "loop_rate": loops / total if total else None,
        "false_completion_rate": false_completion / total if total else None,
        "denominator": "scored trajectories",
        "source_fields": [
            _field("trajectory.dimensions.constraints_kept"),
            _field("trajectory.dimensions.loop_free"),
            _field("trajectory.dimensions.no_false_completion"),
        ],
    }


def calibration_view(
    calibrations: list[dict[str, Any]],
    panels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Judge calibration and human panel health."""
    return {
        "view": "human_and_judge_calibration",
        "judges": [
            {
                "judge_id": record["judge"]["judge_id"],
                "version": record["judge"]["version"],
                "state": record["state"],
                "raw_agreement": record["agreement"]["raw"],
                "kappa": record["agreement"]["kappa"],
                "kappa_defined": record["agreement"]["kappa_defined"],
                "abstention_rate": record["abstention"]["rate"],
                "invalid_output_rate": record["invalid_output"]["rate"],
                "independent": record["independence"]["independent"],
            }
            for record in calibrations
        ],
        "panels": [
            {
                "decision": panel["decision"],
                "raw_agreement": panel["raw_agreement"],
                "kappa": panel["kappa"],
                "kappa_defined": panel["kappa_defined"],
                "tie": panel["tie"],
            }
            for panel in panels
        ],
        "source_fields": [
            _field("judge_calibration_records.agreement"),
            _field("judge_calibration_records.abstention"),
            _field("review_panel.decision"),
        ],
    }


async def overview_inputs(run: dict[str, Any]) -> dict[str, Any]:
    """Collect the stored inputs every overview view reads for one run.

    The cost per arm sums the confirmed ledger charges of the arm's
    attempts. The horizon per case comes from the dataset items. The
    failure classifications and the score records come from the
    evaluation storage per attempt, and the calibrations are the
    stored judge calibration records.
    """
    import database as db
    from benchmarks import evaluation_records, resource_ledger
    from benchmarks.costs import money_to_json
    from core.money import Money

    attempts = list(run.get("attempts") or [])
    arm_of_attempt = {
        str(attempt["id"]): str(attempt.get("arm_id") or attempt.get("arm_name") or "")
        for attempt in attempts
    }
    run_id = str(run["id"])
    totals: dict[str, Money] = {}
    for entry in await resource_ledger.list_entries(run_id):
        actual = (entry.get("actual") or {}).get("value")
        attempt_id = str((entry.get("references") or {}).get("attempt_id") or "")
        arm = arm_of_attempt.get(attempt_id)
        if not arm or not actual:
            continue
        money = Money(str(actual["currency"]), int(actual["amount_nanos"]))
        totals[arm] = totals[arm].add(money) if arm in totals else money
    cost_by_arm = {arm: money_to_json(total) for arm, total in totals.items()}

    horizon_by_case: dict[str, str] = {}
    version_id = str(run.get("dataset_version_id") or "")
    if version_id:
        offset = 0
        while True:
            page, total = await db.list_dataset_items(
                version_id, limit=200, offset=offset,
            )
            for item in page:
                metadata = item.get("metadata") or {}
                classification = metadata.get("classification") or {}
                horizon = (
                    classification.get("intrinsic_horizon")
                    or metadata.get("intrinsic_horizon")
                )
                if horizon:
                    horizon_by_case[str(item.get("id"))] = str(horizon)
                    horizon_by_case[str(item.get("item_key") or item.get("id"))] = str(horizon)
            offset += len(page)
            if not page or offset >= int(total):
                break

    classifications: list[dict[str, Any]] = []
    trajectory_results: list[dict[str, Any]] = []
    for attempt in attempts:
        attempt_id = str(attempt["id"])
        for stored in await evaluation_records.list_records_for(
            "failure-classification-record", "attempt_id", attempt_id,
        ):
            classifications.append({"attempt_id": attempt_id, **stored["record"]})
        for stored in await evaluation_records.list_records_for(
            "score-record", "attempt_id", attempt_id,
        ):
            trajectory_results.append({"attempt_id": attempt_id, **stored["record"]})
    calibrations = [
        stored["record"]
        for stored in await evaluation_records.list_records(
            "judge-calibration-record",
        )
    ]
    return {
        "cost_by_arm": cost_by_arm or None,
        "horizon_by_case": horizon_by_case,
        "classifications": classifications,
        "trajectory_results": trajectory_results,
        "calibrations": calibrations,
        "panels": [],
    }


def overview(
    report: dict[str, Any],
    *,
    frozen_input: dict[str, Any],
    cost_by_arm: dict[str, dict[str, Any]] | None = None,
    horizon_by_case: dict[str, str] | None = None,
    classifications: list[dict[str, Any]] | None = None,
    trajectory_results: list[dict[str, Any]] | None = None,
    calibrations: list[dict[str, Any]] | None = None,
    panels: list[dict[str, Any]] | None = None,
    replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble every documented overview section."""
    return {
        "sections": [
            success_funnel(report),
            primary_metric(report),
            pareto(report, cost_by_arm),
            paired_differences(report),
            horizon_curves(report, horizon_by_case or {}, frozen_input),
            failure_recovery_matrix(
                classifications or [], trajectory_results or [],
            ),
            memory_cascade_diagnostics(trajectory_results or []),
            calibration_view(calibrations or [], panels or []),
        ],
        "estimand": report["primary_estimand"],
        "replay": replay or {"claim": "analysis_not_replayable"},
        "resources": report["resources"],
    }
