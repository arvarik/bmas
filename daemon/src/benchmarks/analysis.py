"""Build reproducible benchmark aggregates and paired comparisons."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import fmean, stdev
from typing import TYPE_CHECKING, Any

from benchmarks.provenance import content_checksum

if TYPE_CHECKING:
    from collections.abc import Iterable


def safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet software from executing exported text as a formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _mean_interval(
    values: list[float],
    lower: float | None = None,
    upper: float | None = None,
) -> dict[str, float | int | None]:
    """Return a normal 95 percent interval for one finite sample."""
    clean = [value for value in values if math.isfinite(value)]
    if not clean:
        return {"count": 0, "mean": None, "ci_low": None, "ci_high": None}
    mean = fmean(clean)
    if len(clean) < 2:
        return {"count": 1, "mean": mean, "ci_low": None, "ci_high": None}
    margin = 1.96 * stdev(clean) / math.sqrt(len(clean))
    low = mean - margin
    high = mean + margin
    if lower is not None:
        low = max(lower, low)
    if upper is not None:
        high = min(upper, high)
    return {"count": len(clean), "mean": mean, "ci_low": low, "ci_high": high}


def _percentile(values: list[float], percentile: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    index = (len(clean) - 1) * percentile
    lower_index = math.floor(index)
    upper_index = math.ceil(index)
    if lower_index == upper_index:
        return clean[lower_index]
    weight = index - lower_index
    return clean[lower_index] * (1 - weight) + clean[upper_index] * weight


def _latest_attempts(attempts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for attempt in attempts:
        key = (str(attempt["trial_id"]), int(attempt.get("repeat_index") or 1))
        previous = latest.get(key)
        if previous is None or int(attempt.get("retry_index") or 0) > int(previous.get("retry_index") or 0):
            latest[key] = attempt
    return list(latest.values())


def _matches_filters(attempt: dict[str, Any], filters: dict[str, Any]) -> bool:
    if filters.get("subject") and attempt.get("subject") != filters["subject"]:
        return False
    if filters.get("split") and attempt.get("split") != filters["split"]:
        return False
    return not (
        filters.get("tag") and filters["tag"] not in (attempt.get("tags") or [])
    )


def _metric(values: list[float]) -> dict[str, float | int | None]:
    interval = _mean_interval(values)
    return {
        **interval,
        "total": sum(values) if values else None,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
    }


def build_run_report(run: dict[str, Any], filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate one run without treating excluded attempts as zero scores."""
    selected_filters = {key: value for key, value in (filters or {}).items() if value}
    all_latest = _latest_attempts(run.get("attempts") or [])
    latest = [attempt for attempt in all_latest if _matches_filters(attempt, selected_filters)]
    latest_ids = {str(attempt["id"]) for attempt in latest}
    score_rows = [
        score for score in run.get("scores") or []
        if str(score["attempt_id"]) in latest_ids
        and (not selected_filters.get("scorer_id") or score.get("scorer_id") == selected_filters["scorer_id"])
    ]
    scores_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in score_rows:
        scores_by_attempt[str(score["attempt_id"])].append(score)

    arms: list[dict[str, Any]] = []
    arm_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in latest:
        arm_attempts[str(attempt["arm_id"])].append(attempt)
    for arm_id, attempts in arm_attempts.items():
        first = attempts[0]
        completed = [attempt for attempt in attempts if attempt.get("status") == "completed"]
        failures = [attempt for attempt in attempts if attempt.get("status") != "completed"]
        score_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in attempts:
            for score in scores_by_attempt.get(str(attempt["id"]), []):
                score_groups[str(score["scorer_id"])].append(score)
        scorer_metrics = []
        for scorer_id, scores in sorted(score_groups.items()):
            scored = [score for score in scores if score.get("status") == "scored" and score.get("score") is not None]
            values = [float(score["score"]) for score in scored]
            scorer_metrics.append({
                "scorer_id": scorer_id,
                "scorer_name": scores[0].get("scorer_name"),
                "scorer_version": scores[0].get("scorer_version"),
                **_mean_interval(values, 0.0, 1.0),
                "passed": sum(1 for score in scored if bool(score.get("passed"))),
                "failed": sum(1 for score in scored if not bool(score.get("passed"))),
                "excluded": sum(1 for score in scores if score.get("status") != "scored"),
            })
        arms.append({
            "arm_id": arm_id,
            "arm_name": first.get("arm_name"),
            "arm_slug": first.get("arm_slug"),
            "runtime_id": first.get("runtime_id"),
            "attempt_count": len(attempts),
            "completed_count": len(completed),
            "failure_count": len(failures),
            "failure_rate": len(failures) / len(attempts) if attempts else None,
            "cost_usd": _metric([
                float(attempt["total_cost_usd"])
                for attempt in completed
                if attempt.get("total_cost_usd") is not None
            ]),
            "duration_ms": _metric([float(attempt["duration_ms"]) for attempt in completed if attempt.get("duration_ms") is not None]),
            "tokens": _metric([float(attempt["total_tokens"]) for attempt in completed if attempt.get("total_tokens") is not None]),
            "scorers": scorer_metrics,
        })

    comparisons: list[dict[str, Any]] = []
    sorted_arms = sorted(arms, key=lambda item: str(item["arm_slug"]))
    attempt_by_arm_key: dict[str, dict[tuple[str, int], dict[str, Any]]] = defaultdict(dict)
    for attempt in latest:
        key = (str(attempt["dataset_item_id"]), int(attempt.get("repeat_index") or 1))
        attempt_by_arm_key[str(attempt["arm_id"])][key] = attempt
    for left_index, left_arm in enumerate(sorted_arms):
        for right_arm in sorted_arms[left_index + 1:]:
            left_items = attempt_by_arm_key[str(left_arm["arm_id"])]
            right_items = attempt_by_arm_key[str(right_arm["arm_id"])]
            common_keys = sorted(set(left_items) & set(right_items))
            scorer_deltas: dict[str, list[float]] = defaultdict(list)
            for key in common_keys:
                left_scores = {
                    str(score["scorer_id"]): score
                    for score in scores_by_attempt.get(str(left_items[key]["id"]), [])
                    if score.get("status") == "scored" and score.get("score") is not None
                }
                right_scores = {
                    str(score["scorer_id"]): score
                    for score in scores_by_attempt.get(str(right_items[key]["id"]), [])
                    if score.get("status") == "scored" and score.get("score") is not None
                }
                for scorer_id in set(left_scores) & set(right_scores):
                    scorer_deltas[scorer_id].append(
                        float(right_scores[scorer_id]["score"]) - float(left_scores[scorer_id]["score"])
                    )
            comparisons.append({
                "left_arm_id": left_arm["arm_id"],
                "left_arm_name": left_arm["arm_name"],
                "right_arm_id": right_arm["arm_id"],
                "right_arm_name": right_arm["arm_name"],
                "matched_attempts": len(common_keys),
                "scorers": [
                    {
                        "scorer_id": scorer_id,
                        **_mean_interval(deltas, -1.0, 1.0),
                        "wins": sum(delta > 0 for delta in deltas),
                        "ties": sum(delta == 0 for delta in deltas),
                        "losses": sum(delta < 0 for delta in deltas),
                        "direction": "right_minus_left",
                    }
                    for scorer_id, deltas in sorted(scorer_deltas.items())
                ],
            })

    report = {
        "schema_version": "1",
        "interval_method": "normal_95",
        "run": {
            "id": run["id"],
            "status": run["status"],
            "test_id": run["test_id"],
            "test_revision_id": run["test_revision_id"],
            "test_configuration_checksum": run.get("test_configuration_checksum"),
            "dataset_id": run.get("dataset_id"),
            "dataset_checksum": run.get("dataset_checksum"),
            "execution_plan_checksum": run.get("execution_plan_checksum"),
        },
        "filters": selected_filters,
        "latest_attempt_count": len(latest),
        "prior_attempt_count": len(run.get("attempts") or []) - len(all_latest),
        "arms": sorted_arms,
        "comparisons": comparisons,
        "complete": run.get("status") == "completed",
    }
    return {**report, "report_checksum": content_checksum(report)}


