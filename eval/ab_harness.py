"""A/B harness — same dataset, swap only coordination.variant, emit side-by-side report.

See docs/proposals/10-migration-and-rollout.md Phase E bullet 3:
  "same dataset, swap only coordination.variant: legacy_pipeline vs traditional now;
   patchboard and stigmergic later. Emit a side-by-side report."

Design: the daemon reads coordination.variant at startup. The A/B harness therefore
runs each arm as a separate benchmark pass, verifying the daemon's active variant
between arms via GET /config/active.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.datasets import EvalItem
from eval.metrics import RunMetrics, compute_run_metrics
from eval.runner import BenchmarkRunner
from eval.scorer import ScoredResult, compute_accuracy, compute_accuracy_by_subject

logger = logging.getLogger("bmas.eval.ab")


class ABHarness:
    """Run the same dataset under two coordination variants and compare.

    Usage:
        harness = ABHarness(daemon_url="http://192.168.4.240:9000")
        report = await harness.run(
            items=dataset,
            variant_a="legacy_pipeline",
            variant_b="traditional",
        )
    """

    def __init__(
        self,
        daemon_url: str,
        concurrency: int = 1,
        results_dir: str | Path = "eval/results",
    ):
        self.daemon_url = daemon_url
        self.concurrency = concurrency
        self.results_dir = Path(results_dir)

    async def run_arm(
        self,
        items: list[EvalItem],
        expected_variant: str,
        run_id: str,
        run_config: dict[str, Any],
    ) -> tuple[list[ScoredResult], RunMetrics]:
        """Run one arm of the A/B test.

        Verifies that the daemon's active variant matches expected_variant
        before proceeding.
        """
        runner = BenchmarkRunner(
            daemon_url=self.daemon_url,
            concurrency=self.concurrency,
        )

        try:
            # Pre-flight: verify active variant
            config = await runner.verify_daemon()
            active = config.get("variant", "unknown")
            if active != expected_variant:
                raise RuntimeError(
                    f"Daemon variant mismatch: expected '{expected_variant}', "
                    f"got '{active}'. Restart the daemon with the correct "
                    f"coordination.variant in bmas.yaml."
                )
            logger.info(
                "A/B arm '%s' verified — daemon running variant '%s'",
                run_id, active,
            )

            # Run benchmark
            started = datetime.now(timezone.utc).isoformat()
            scored = await runner.run(items, run_id=run_id, results_dir=self.results_dir)
            completed = datetime.now(timezone.utc).isoformat()

            # Compute metrics
            metrics = compute_run_metrics(
                run_id=run_id,
                dataset=items[0].dataset if items else "unknown",
                results=scored,
                run_config=run_config,
                started_at=started,
                completed_at=completed,
            )
            metrics.save(self.results_dir)

            return scored, metrics

        finally:
            await runner.close()

    def generate_report(
        self,
        variant_a: str,
        metrics_a: RunMetrics,
        variant_b: str,
        metrics_b: RunMetrics,
        run_id: str,
    ) -> str:
        """Generate a side-by-side markdown report comparing two arms.

        Returns the markdown string and saves to results_dir/ab_{run_id}.md.
        """
        report = _build_report(variant_a, metrics_a, variant_b, metrics_b, run_id)

        out_path = self.results_dir / f"ab_{run_id}.md"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report)
        logger.info("A/B report saved: %s", out_path)

        return report


def _build_report(
    variant_a: str,
    a: RunMetrics,
    variant_b: str,
    b: RunMetrics,
    run_id: str,
) -> str:
    """Build the side-by-side comparison markdown."""

    def _delta(va: float, vb: float) -> str:
        diff = vb - va
        if va == 0:
            return "N/A"
        pct = (diff / va) * 100
        sign = "+" if diff >= 0 else ""
        return f"{sign}{diff:.4f} ({sign}{pct:.1f}%)"

    def _delta_int(va: int, vb: int) -> str:
        diff = vb - va
        if va == 0:
            return "N/A"
        pct = (diff / va) * 100
        sign = "+" if diff >= 0 else ""
        return f"{sign}{diff} ({sign}{pct:.1f}%)"

    lines = [
        f"# A/B Comparison Report — {run_id}",
        "",
        f"**Variant A:** `{variant_a}` | **Variant B:** `{variant_b}`",
        f"**Dataset:** {a.dataset} ({a.dataset_size} items)",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | Variant A | Variant B | Delta |",
        "|:-------|----------:|----------:|------:|",
        f"| Accuracy | {a.accuracy:.4f} | {b.accuracy:.4f} | {_delta(a.accuracy, b.accuracy)} |",
        f"| Total Cost ($) | {a.total_cost_usd:.6f} | {b.total_cost_usd:.6f} | {_delta(a.total_cost_usd, b.total_cost_usd)} |",
        f"| Total Tokens | {a.total_tokens} | {b.total_tokens} | {_delta_int(a.total_tokens, b.total_tokens)} |",
        f"| Avg Latency (ms) | {a.avg_latency_ms:.0f} | {b.avg_latency_ms:.0f} | {_delta(a.avg_latency_ms, b.avg_latency_ms)} |",
        f"| Median Latency (ms) | {a.median_latency_ms:.0f} | {b.median_latency_ms:.0f} | {_delta(a.median_latency_ms, b.median_latency_ms)} |",
        f"| P95 Latency (ms) | {a.p95_latency_ms:.0f} | {b.p95_latency_ms:.0f} | {_delta(a.p95_latency_ms, b.p95_latency_ms)} |",
        f"| Avg Cost/Task ($) | {a.avg_cost_per_task_usd:.6f} | {b.avg_cost_per_task_usd:.6f} | {_delta(a.avg_cost_per_task_usd, b.avg_cost_per_task_usd)} |",
        f"| Avg Rounds | {a.avg_rounds:.1f} | {b.avg_rounds:.1f} | {_delta(a.avg_rounds, b.avg_rounds)} |",
        "",
    ]

    # Subject breakdown (MMLU)
    all_subjects = sorted(
        set(list(a.accuracy_by_subject.keys()) + list(b.accuracy_by_subject.keys()))
    )
    if all_subjects:
        lines.extend([
            "## Per-Subject Accuracy (MMLU)",
            "",
            "| Subject | Variant A | Variant B | Delta |",
            "|:--------|----------:|----------:|------:|",
        ])
        for subj in all_subjects:
            sa = a.accuracy_by_subject.get(subj, 0.0)
            sb = b.accuracy_by_subject.get(subj, 0.0)
            lines.append(
                f"| {subj} | {sa:.4f} | {sb:.4f} | {_delta(sa, sb)} |"
            )
        lines.append("")

    # Termination breakdown
    all_reasons = sorted(
        set(list(a.terminated_by.keys()) + list(b.terminated_by.keys()))
    )
    if all_reasons:
        lines.extend([
            "## Termination Breakdown",
            "",
            "| Reason | Variant A | Variant B |",
            "|:-------|----------:|----------:|",
        ])
        for reason in all_reasons:
            ca = a.terminated_by.get(reason, 0)
            cb = b.terminated_by.get(reason, 0)
            lines.append(f"| {reason} | {ca} | {cb} |")
        lines.append("")

    # Statistical note
    lines.extend([
        "## Notes",
        "",
        f"> N={a.dataset_size}. No statistical significance test applied at this "
        f"sample size. Results are directional, not conclusive.",
        "",
        f"> Energy estimate: "
        f"{'not captured' if a.joules_estimate is None else f'{a.joules_estimate:.1f}J'} (A) / "
        f"{'not captured' if b.joules_estimate is None else f'{b.joules_estimate:.1f}J'} (B)",
    ])

    return "\n".join(lines) + "\n"
