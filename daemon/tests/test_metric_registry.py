"""Metric definition lifecycle, calibration states, and privacy gates.

Only a published definition appears in a report, publication makes
the version immutable, deprecation blocks new run plans but keeps
earlier reports readable, withdrawal identifies affected reports
without rewriting them, calibration moves through current, due,
expired, and failed, an expired or failed calibration blocks a new
terminal gate, every required contract field gates publication, and
the joint privacy gate never lets zero disclosure pass when the task
loses its required facts.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_metric_definition

import database as db
from benchmarks import metric_registry
from benchmarks.metric_registry import (
    PRIVACY_METRIC_IDS,
    MetricRegistryError,
    assert_report_metric,
    assert_run_plan_metric,
    assert_terminal_gate_allowed,
    calibration_state,
    joint_privacy_gate,
    privacy_metric_definitions,
    resolve_display_metric,
    semantic_expiry,
    transition,
    validate_for_publication,
    withdrawal_impact,
)

NOW = "2026-09-02T00:00:00Z"
CHECKS = {"schema": True, "fixture": True, "evidence": True}


def complete_definition(**overrides) -> dict:
    definition = valid_metric_definition()
    definition["calibration"] = {
        "state": "current",
        "dataset": "labels-alpha",
        "method": "semantic",
        "result": {"raw_agreement": 0.92, "limits_failed": False},
        "version": "1",
        "calibrated_at": "2026-09-01T00:00:00Z",
        "expires_at": semantic_expiry("2026-09-01T00:00:00Z"),
        "drift_policy": "score_shift_0.05",
    }
    definition.update(overrides)
    return definition


def publish(definition: dict) -> dict:
    validated = transition(definition, "validated", now=NOW,
                           validation_evidence=CHECKS)
    return transition(validated, "published", now=NOW)


# ── Publication requirements ─────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "labels.source", "labels.evidence_contract", "scorer.version",
    "scorer.configuration_digest", "missingness", "uncertainty_method",
    "calibration.dataset", "calibration.result", "calibration.version",
    "calibration.calibrated_at", "calibration.expires_at",
    "calibration.drift_policy", "measurement.numerator",
    "measurement.denominator", "population.inclusion_rule",
])
def test_publication_rejects_each_missing_required_field(path):
    definition = complete_definition()
    container, field = path.split(".") if "." in path else (None, path)
    if container:
        definition[container] = {**definition[container]}
        definition[container][field] = "" if field != "result" else {}
        if field == "evidence_contract":
            definition[container][field] = []
    else:
        definition[field] = ""
    with pytest.raises(MetricRegistryError, match=path):
        validate_for_publication(definition)


def test_complete_definition_publishes():
    published = publish(complete_definition())
    assert published["lifecycle_state"] == "published"
    assert published["calibration"]["state"] == "current"


# ── Lifecycle transitions ────────────────────────────────────────────


def test_lifecycle_follows_the_declared_transitions():
    definition = complete_definition()
    with pytest.raises(MetricRegistryError, match="never moves"):
        transition(definition, "published", now=NOW)
    with pytest.raises(MetricRegistryError, match="passing checks"):
        transition(definition, "validated", now=NOW,
                   validation_evidence={"schema": True})
    published = publish(definition)
    with pytest.raises(MetricRegistryError, match="never moves"):
        transition(published, "draft", now=NOW)
    with pytest.raises(MetricRegistryError, match="reason"):
        transition(published, "deprecated", now=NOW)
    deprecated = transition(published, "deprecated", now=NOW,
                            reason="replaced by a calibrated version")
    with pytest.raises(MetricRegistryError, match="never moves"):
        transition(deprecated, "published", now=NOW)


def test_only_published_definitions_enter_reports_and_plans():
    definition = complete_definition()
    with pytest.raises(MetricRegistryError, match="only a published"):
        assert_report_metric(definition)
    published = publish(definition)
    assert_report_metric(published)
    assert_run_plan_metric(published)
    deprecated = transition(published, "deprecated", now=NOW,
                            reason="superseded")
    with pytest.raises(MetricRegistryError, match="cannot enter a new run"):
        assert_run_plan_metric(deprecated)
    # A deprecated definition stays readable for earlier reports.
    assert deprecated["measurement"]["numerator"]


def test_withdrawal_identifies_reports_without_rewriting():
    impact = withdrawal_impact("metric-task-success", [
        {"report_id": "report-a", "metric_ids": ["metric-task-success"]},
        {"report_id": "report-b", "metric_ids": ["metric-other"]},
    ])
    assert impact["affected_report_ids"] == ["report-a"]
    assert impact["reports_rewritten"] == 0


def test_displayed_metrics_resolve_to_one_immutable_definition():
    published = publish(complete_definition())
    resolved = resolve_display_metric(
        "metric-task-success", {"metric-task-success": published},
    )
    assert resolved["calibration_version"] == "1"
    assert resolved["scorer"]["version"] == "2"
    with pytest.raises(MetricRegistryError, match="no registered"):
        resolve_display_metric("metric-unknown", {})


# ── Calibration states ───────────────────────────────────────────────


def test_semantic_calibration_moves_through_every_state():
    definition = complete_definition()
    assert calibration_state(definition, now="2026-09-10T00:00:00Z") == (
        "current"
    )
    assert calibration_state(definition, now="2026-11-20T00:00:00Z") == "due"
    assert calibration_state(definition, now="2026-12-15T00:00:00Z") == (
        "expired"
    )
    failed = complete_definition()
    failed["calibration"]["result"] = {"limits_failed": True}
    assert calibration_state(failed, now=NOW) == "failed"


def test_deterministic_calibration_expires_on_digest_change():
    definition = complete_definition()
    definition["calibration"]["method"] = "deterministic"
    definition["calibration"]["result"] = {
        "pinned_digests": {"implementation": "a" * 64},
        "limits_failed": False,
    }
    assert calibration_state(definition, now=NOW) == "current"
    assert calibration_state(
        definition, now=NOW, current_digests={"implementation": "b" * 64},
    ) == "expired"


def test_expired_or_failed_calibration_blocks_a_new_terminal_gate():
    published = publish(complete_definition())
    assert_terminal_gate_allowed([published], now="2026-09-10T00:00:00Z")
    with pytest.raises(MetricRegistryError, match="expired"):
        assert_terminal_gate_allowed([published], now="2027-01-01T00:00:00Z")
    with pytest.raises(MetricRegistryError, match="current calibration"):
        transition(
            transition(complete_definition(), "validated", now=NOW,
                       validation_evidence=CHECKS),
            "published", now="2027-01-01T00:00:00Z",
        )


# ── Stored lifecycle through the facade ──────────────────────────────


@pytest_asyncio.fixture
async def metric_db(tmp_path, monkeypatch):
    path = str(tmp_path / "metrics.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return path


@pytest.mark.asyncio
async def test_stored_definition_publishes_and_freezes(metric_db):
    import aiosqlite

    from benchmarks import evaluation_records, facade

    definition = complete_definition()
    definition["calibration"]["state"] = "due"
    await facade.execute("register_metric_definition",
                         {"record": definition})
    await metric_registry.advance(
        "metric-task-success", "validated", now=NOW,
        validation_evidence=CHECKS,
    )
    published = await metric_registry.advance(
        "metric-task-success", "published", now=NOW,
    )
    assert published["lifecycle_state"] == "published"
    stored = await evaluation_records.get_record(
        "metric-definition", "metric-task-success",
    )
    assert stored["lifecycle_state"] == "published"
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE metric_definitions SET record = '{}' WHERE id = ?",
                ("metric-task-success",),
            )
    with pytest.raises(MetricRegistryError, match="does not exist"):
        await metric_registry.advance("metric-missing", "validated", now=NOW)


# ── Privacy definitions and the joint gate ───────────────────────────


def test_privacy_definitions_publish_together():
    definitions = privacy_metric_definitions(
        scorer_version="2", configuration_digest="c" * 64,
        calibrated_at="2026-09-01T00:00:00Z",
    )
    assert [d["metric_id"] for d in definitions] == list(PRIVACY_METRIC_IDS)
    for definition in definitions:
        assert_report_metric(definition)
        assert definition["labels"]["source"] == "blinded_human_labels"
    directions = {
        d["metric_id"]: d["measurement"]["direction"] for d in definitions
    }
    assert directions["metric-privacy-disclosure"] == "lower_is_better"
    assert directions["metric-necessary-fact-retention"] == "higher_is_better"


def test_joint_gate_requires_safety_and_utility_together():
    passed = joint_privacy_gate(
        disclosure_rate=0.0, constrained_success=0.8, fact_retention=0.95,
    )
    assert passed["status"] == "passed"
    # Zero disclosure never passes when required facts are lost.
    unusable = joint_privacy_gate(
        disclosure_rate=0.0, constrained_success=0.1, fact_retention=0.2,
    )
    assert unusable["status"] == "failed"
    assert unusable["checks"]["disclosure_safe"] is True
    assert unusable["checks"]["facts_retained"] is False
    leaky = joint_privacy_gate(
        disclosure_rate=0.2, constrained_success=0.9, fact_retention=1.0,
    )
    assert leaky["status"] == "failed"