def report_csv_rows(run: dict[str, Any], filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return one current-attempt CSV row per scorer result."""
    selected_filters = {key: value for key, value in (filters or {}).items() if value}
    latest = [attempt for attempt in _latest_attempts(run.get("attempts") or []) if _matches_filters(attempt, selected_filters)]
    scores_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in run.get("scores") or []:
        scores_by_attempt[str(score["attempt_id"])].append(score)
    rows: list[dict[str, Any]] = []
    for attempt in latest:
        attempt_scores = scores_by_attempt.get(str(attempt["id"]))
        scores: list[dict[str, Any] | None]
        if attempt_scores:
            scores = [*attempt_scores]
        else:
            scores = [None]
        for score in scores:
            if score and selected_filters.get("scorer_id") and score.get("scorer_id") != selected_filters["scorer_id"]:
                continue
            rows.append({
                "run_id": run["id"],
                "test_revision_id": run["test_revision_id"],
                "dataset_checksum": run.get("dataset_checksum"),
                "arm": attempt.get("arm_name"),
                "runtime_id": attempt.get("runtime_id"),
                "item_key": attempt.get("item_key"),
                "subject": attempt.get("subject"),
                "split": attempt.get("split"),
                "repeat_index": attempt.get("repeat_index"),
                "retry_index": attempt.get("retry_index"),
                "attempt_status": attempt.get("status"),
                "scorer_id": score.get("scorer_id") if score else None,
                "scorer_version": score.get("scorer_version") if score else None,
                "score_status": score.get("status") if score else None,
                "score": score.get("score") if score else None,
                "passed": score.get("passed") if score else None,
                "cost_usd": attempt.get("total_cost_usd"),
                "duration_ms": attempt.get("duration_ms"),
                "tokens": attempt.get("total_tokens"),
                "task_id": attempt.get("task_id"),
                "attempt_snapshot_checksum": attempt.get("snapshot_checksum"),
            })
    return rows
