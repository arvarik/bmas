"""Per-run metrics capture — the table that goes in the writeup.

See docs/proposals/10-migration-and-rollout.md Phase E bullet 2:
  accuracy, tokens, $, latency, rounds-to-termination,
  terminated-by breakdown, and joules_estimate.

One row per task; one summary per benchmark.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.scorer import ScoredResult


@dataclass
class RunMetrics:
    """Summary metrics for a single benchmark run."""

    # Identity
    run_id: str
    dataset: str
    dataset_size: int
    started_at: str
    completed_at: str

    # Accuracy
    accuracy: float
    accuracy_by_subject: dict[str, float] = field(default_factory=dict)

    # Cost
    total_cost_usd: float = 0.0
    avg_cost_per_task_usd: float = 0.0

    # Tokens
    total_tokens: int = 0
    avg_tokens_per_task: float = 0.0

    # Latency
    avg_latency_ms: float = 0.0
    median_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    min_latency_ms: float = 0.0
    max_latency_ms: float = 0.0

    # Termination
    avg_rounds: float = 0.0
    terminated_by: dict[str, int] = field(default_factory=dict)

    # Energy (Phase 1 hook — nullable until wired)
    joules_estimate: float | None = None

    # Config snapshot for reproducibility
    run_config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def save(self, results_dir: str | Path) -> Path:
        """Save summary JSON to results_dir/{run_id}_summary.json."""
        out_dir = Path(results_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{self.run_id}_summary.json"
        out_path.write_text(self.to_json())
        return out_path

    @classmethod
    def load(cls, path: str | Path) -> "RunMetrics":
        """Load a RunMetrics from a saved JSON file."""
        data = json.loads(Path(path).read_text())
        return cls(**data)


def compute_run_metrics(
    run_id: str,
    dataset: str,
    results: list[ScoredResult],
    run_config: dict[str, Any],
    started_at: str | None = None,
    completed_at: str | None = None,
    joules_estimate: float | None = None,
) -> RunMetrics:
    """Compute aggregate RunMetrics from a list of scored results.

    This is the primary entry point for metrics capture after scoring.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    started_at = started_at or now_iso
    completed_at = completed_at or now_iso

    dataset_size = len(results)
    if dataset_size == 0:
        return RunMetrics(
            run_id=run_id,
            dataset=dataset,
            dataset_size=0,
            started_at=started_at,
            completed_at=completed_at,
            accuracy=0.0,
            run_config=run_config,
            joules_estimate=joules_estimate,
        )

    # Accuracy
    correct_count = sum(1 for r in results if r.correct)
    accuracy = correct_count / dataset_size

    # Accuracy by subject
    accuracy_by_subject: dict[str, float] = {}
    by_subj: dict[str, list[bool]] = {}
    for r in results:
        if r.subject:
            by_subj.setdefault(r.subject, []).append(r.correct)
    for subj, flags in sorted(by_subj.items()):
        accuracy_by_subject[subj] = sum(flags) / len(flags) if flags else 0.0

    # Cost
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    total_cost = sum(costs)
    avg_cost = total_cost / dataset_size if dataset_size > 0 else 0.0

    # Tokens
    token_counts = [r.tokens for r in results if r.tokens is not None]
    total_tokens = sum(token_counts)
    avg_tokens = total_tokens / dataset_size if dataset_size > 0 else 0.0

    # Latency
    latencies = [r.duration_ms for r in results if r.duration_ms is not None]
    if latencies:
        avg_latency = statistics.mean(latencies)
        median_latency = statistics.median(latencies)
        p95_latency = _percentile(latencies, 95)
        min_latency = min(latencies)
        max_latency = max(latencies)
    else:
        avg_latency = median_latency = p95_latency = min_latency = max_latency = 0.0

    # Termination breakdown
    terminated_by: dict[str, int] = {}
    for r in results:
        reason = r.terminated_by or "unknown"
        terminated_by[reason] = terminated_by.get(reason, 0) + 1

    # Rounds
    # Future: parse from task metadata when traditional variant populates it
    avg_rounds = 1.0

    return RunMetrics(
        run_id=run_id,
        dataset=dataset,
        dataset_size=dataset_size,
        started_at=started_at,
        completed_at=completed_at,
        accuracy=accuracy,
        accuracy_by_subject=accuracy_by_subject,
        total_cost_usd=total_cost,
        avg_cost_per_task_usd=avg_cost,
        total_tokens=total_tokens,
        avg_tokens_per_task=avg_tokens,
        avg_latency_ms=avg_latency,
        median_latency_ms=median_latency,
        p95_latency_ms=p95_latency,
        min_latency_ms=min_latency,
        max_latency_ms=max_latency,
        avg_rounds=avg_rounds,
        terminated_by=terminated_by,
        joules_estimate=joules_estimate,
        run_config=run_config,
    )


def _percentile(data: list[float | int], pct: float) -> float:
    """Compute the pct-th percentile of a sorted list.

    Uses the nearest-rank method.
    """
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = max(0, min(len(sorted_data) - 1, int(len(sorted_data) * pct / 100)))
    return float(sorted_data[k])
