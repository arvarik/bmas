"""Tests for the classic long-horizon soak harness."""

from __future__ import annotations

import json
import random

import pytest

from eval.soak import (
    HORIZON_BANDS,
    RoleMeasurement,
    SoakHarness,
    TrialOutcome,
    TrialRecord,
    TrialSpec,
    compute_soak_metrics,
)


def _record(horizon: int, repetition: int, outcome: TrialOutcome) -> TrialRecord:
    return TrialRecord(
        spec=TrialSpec(
            configuration="classic",
            settings={"round_execution": "concurrent"},
            horizon=horizon,
            repetition=repetition,
            seed=repetition,
        ),
        outcome=outcome,
    )


def test_soak_requires_ten_repetitions_per_configuration():
    with pytest.raises(ValueError, match="at least 10"):
        SoakHarness(repetitions=9)


@pytest.mark.asyncio
async def test_soak_runs_all_horizon_bands_ten_times(tmp_path):
    harness = SoakHarness(repetitions=10, concurrency=8, base_seed=71)

    async def driver(spec: TrialSpec) -> TrialOutcome:
        rng = random.Random(spec.seed)
        return TrialOutcome(
            effective_actions=spec.horizon,
            exact_success=rng.random() >= 0.0,
            completed=True,
            restart_attempted=spec.repetition == 0,
            restart_recovered=spec.repetition == 0,
            external_action_keys=[
                f"{spec.configuration}:{spec.horizon}:{index}"
                for index in range(spec.horizon)
            ],
            budget_limit_usd=1.0,
            budget_spent_usd=spec.horizon / 1000,
            retrieval_expected=5,
            retrieval_found=5,
            role_measurements=[
                RoleMeasurement("expert", 0.001, rng.uniform(1.0, 2.0))
            ],
            minority_opportunities=1,
            minority_corrections=1,
        )

    report = await harness.run(
        {"classic": {"round_execution": "concurrent"}},
        driver,
    )

    assert report.horizon_bands == HORIZON_BANDS
    assert len(report.records) == len(HORIZON_BANDS) * 10
    assert len(report.metrics) == len(HORIZON_BANDS)
    assert {metric.horizon for metric in report.metrics} == set(HORIZON_BANDS)
    assert all(metric.trials == 10 for metric in report.metrics)
    assert all(metric.strict_repeated_run_success for metric in report.metrics)
    assert len({record.spec.seed for record in report.records}) == len(
        report.records
    )

    output = report.save(tmp_path / "soak.json")
    saved = json.loads(output.read_text())
    assert saved["horizon_bands"] == list(HORIZON_BANDS)
    assert len(saved["records"]) == 60


def test_soak_metrics_cover_failures_and_long_horizon_decay():
    records = [
        _record(
            1,
            repetition,
            TrialOutcome(
                effective_actions=1,
                exact_success=True,
                completed=True,
            ),
        )
        for repetition in range(10)
    ]

    for repetition in range(10):
        records.append(_record(
            5,
            repetition,
            TrialOutcome(
                effective_actions=5,
                exact_success=repetition != 0,
                completed=True,
                restart_attempted=repetition < 2,
                restart_recovered=repetition == 0,
                external_action_keys=(
                    ["tool-1", "tool-1"] if repetition == 0 else ["tool-1"]
                ),
                budget_limit_usd=1.0,
                budget_spent_usd=1.25 if repetition == 0 else 0.5,
                retrieval_expected=2,
                retrieval_found=1 if repetition == 0 else 2,
                stall_count=1,
                replan_count=1 if repetition < 3 else 0,
                role_measurements=[
                    RoleMeasurement("critic", 0.02, 100 + repetition),
                ],
                unresolved_conflicts=1 if repetition == 0 else 0,
                minority_opportunities=1,
                minority_corrections=1 if repetition < 8 else 0,
            ),
        ))

    metrics = compute_soak_metrics(records)
    long_band = next(metric for metric in metrics if metric.horizon == 5)

    assert long_band.exact_task_success == pytest.approx(0.9)
    assert long_band.strict_repeated_run_success is False
    assert long_band.false_completion_rate == pytest.approx(0.1)
    assert long_band.reliability_decay == pytest.approx(0.1)
    assert long_band.restart_attempts == 2
    assert long_band.restart_recovery_rate == pytest.approx(0.5)
    assert long_band.duplicate_external_actions == 1
    assert long_band.budget_overshoot_rate == pytest.approx(0.1)
    assert long_band.budget_overshoot_usd == pytest.approx(0.25)
    assert long_band.context_retrieval_recall == pytest.approx(0.95)
    assert long_band.stall_count == 10
    assert long_band.replan_count == 3
    assert long_band.unresolved_conflict_count == 1
    assert long_band.minority_correction_rate == pytest.approx(0.8)
    assert long_band.average_effective_actions == 5
    assert long_band.role_metrics["critic"]["activations"] == 10
    assert long_band.role_metrics["critic"]["total_cost_usd"] == pytest.approx(
        0.2
    )
    assert long_band.role_metrics["critic"]["p95_latency_ms"] == 109


@pytest.mark.asyncio
async def test_soak_records_driver_errors_as_failed_trials():
    harness = SoakHarness(horizons=(1,), repetitions=10)

    async def driver(spec: TrialSpec) -> TrialOutcome:
        if spec.repetition == 3:
            raise RuntimeError("injected provider loss")
        return TrialOutcome(
            effective_actions=1,
            exact_success=True,
            completed=True,
        )

    report = await harness.run({"classic": {}}, driver)

    assert len(report.records) == 10
    assert sum(record.error is not None for record in report.records) == 1
    assert report.metrics[0].exact_task_success == pytest.approx(0.9)
    assert report.metrics[0].strict_repeated_run_success is False


@pytest.mark.asyncio
async def test_soak_rejects_invalid_measurements_without_stopping_other_trials():
    harness = SoakHarness(horizons=(1,), repetitions=10)

    async def driver(spec: TrialSpec) -> TrialOutcome:
        return TrialOutcome(
            effective_actions=1,
            exact_success=True,
            completed=True,
            retrieval_expected=1,
            retrieval_found=2 if spec.repetition == 0 else 1,
        )

    report = await harness.run({"classic": {}}, driver)

    assert report.records[0].error == (
        "ValueError: retrieval_found exceeds retrieval_expected"
    )
    assert report.metrics[0].exact_task_success == pytest.approx(0.9)
