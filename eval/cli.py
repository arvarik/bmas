"""bMAS Evaluation CLI — benchmark, A/B, and failure-injection tooling.

Usage:
  python -m eval.cli benchmark --dataset gsm8k --limit 10
  python -m eval.cli ab --dataset gsm8k --variant-a traditional --variant-b patchboard
  python -m eval.cli report --file-a results/run_a_summary.json --file-b results/run_b_summary.json
  python -m eval.cli inject-failure --node node-1 --mode kill --confirm-destructive

See docs/proposals/10-migration-and-rollout.md Phase E.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eval.config import load_eval_config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bmas-eval",
        description="bMAS Evaluation, A/B & Showcase Instrumentation (Phase E)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── benchmark ────────────────────────────────────────────────────
    bench = subparsers.add_parser(
        "benchmark", help="Run a benchmark dataset through bMAS and score accuracy"
    )
    bench.add_argument(
        "--dataset",
        choices=["gsm8k", "mmlu"],
        required=True,
        help="Dataset to evaluate",
    )
    bench.add_argument(
        "--limit", type=int, default=None, help="Cap number of items (for smoke tests)"
    )
    bench.add_argument(
        "--concurrency", type=int, default=1, help="Concurrent task submissions"
    )
    bench.add_argument(
        "--run-id", type=str, default=None, help="Run identifier (auto-generated if omitted)"
    )
    bench.add_argument(
        "--config", type=str, default=None, help="Path to bmas.yaml (auto-detected if omitted)"
    )

    # ── ab ────────────────────────────────────────────────────────────
    ab = subparsers.add_parser(
        "ab", help="Run A/B comparison between two coordination variants"
    )
    ab.add_argument(
        "--dataset",
        choices=["gsm8k", "mmlu"],
        required=True,
        help="Dataset to evaluate",
    )
    ab.add_argument(
        "--variant-a", type=str, default="traditional", help="First variant"
    )
    ab.add_argument(
        "--variant-b", type=str, default="traditional", help="Second variant"
    )
    ab.add_argument("--limit", type=int, default=None)
    ab.add_argument("--concurrency", type=int, default=1)
    ab.add_argument("--run-id", type=str, default=None)
    ab.add_argument("--config", type=str, default=None)

    # ── report ────────────────────────────────────────────────────────
    rpt = subparsers.add_parser(
        "report", help="Generate side-by-side report from two summary files"
    )
    rpt.add_argument("--file-a", required=True, help="Path to variant A summary JSON")
    rpt.add_argument("--file-b", required=True, help="Path to variant B summary JSON")
    rpt.add_argument(
        "--variant-a", type=str, default="variant_a", help="Label for variant A"
    )
    rpt.add_argument(
        "--variant-b", type=str, default="variant_b", help="Label for variant B"
    )

    # ── inject-failure ────────────────────────────────────────────────
    inj = subparsers.add_parser(
        "inject-failure", help="Inject failure into a cluster node (DESTRUCTIVE)"
    )
    inj.add_argument("--node", required=True, help="Node name from bmas.yaml")
    inj.add_argument(
        "--mode",
        choices=["kill", "partition"],
        required=True,
        help="kill=stop service, partition=firewall",
    )
    inj.add_argument("--task-id", type=str, default=None, help="Task to observe")
    inj.add_argument(
        "--heal-after-s",
        type=int,
        default=None,
        help="Auto-heal after N seconds (omit for manual heal)",
    )
    inj.add_argument("--config", type=str, default=None)
    inj.add_argument(
        "--confirm-destructive",
        action="store_true",
        help="REQUIRED: confirm this performs destructive operations on cluster nodes",
    )

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "benchmark":
        asyncio.run(_cmd_benchmark(args))
    elif args.command == "ab":
        asyncio.run(_cmd_ab(args))
    elif args.command == "report":
        _cmd_report(args)
    elif args.command == "inject-failure":
        _cmd_inject_failure(args)


async def _cmd_benchmark(args: argparse.Namespace) -> None:
    from eval.datasets import load_gsm8k, load_mmlu
    from eval.runner import BenchmarkRunner
    from eval.metrics import compute_run_metrics
    from eval.scorer import compute_accuracy, compute_accuracy_by_subject

    cfg = load_eval_config(args.config)

    # Load dataset
    if args.dataset == "gsm8k":
        items = load_gsm8k(limit=args.limit)
    else:
        items = load_mmlu(limit=args.limit)

    run_id = args.run_id or f"bench-{args.dataset}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    runner = BenchmarkRunner(
        daemon_url=cfg["daemon_url"],
        concurrency=args.concurrency,
    )
    try:
        scored = await runner.run(items, run_id=run_id)
    finally:
        await runner.close()

    metrics = compute_run_metrics(
        run_id=run_id,
        dataset=args.dataset,
        results=scored,
        run_config=cfg["coordination"],
    )
    out_path = metrics.save("eval/results")

    # Print summary
    print(f"\n{'='*60}")
    print(f"  Benchmark: {args.dataset} | Run: {run_id}")
    print(f"  Items: {metrics.dataset_size} | Accuracy: {metrics.accuracy:.2%}")
    print(f"  Cost: ${metrics.total_cost_usd:.4f} | Tokens: {metrics.total_tokens}")
    print(f"  Avg Latency: {metrics.avg_latency_ms:.0f}ms | P95: {metrics.p95_latency_ms:.0f}ms")
    if metrics.accuracy_by_subject:
        print(f"  Per-subject:")
        for subj, acc in metrics.accuracy_by_subject.items():
            print(f"    {subj}: {acc:.2%}")
    print(f"  Summary saved: {out_path}")
    print(f"{'='*60}")


async def _cmd_ab(args: argparse.Namespace) -> None:
    from eval.datasets import load_gsm8k, load_mmlu
    from eval.ab_harness import ABHarness

    cfg = load_eval_config(args.config)

    if args.dataset == "gsm8k":
        items = load_gsm8k(limit=args.limit)
    else:
        items = load_mmlu(limit=args.limit)

    run_id = args.run_id or f"ab-{args.dataset}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    harness = ABHarness(
        daemon_url=cfg["daemon_url"],
        concurrency=args.concurrency,
    )

    # Arm A
    print(f"\n{'='*60}")
    print(f"  A/B Test — Arm A: {args.variant_a}")
    print(f"  Ensure daemon is running with coordination.variant: {args.variant_a}")
    print(f"{'='*60}\n")

    input("Press Enter when the daemon is ready for Arm A...")

    scored_a, metrics_a = await harness.run_arm(
        items=items,
        expected_variant=args.variant_a,
        run_id=f"{run_id}-a",
        run_config={**cfg["coordination"], "variant": args.variant_a},
    )

    # Arm B
    print(f"\n{'='*60}")
    print(f"  A/B Test — Arm B: {args.variant_b}")
    print(f"  Change coordination.variant to '{args.variant_b}' and restart daemon.")
    print(f"{'='*60}\n")

    input("Press Enter when the daemon is ready for Arm B...")

    scored_b, metrics_b = await harness.run_arm(
        items=items,
        expected_variant=args.variant_b,
        run_id=f"{run_id}-b",
        run_config={**cfg["coordination"], "variant": args.variant_b},
    )

    # Report
    report = harness.generate_report(
        variant_a=args.variant_a,
        metrics_a=metrics_a,
        variant_b=args.variant_b,
        metrics_b=metrics_b,
        run_id=run_id,
    )
    print(report)


def _cmd_report(args: argparse.Namespace) -> None:
    from eval.metrics import RunMetrics
    from eval.ab_harness import ABHarness

    metrics_a = RunMetrics.load(args.file_a)
    metrics_b = RunMetrics.load(args.file_b)

    harness = ABHarness(daemon_url="unused")
    report = harness.generate_report(
        variant_a=args.variant_a,
        metrics_a=metrics_a,
        variant_b=args.variant_b,
        metrics_b=metrics_b,
        run_id=f"report-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
    )
    print(report)


def _cmd_inject_failure(args: argparse.Namespace) -> None:
    import time as _time

    if not args.confirm_destructive:
        print(
            "❌ REFUSED: Failure injection performs DESTRUCTIVE operations.\n"
            "   Pass --confirm-destructive to proceed.",
            file=sys.stderr,
        )
        sys.exit(1)

    from eval.failure_injection import FailureInjector

    cfg = load_eval_config(args.config)

    injector = FailureInjector(
        nodes=cfg["nodes"],
        daemon_host=cfg["daemon_url"].split("//")[1].split(":")[0],
    )

    print(f"\n⚠️  DESTRUCTIVE: {args.mode} node '{args.node}'")
    if args.mode == "kill":
        event = injector.kill_node(args.node, task_id=args.task_id)
    else:
        event = injector.partition_node(args.node, task_id=args.task_id)

    print(f"  Result: {'✅ success' if event.success else '❌ failed'}")
    print(f"  Detail: {event.detail}")

    if args.heal_after_s is not None and event.success:
        print(f"  Healing in {args.heal_after_s}s...")
        _time.sleep(args.heal_after_s)
        heal = injector.heal_node(args.node, mode=args.mode)
        print(f"  Heal: {'✅ success' if heal.success else '❌ failed'}")
        print(f"  Detail: {heal.detail}")


if __name__ == "__main__":
    main()
