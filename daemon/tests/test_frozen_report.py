"""The benchmark report route serves frozen snapshots with published metrics.

Before a snapshot exists the route serves the legacy report and says
so. Once a frozen snapshot exists the route recomputes it from the
stored specification and evidence, verifies the stored digests, and
resolves every declared metric to one published definition. A metric
without a published definition blocks the report unless the caller
explicitly allows an unresolved display, and a superseded snapshot
never serves.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from test_evaluation_contracts import valid_metric_definition
from test_frozen_analysis import _stored_spec, comparison, snapshot_db, spec_for  # noqa: F401

from benchmarks import frozen_analysis, metric_registry
from routes import benchmarks as benchmark_routes

RUN_ID = "run-evidence"
METRIC_ID = "metric-task-success"


def publishable_metric_definition() -> dict:
    """The contract fixture completed with a deterministic calibration."""
    definition = valid_metric_definition()
    definition["calibration"] = {
        "state": "current",
        "method": "deterministic",
        "version": "1",
        "dataset": "calibration-fixtures",
        "result": {"limits_failed": False, "pinned_digests": {}},
        "calibrated_at": "2026-09-01T00:00:00Z",
        "expires_at": "2027-09-01T00:00:00Z",
        "drift_policy": "recalibrate-on-implementation-change",
    }
    return definition


async def _publish_metric() -> None:
    from benchmarks import facade

    await facade.execute(
        "register_metric_definition",
        {"record": publishable_metric_definition()},
    )
    await metric_registry.advance(
        METRIC_ID, "validated", now="2026-09-03T00:00:00Z",
        validation_evidence={"schema": True, "fixture": True, "evidence": True},
    )
    await metric_registry.advance(
        METRIC_ID, "published", now="2026-09-03T00:00:00Z",
    )


def _spec(metric_ids):
    return spec_for(
        {"math": ["item-0", "item-1"]},
        resample_count=9,
        comparison_family={
            "family_id": "primary",
            "comparisons": [comparison(
                baseline_arm="arm-evidence", candidate_arm="arm-evidence",
            )],
        },
        metric_ids=metric_ids,
    )


@pytest.mark.asyncio
async def test_legacy_serves_until_a_snapshot_exists(snapshot_db):  # noqa: F811
    served = await benchmark_routes.get_run_report_endpoint(RUN_ID)
    assert served["engine"] == "legacy-report"
    assert served["frozen_snapshot"] is None
    assert "freeze one" in served["statement"]
    assert served["analysis"]["estimand"]
    assert await frozen_analysis.served_report(RUN_ID) is None


@pytest.mark.asyncio
async def test_frozen_report_needs_published_definitions(snapshot_db):  # noqa: F811
    await frozen_analysis.freeze_and_store(
        RUN_ID, specification=_spec([METRIC_ID]), planned_repetitions=1,
    )
    with pytest.raises(HTTPException) as blocked:
        await benchmark_routes.get_run_report_endpoint(RUN_ID)
    assert blocked.value.status_code == 409
    assert "no registered definition" in blocked.value.detail

    tolerated = await benchmark_routes.get_run_report_endpoint(
        RUN_ID, allow_unresolved=True,
    )
    assert tolerated["engine"] == "bmas-frozen-analysis"
    assert tolerated["metrics"] == []
    assert tolerated["unresolved_metrics"][0]["metric_id"] == METRIC_ID

    await _publish_metric()
    served = await benchmark_routes.get_run_report_endpoint(RUN_ID)
    assert served["engine"] == "bmas-frozen-analysis"
    assert served["replay_verified"] is True
    assert served["unresolved_metrics"] == []
    (metric,) = served["metrics"]
    assert metric["metric_id"] == METRIC_ID
    assert metric["lifecycle_state"] == "published"
    assert metric["measurement"]["direction"] == "higher_is_better"
    assert served["report"]["metric_ids"] == [METRIC_ID]
    assert served["analysis"]["replay_claim"] == "analysis_replayable"
    assert served["denominators"]["planned"] == 2
    assert isinstance(served["comparisons"], list)
    assert served["results_digest"] == served["stored_results_digest"]

    legacy = await benchmark_routes.get_run_report_endpoint(RUN_ID, engine="legacy")
    assert legacy["engine"] == "legacy-report"
    assert "requested the legacy engine" in legacy["statement"]
    with pytest.raises(HTTPException) as rejected:
        await benchmark_routes.get_run_report_endpoint(RUN_ID, engine="abacus")
    assert rejected.value.status_code == 422


@pytest.mark.asyncio
async def test_a_specification_without_metrics_blocks_the_report(snapshot_db):  # noqa: F811
    await frozen_analysis.freeze_and_store(
        RUN_ID, specification=_stored_spec(), planned_repetitions=1,
    )
    with pytest.raises(metric_registry.MetricRegistryError, match="declares no metric"):
        await frozen_analysis.served_report(RUN_ID)


@pytest.mark.asyncio
async def test_a_superseded_snapshot_never_serves(snapshot_db):  # noqa: F811
    await _publish_metric()
    first = await frozen_analysis.freeze_and_store(
        RUN_ID, specification=_spec([METRIC_ID]), planned_repetitions=1,
    )
    replacement = await frozen_analysis.recompute_snapshot(
        first["snapshot_id"], ledger_summary=None, reason="manual",
    )
    current = await frozen_analysis.current_snapshot(RUN_ID)
    assert current["id"] == replacement["snapshot_id"]
    served = await frozen_analysis.served_report(RUN_ID)
    assert served["snapshot_id"] == replacement["snapshot_id"]


@pytest.mark.asyncio
async def test_the_metric_listing_reports_every_lifecycle_state(snapshot_db):  # noqa: F811
    from routes import evaluation as evaluation_routes

    assert await evaluation_routes.list_metrics_endpoint() == {"metrics": []}
    await _publish_metric()
    listing = await evaluation_routes.list_metrics_endpoint()
    (entry,) = listing["metrics"]
    assert entry["metric_id"] == METRIC_ID
    assert entry["lifecycle_state"] == "published"
    assert entry["calibration_state"] == "current"
    assert entry["record"]["scorer"]["scorer_id"] == "scorer-exact-match"
    assert await evaluation_routes.list_metrics_endpoint(lifecycle_state="draft") == {
        "metrics": [],
    }
