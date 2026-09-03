"""bMAS Evaluation CLI — benchmark, A/B, and failure-injection tooling.

Usage:
  python -m eval.cli benchmark --dataset gsm8k --limit 10
  python -m eval.cli ab --dataset gsm8k --variant-a classic --variant-b candidate
  python -m eval.cli report --file-a results/run_a_summary.json --file-b results/run_b_summary.json
  python -m eval.cli inject-failure --node node-1 --mode kill --confirm-destructive
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
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
        "--variant-a", type=str, default="classic", help="First variant"
    )
    ab.add_argument(
        "--variant-b", type=str, default="classic", help="Second variant"
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

    # ── evaluation (the facade client commands) ──────────────────────
    evaluation = subparsers.add_parser(
        "evaluation",
        help="Call the versioned evaluation API as a client (no local writes)",
    )
    evaluation.add_argument("--daemon-url", type=str, default=None)
    evaluation.add_argument("--config", type=str, default=None)
    evaluation_commands = evaluation.add_subparsers(
        dest="evaluation_command", required=True,
    )
    evaluation_commands.add_parser(
        "authority", help="Show the migration authority and facade counters",
    )
    evaluation_commands.add_parser(
        "removal-gates", help="Show measured fallback, rollback, and retention evidence",
    )
    migrate = evaluation_commands.add_parser(
        "migrate-results", help="Migrate legacy summary files through the API",
    )
    migrate.add_argument("--results-dir", type=str, default="eval/results")
    export = evaluation_commands.add_parser(
        "export-bundle", help="Export one analysis-replay bundle to a local file",
    )
    export.add_argument("--run-id", required=True)
    export.add_argument("--policy", choices=["redacted", "complete"], default="redacted")
    export.add_argument("--output", type=str, default=None)
    replay = evaluation_commands.add_parser(
        "replay-bundle", help="Import one bundle and replay its analysis after approval",
    )
    replay.add_argument("--file", required=True)
    replay.add_argument("--actor", type=str, default=None)
    replay.add_argument("--policy-version", type=str, default="1")
    preview = evaluation_commands.add_parser(
        "score-preview", help="Score one response through the daemon boundary",
    )
    preview.add_argument("--plugin", type=str, default="deterministic")
    preview.add_argument("--comparison", type=str, default="last_number")
    preview.add_argument("--reference", required=True)
    preview.add_argument("--response", required=True)

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "benchmark":
        _deprecated_command("eval.cli.benchmark", args)
        asyncio.run(_cmd_benchmark(args))
    elif args.command == "ab":
        _deprecated_command("eval.cli.ab", args)
        asyncio.run(_cmd_ab(args))
    elif args.command == "report":
        _deprecated_command("eval.cli.report", args)
        _cmd_report(args)
    elif args.command == "inject-failure":
        _cmd_inject_failure(args)
    elif args.command == "evaluation":
        _cmd_evaluation(args)


DEPRECATION_CYCLE = "one release"


def _client_for(args: argparse.Namespace):
    from eval.client import EvaluationClient

    daemon_url = getattr(args, "daemon_url", None)
    if not daemon_url:
        try:
            daemon_url = load_eval_config(getattr(args, "config", None))["daemon_url"]
        except Exception:  # noqa: BLE001 — a missing config falls back to the default URL.
            daemon_url = "http://127.0.0.1:8000"
    return EvaluationClient(daemon_url)


def _deprecated_command(entry_point: str, args: argparse.Namespace) -> None:
    """Warn once and record the legacy command as a measured fallback.

    The compatibility commands stay for one deprecation cycle. Every
    use records through the facade so the removal gate measures real
    fallback traffic instead of assuming none.
    """
    import warnings

    warnings.warn(
        f"{entry_point} is a compatibility command kept for "
        f"{DEPRECATION_CYCLE}; use `python -m eval.cli evaluation ...`",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        _client_for(args).record_fallback(entry_point)
    except Exception as error:  # noqa: BLE001 — an unreachable daemon never blocks the command.
        logging.getLogger("eval.cli").warning(
            "fallback use was not recorded: %s", error,
        )


def _cmd_evaluation(args: argparse.Namespace) -> None:
    client = _client_for(args)
    command = args.evaluation_command
    if command == "authority":
        print(json.dumps(client.authority(), indent=2, sort_keys=True))
    elif command == "removal-gates":
        print(json.dumps(client.removal_gates(), indent=2, sort_keys=True))
    elif command == "migrate-results":
        from eval.legacy_results import migrate_directory

        for entry in migrate_directory(client, args.results_dir):
            print(json.dumps(entry, sort_keys=True))
    elif command == "export-bundle":
        import base64

        built = client.export_bundle(args.run_id, policy=args.policy)
        output = Path(args.output or f"eval/results/{args.run_id}-replay-bundle.zip")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(base64.b64decode(built["archive_base64"]))
        manifest_path = output.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(built["manifest"], indent=2, sort_keys=True))
        print(json.dumps({
            "bundle": str(output),
            "manifest": str(manifest_path),
            "bundle_digest": built["bundle_digest"],
            "member_count": built["member_count"],
            "claims": built["manifest"]["claims"],
        }, indent=2, sort_keys=True))
    elif command == "replay-bundle":
        import base64

        archive = base64.b64encode(Path(args.file).read_bytes()).decode("ascii")
        result = client.replay_bundle(
            archive, actor=args.actor, policy_version=args.policy_version,
        )
        result["execution_repeat_note"] = (
            "analysis replay recomputes stored evidence; it never repeats "
            "the model execution"
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif command == "score-preview":
        result = client.preview_score(
            args.plugin,
            {"final_output": args.response, "reference_answer": args.reference},
            {"comparison": args.comparison},
        )
        print(json.dumps(result, indent=2, sort_keys=True))


async def _cmd_benchmark(args: argparse.Namespace) -> None:
    from eval.datasets import load_gsm8k, load_mmlu
    from eval.metrics import compute_run_metrics
    from eval.runner import BenchmarkRunner

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
        variant=cfg["coordination_variant"],
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
        print("  Per-subject:")
        for subj, acc in metrics.accuracy_by_subject.items():
            print(f"    {subj}: {acc:.2%}")
    print(f"  Summary saved: {out_path}")
    print(f"{'='*60}")


async def _cmd_ab(args: argparse.Namespace) -> None:
    from eval.ab_harness import ABHarness
    from eval.datasets import load_gsm8k, load_mmlu

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
    print("  The daemon validates this implementation before submission.")
    print(f"{'='*60}\n")

    _, metrics_a = await harness.run_arm(
        items=items,
        expected_variant=args.variant_a,
        run_id=f"{run_id}-a",
        run_config={**cfg["coordination"], "variant": args.variant_a},
    )

    # Arm B
    print(f"\n{'='*60}")
    print(f"  A/B Test — Arm B: {args.variant_b}")
    print("  The same daemon receives this implementation per task.")
    print(f"{'='*60}\n")

    _, metrics_b = await harness.run_arm(
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
    from eval.ab_harness import ABHarness
    from eval.metrics import RunMetrics

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
