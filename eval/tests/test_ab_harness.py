"""Tests for the A/B benchmark report."""

from eval.ab_harness import _build_report
from eval.metrics import RunMetrics


def _metrics(run_id: str, completion_rate: float) -> RunMetrics:
    return RunMetrics(
        run_id=run_id,
        dataset="gsm8k",
        dataset_size=4,
        started_at="2026-08-18T00:00:00Z",
        completed_at="2026-08-18T00:01:00Z",
        accuracy=0.5,
        completed_tasks=int(completion_rate * 4),
        completion_rate=completion_rate,
        accuracy_on_completed=0.5,
        cost_observations=3,
        token_observations=2,
        latency_observations=4,
        round_observations=1,
    )


def test_report_shows_completion_and_metric_coverage():
    report = _build_report(
        "traditional-a",
        _metrics("a", 0.75),
        "traditional-b",
        _metrics("b", 1.0),
        "comparison",
    )

    assert "| Completion Rate | 0.7500 | 1.0000 |" in report
    assert "| Accuracy on Completed | 0.5000 | 0.5000 |" in report
    assert "## Metric Coverage" in report
    assert "| Cost | 3/4 | 3/4 |" in report
    assert "| Tokens | 2/4 | 2/4 |" in report
    assert "| Latency | 4/4 | 4/4 |" in report
    assert "| Rounds | 1/4 | 1/4 |" in report
