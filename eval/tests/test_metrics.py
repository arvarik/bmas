"""Tests for the RunMetrics computation and serialization.

Covers:
  - RunMetrics from scored results
  - Latency percentile calculation
  - terminated_by breakdown counting
  - JSON round-trip serialization
  - Edge cases (empty results, all-same latency)
"""

import json

import pytest

from eval.metrics import RunMetrics, _percentile, compute_run_metrics
from eval.scored_result import ScoredResult


class TestComputeRunMetrics:
    """Tests for compute_run_metrics."""

    def test_mixed_results(self, sample_scored_results):
        metrics = compute_run_metrics(
            run_id="test-001",
            dataset="mixed",
            results=sample_scored_results,
            run_config={"variant": "traditional"},
            started_at="2026-06-10T00:00:00Z",
            completed_at="2026-06-10T00:10:00Z",
        )
        assert metrics.run_id == "test-001"
        assert metrics.dataset == "mixed"
        assert metrics.dataset_size == 7
        assert metrics.accuracy == pytest.approx(5 / 7)
        assert metrics.total_cost_usd == pytest.approx(0.01 + 0.008 + 0.02 + 0.005 + 0.005 + 0.004 + 0.004)
        assert metrics.total_tokens == 500 + 400 + 800 + 200 + 250 + 180 + 190

    def test_accuracy_100(self, sample_scored_all_correct):
        metrics = compute_run_metrics(
            run_id="test-100",
            dataset="gsm8k",
            results=sample_scored_all_correct,
            run_config={"variant": "traditional"},
        )
        assert metrics.accuracy == 1.0
        assert metrics.dataset_size == 10

    def test_accuracy_0(self, sample_scored_all_wrong):
        metrics = compute_run_metrics(
            run_id="test-000",
            dataset="gsm8k",
            results=sample_scored_all_wrong,
            run_config={"variant": "traditional"},
        )
        assert metrics.accuracy == 0.0
        assert metrics.dataset_size == 10

    def test_empty_results(self):
        metrics = compute_run_metrics(
            run_id="test-empty",
            dataset="gsm8k",
            results=[],
            run_config={"variant": "traditional"},
        )
        assert metrics.accuracy == 0.0
        assert metrics.dataset_size == 0
        assert metrics.total_cost_usd == 0.0
        assert metrics.total_tokens == 0
        assert metrics.avg_latency_ms == 0.0
        assert metrics.completed_tasks == 0
        assert metrics.completion_rate == 0.0
        assert metrics.accuracy_on_completed == 0.0
        assert metrics.cost_observations == 0
        assert metrics.token_observations == 0
        assert metrics.latency_observations == 0
        assert metrics.round_observations == 0
        assert metrics.action_observations == 0
        assert metrics.recovery_observations == 0

    def test_partial_metric_coverage_does_not_count_missing_values_as_zero(self):
        results = [
            ScoredResult(
                id="complete-with-metrics", question="Q", expected_answer="1",
                actual_response="1", dataset="gsm8k", subject=None,
                extracted_answer="1", correct=True, score_method="numeric_match",
                status="completed", cost_usd=0.3, tokens=300,
                duration_ms=3000, rounds=3,
            ),
            ScoredResult(
                id="complete-without-metrics", question="Q", expected_answer="1",
                actual_response="2", dataset="gsm8k", subject=None,
                extracted_answer="2", correct=False, score_method="numeric_match",
                status="completed",
            ),
            ScoredResult(
                id="failed", question="Q", expected_answer="1",
                actual_response="", dataset="gsm8k", subject=None,
                extracted_answer=None, correct=False, score_method="task_failed",
                status="failed",
            ),
        ]

        metrics = compute_run_metrics(
            run_id="test-coverage",
            dataset="gsm8k",
            results=results,
            run_config={"variant": "traditional"},
        )

        assert metrics.accuracy == pytest.approx(1 / 3)
        assert metrics.completed_tasks == 2
        assert metrics.completion_rate == pytest.approx(2 / 3)
        assert metrics.accuracy_on_completed == pytest.approx(1 / 2)
        assert metrics.avg_cost_per_task_usd == pytest.approx(0.3)
        assert metrics.avg_tokens_per_task == pytest.approx(300)
        assert metrics.cost_observations == 1
        assert metrics.token_observations == 1
        assert metrics.latency_observations == 1
        assert metrics.round_observations == 1

    def test_latency_stats(self, sample_scored_results):
        metrics = compute_run_metrics(
            run_id="test-lat",
            dataset="mixed",
            results=sample_scored_results,
            run_config={},
        )
        # Latencies: 5000, 3000, 8000, 2000, 2500, 1500, 1800
        assert metrics.min_latency_ms == 1500
        assert metrics.max_latency_ms == 8000
        assert metrics.avg_latency_ms == pytest.approx(
            (5000 + 3000 + 8000 + 2000 + 2500 + 1500 + 1800) / 7
        )

    def test_terminated_by_breakdown(self, sample_scored_results):
        metrics = compute_run_metrics(
            run_id="test-term",
            dataset="mixed",
            results=sample_scored_results,
            run_config={},
        )
        # All have terminated_by="solution"
        assert metrics.terminated_by == {"solution": 7}

    def test_average_rounds_uses_recorded_classic_rounds(self):
        results = [
            ScoredResult(
                id=f"item-{rounds}", question="Q", expected_answer="1",
                actual_response="1", dataset="gsm8k", subject=None,
                extracted_answer="1", correct=True, score_method="numeric_match",
                rounds=rounds,
            )
            for rounds in (2, 4, 6)
        ]
        results.append(
            ScoredResult(
                id="legacy", question="Q", expected_answer="1",
                actual_response="1", dataset="gsm8k", subject=None,
                extracted_answer="1", correct=True, score_method="numeric_match",
                rounds=None,
            )
        )

        metrics = compute_run_metrics(
            run_id="test-rounds",
            dataset="gsm8k",
            results=results,
            run_config={"variant": "traditional"},
        )

        assert metrics.avg_rounds == 4.0

    def test_average_rounds_is_zero_without_recorded_rounds(
        self, sample_scored_results
    ):
        metrics = compute_run_metrics(
            run_id="test-no-rounds",
            dataset="mixed",
            results=sample_scored_results,
            run_config={"variant": "traditional"},
        )

        assert metrics.avg_rounds == 0.0

    def test_generic_lifecycle_metrics_do_not_require_rounds(self):
        """The summary measures work and recovery for every implementation."""
        results = [
            ScoredResult(
                id="one", question="Q", expected_answer="1",
                actual_response="1", dataset="gsm8k", subject=None,
                extracted_answer="1", correct=True, score_method="numeric_match",
                effective_actions=8, recovery_count=0, variant="classic",
                phase="completed",
            ),
            ScoredResult(
                id="two", question="Q", expected_answer="1",
                actual_response="1", dataset="gsm8k", subject=None,
                extracted_answer="1", correct=True, score_method="numeric_match",
                effective_actions=12, recovery_count=2, variant="classic",
                phase="completed",
            ),
        ]

        metrics = compute_run_metrics(
            run_id="generic", dataset="gsm8k", results=results, run_config={},
        )

        assert metrics.avg_effective_actions == 10
        assert metrics.action_observations == 2
        assert metrics.total_recoveries == 2
        assert metrics.recovery_observations == 2
        assert metrics.variants == {"classic": 2}
        assert metrics.terminal_phases == {"completed": 2}

    def test_subject_accuracy(self, sample_scored_results):
        metrics = compute_run_metrics(
            run_id="test-subj",
            dataset="mmlu",
            results=sample_scored_results,
            run_config={},
        )
        assert "abstract_algebra" in metrics.accuracy_by_subject
        assert metrics.accuracy_by_subject["abstract_algebra"] == pytest.approx(0.5)
        assert metrics.accuracy_by_subject["machine_learning"] == pytest.approx(1.0)

    def test_joules_estimate(self):
        metrics = compute_run_metrics(
            run_id="test-joules",
            dataset="gsm8k",
            results=[],
            run_config={},
            joules_estimate=42.5,
        )
        assert metrics.joules_estimate == 42.5

    def test_joules_none_by_default(self):
        metrics = compute_run_metrics(
            run_id="test-nj",
            dataset="gsm8k",
            results=[],
            run_config={},
        )
        assert metrics.joules_estimate is None


