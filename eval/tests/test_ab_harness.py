"""Tests for the capability-driven A/B benchmark harness."""

import pytest

from eval.ab_harness import ABHarness, _build_report
from eval.datasets import EvalItem
from eval.metrics import RunMetrics
from eval.scorer import ScoredResult


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
    assert "| Effective Actions | 0/4 | 0/4 |" in report


@pytest.mark.asyncio
async def test_arm_selects_an_available_variant_per_task(monkeypatch, tmp_path):
    """The harness does not require a daemon restart between comparison arms."""
    created: list[object] = []

    class FakeRunner:
        def __init__(self, **kwargs):
            self.variant = kwargs["variant"]
            self.closed = False
            created.append(self)

        async def verify_daemon(self):
            return {
                "api_version": "1",
                "variants": [{
                    "id": "classic",
                    "available": True,
                    "aliases": ["traditional"],
                }],
            }

        async def run(self, items, **_kwargs):
            item = items[0]
            return [ScoredResult(
                id=item.id,
                question=item.question,
                expected_answer=item.answer,
                actual_response=item.answer,
                dataset=item.dataset,
                subject=item.subject,
                extracted_answer=item.answer,
                correct=True,
                score_method="numeric_match",
                status="completed",
                variant="classic",
            )]

        async def close(self):
            self.closed = True

    monkeypatch.setattr("eval.ab_harness.BenchmarkRunner", FakeRunner)
    harness = ABHarness(daemon_url="http://daemon", results_dir=tmp_path)
    item = EvalItem(
        id="one", question="One?", answer="1", dataset="gsm8k",
    )

    results, metrics = await harness.run_arm(
        items=[item],
        expected_variant="traditional",
        run_id="arm",
        run_config={},
    )

    assert created[0].variant == "traditional"
    assert created[0].closed is True
    assert results[0].correct is True
    assert metrics.variants == {"classic": 1}


@pytest.mark.asyncio
async def test_arm_rejects_an_unavailable_variant(monkeypatch, tmp_path):
    """The harness fails before submission when the daemon lacks a variant."""
    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        async def verify_daemon(self):
            return {"api_version": "1", "variants": []}

        async def close(self):
            pass

    monkeypatch.setattr("eval.ab_harness.BenchmarkRunner", FakeRunner)
    harness = ABHarness(daemon_url="http://daemon", results_dir=tmp_path)

    with pytest.raises(RuntimeError, match="does not offer"):
        await harness.run_arm(
            items=[],
            expected_variant="missing",
            run_id="arm",
            run_config={},
        )
