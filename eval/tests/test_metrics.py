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
from pathlib import Path

from eval.metrics import RunMetrics, compute_run_metrics, _percentile
from eval.scorer import ScoredResult


class TestComputeRunMetrics:
    """Tests for compute_run_metrics."""

    def test_mixed_results(self, sample_scored_results):
        metrics = compute_run_metrics(
            run_id="test-001",
            dataset="mixed",
            results=sample_scored_results,
            run_config={"variant": "legacy_pipeline"},
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
            run_config={"variant": "legacy_pipeline"},
        )
        assert metrics.accuracy == 1.0
        assert metrics.dataset_size == 10

    def test_accuracy_0(self, sample_scored_all_wrong):
        metrics = compute_run_metrics(
            run_id="test-000",
            dataset="gsm8k",
            results=sample_scored_all_wrong,
            run_config={"variant": "legacy_pipeline"},
        )
        assert metrics.accuracy == 0.0
        assert metrics.dataset_size == 10

    def test_empty_results(self):
        metrics = compute_run_metrics(
            run_id="test-empty",
            dataset="gsm8k",
            results=[],
            run_config={"variant": "legacy_pipeline"},
        )
        assert metrics.accuracy == 0.0
        assert metrics.dataset_size == 0
        assert metrics.total_cost_usd == 0.0
        assert metrics.total_tokens == 0
        assert metrics.avg_latency_ms == 0.0

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
        # nearest-rank: index = int(100 * 95 / 100) = 95 → value 96
        assert _percentile(data, 95) == 96.0

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
            run_config={"variant": "legacy_pipeline", "max_rounds": 4},
        )
        json_str = metrics.to_json()
        parsed = json.loads(json_str)
        assert parsed["run_id"] == "test-json"
        assert parsed["dataset"] == "mixed"
        assert parsed["dataset_size"] == 7
        assert "accuracy" in parsed
        assert "run_config" in parsed
        assert parsed["run_config"]["variant"] == "legacy_pipeline"

    def test_save_and_load(self, sample_scored_results, tmp_path):
        metrics = compute_run_metrics(
            run_id="test-save",
            dataset="gsm8k",
            results=sample_scored_results,
            run_config={"variant": "legacy_pipeline"},
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