class TestPercentile:
    """Tests for the _percentile helper."""

    def test_empty(self):
        assert _percentile([], 95) == 0.0

    def test_single_value(self):
        assert _percentile([42], 95) == 42.0

    def test_ordered(self):
        data = list(range(1, 101))  # 1..100
        # Nearest rank 95 selects the 95th one-based item.
        assert _percentile(data, 95) == 95.0

    def test_bounds(self):
        data = [10, 20, 30]
        assert _percentile(data, 0) == 10.0
        assert _percentile(data, 100) == 30.0

    def test_p50(self):
        data = [1, 2, 3, 4, 5]
        assert _percentile(data, 50) == 3.0

    def test_unordered(self):
        data = [5, 1, 4, 2, 3]
        assert _percentile(data, 50) == 3.0  # should sort internally


class TestRunMetricsSerialization:
    """Tests for JSON round-trip and save/load."""

    def test_to_json_roundtrip(self, sample_scored_results):
        metrics = compute_run_metrics(
            run_id="test-json",
            dataset="mixed",
            results=sample_scored_results,
            run_config={"variant": "traditional", "max_rounds": 4},
        )
        json_str = metrics.to_json()
        parsed = json.loads(json_str)
        assert parsed["run_id"] == "test-json"
        assert parsed["dataset"] == "mixed"
        assert parsed["dataset_size"] == 7
        assert "accuracy" in parsed
        assert parsed["completion_rate"] == 1.0
        assert parsed["cost_observations"] == 7
        assert "run_config" in parsed
        assert parsed["run_config"]["variant"] == "traditional"

    def test_save_and_load(self, sample_scored_results, tmp_path):
        metrics = compute_run_metrics(
            run_id="test-save",
            dataset="gsm8k",
            results=sample_scored_results,
            run_config={"variant": "traditional"},
        )
        saved_path = metrics.save(tmp_path)
        assert saved_path.exists()

        loaded = RunMetrics.load(saved_path)
        assert loaded.run_id == "test-save"
        assert loaded.dataset == "gsm8k"
        assert loaded.accuracy == pytest.approx(metrics.accuracy)
        assert loaded.total_cost_usd == pytest.approx(metrics.total_cost_usd)

    def test_to_dict(self):
        metrics = RunMetrics(
            run_id="x", dataset="gsm8k", dataset_size=0,
            started_at="t0", completed_at="t1", accuracy=0.0,
        )
        d = metrics.to_dict()
        assert isinstance(d, dict)
        assert d["run_id"] == "x"
        assert d["accuracy"] == 0.0
