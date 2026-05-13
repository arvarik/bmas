"""
Metrics computation and CLI reporting for the triage evaluation suite.

Contains:
  - ANSI color helpers (TTY-aware)
  - Confusion matrix and per-tier precision/recall/F1
  - Latency percentile computation
  - Rich CLI table and summary formatting
"""

import sys
import textwrap
import statistics
from datetime import datetime, timezone

from client import MODEL, TIERS


# ── ANSI Colors ──────────────────────────────────────────────────────────────

class C:
    """Minimal ANSI color codes. Auto-disabled when stdout is not a TTY."""
    _enabled = sys.stdout.isatty()

    @staticmethod
    def _wrap(code: str, text: str) -> str:
        return f"{code}{text}\033[0m" if C._enabled else text

    @staticmethod
    def bold(t: str) -> str:   return C._wrap("\033[1m", t)
    @staticmethod
    def dim(t: str) -> str:    return C._wrap("\033[2m", t)
    @staticmethod
    def cyan(t: str) -> str:   return C._wrap("\033[96m", t)
    @staticmethod
    def green(t: str) -> str:  return C._wrap("\033[92m", t)
    @staticmethod
    def yellow(t: str) -> str: return C._wrap("\033[93m", t)
    @staticmethod
    def red(t: str) -> str:    return C._wrap("\033[91m", t)
    @staticmethod
    def blue(t: str) -> str:   return C._wrap("\033[94m", t)


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_confusion_matrix(results: list[dict]) -> dict[str, dict[str, int]]:
    """Build a confusion matrix: matrix[expected][predicted] = count."""
    matrix: dict[str, dict[str, int]] = {t: {t2: 0 for t2 in TIERS} for t in TIERS}
    for r in results:
        exp, pred = r["expected"], r["label"]
        if exp in TIERS and pred in TIERS:
            matrix[exp][pred] += 1
    return matrix


def compute_tier_metrics(matrix: dict[str, dict[str, int]]) -> dict[str, dict[str, float]]:
    """Compute precision, recall, F1 per tier from confusion matrix."""
    metrics = {}
    for tier in TIERS:
        tp = matrix[tier][tier]
        fp = sum(matrix[other][tier] for other in TIERS if other != tier)
        fn = sum(matrix[tier][other] for other in TIERS if other != tier)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        metrics[tier] = {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}
    return metrics


def latency_percentiles(latencies: list[float]) -> dict[str, float]:
    """Compute p50, p90, p95, p99 from a list of latencies."""
    if not latencies:
        return {"p50": 0, "p90": 0, "p95": 0, "p99": 0}
    s = sorted(latencies)
    n = len(s)
    def pct(p: float) -> float:
        idx = int(p / 100 * (n - 1))
        return s[idx]
    return {"p50": pct(50), "p90": pct(90), "p95": pct(95), "p99": pct(99)}


# ── CLI Reporting ────────────────────────────────────────────────────────────

def print_header(url: str, total: int) -> None:
    print(f"\n{C.bold(C.cyan('🚀 bMAS Triage Evaluation Suite'))}")
    print(f"{C.dim('Model')}       : {MODEL}")
    print(f"{C.dim('Endpoint')}    : {url}")
    print(f"{C.dim('Constraint')}  : guided_choice {TIERS}")
    print(f"{C.dim('Test Cases')}  : {total}")
    print(f"{C.dim('Timestamp')}   : {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n")


def print_table_header() -> None:
    hdr = f"{'#':<4}{'LAT':>6}  {'TPS':>7}  {'EXPECT':<9}{'RESULT':<9}{'':>3}  {'TASK'}"
    print(C.bold(hdr))
    print("─" * 100)


def print_row(idx: int, r: dict) -> None:
    lat = f"{r['latency_s']:.2f}s"
    tps = f"{r['tps']:.0f}t/s" if r["tps"] > 0 else "—"
    preview = textwrap.shorten(r["task"], width=45, placeholder="…")

    if r.get("error"):
        print(f"{idx:<4}{C.red('ERR'):>6}  {'':>7}  {r['expected']:<9}{C.red('FAILED'):<9}{'✗':>3}  {C.dim(preview)}")
        return

    correct = r["label"] == r["expected"]
    if correct:
        lbl = C.green(f"{r['label']:<9}")
        mark = C.green("✓")
    else:
        lbl = C.yellow(f"{r['label']:<9}")
        mark = C.yellow("✗")

    print(f"{idx:<4}{lat:>6}  {tps:>7}  {r['expected']:<9}{lbl}{mark:>3}  {C.dim(preview)}")


def print_summary(results: list[dict]) -> None:
    valid = [r for r in results if not r.get("error")]
    if not valid:
        print(C.red("\nNo successful results to summarize."))
        return

    correct = sum(1 for r in valid if r["label"] == r["expected"])
    accuracy = correct / len(valid) * 100
    lats = [r["latency_s"] for r in valid]
    pcts = latency_percentiles(lats)
    total_prompt = sum(r["prompt_tokens"] for r in valid)
    total_completion = sum(r["completion_tokens"] for r in valid)

    # Accuracy banner
    if accuracy >= 80:
        acc_str = C.green(f"✅ {correct}/{len(valid)} ({accuracy:.0f}%)")
    elif accuracy >= 60:
        acc_str = C.yellow(f"⚠️  {correct}/{len(valid)} ({accuracy:.0f}%)")
    else:
        acc_str = C.red(f"❌ {correct}/{len(valid)} ({accuracy:.0f}%)")

    print(f"\n{'═' * 100}")
    print(C.bold(C.cyan("🎯 EVALUATION SUMMARY")))
    print(f"{'═' * 100}")
    print(f"{C.bold('Accuracy')}        : {acc_str}")
    print(f"{C.bold('Total Requests')}  : {len(results)} ({len(valid)} ok, {len(results) - len(valid)} failed)")
    print(f"{C.bold('Total Tokens')}    : {total_prompt + total_completion:,} ({total_prompt:,} prompt + {total_completion:,} completion)")

    print(f"\n{C.bold('Latency')}")
    print(f"  avg   : {statistics.mean(lats):.3f}s")
    print(f"  stdev : {statistics.stdev(lats):.3f}s" if len(lats) > 1 else "  stdev : —")
    print(f"  p50   : {pcts['p50']:.3f}s    p90 : {pcts['p90']:.3f}s    p95 : {pcts['p95']:.3f}s    p99 : {pcts['p99']:.3f}s")
    print(f"  min   : {min(lats):.3f}s    max : {max(lats):.3f}s")

    # Throughput
    tps_vals = [r["tps"] for r in valid if r["tps"] > 0]
    if tps_vals:
        print(f"\n{C.bold('Throughput')}")
        print(f"  avg   : {statistics.mean(tps_vals):.1f} tokens/sec")

    # Confusion matrix
    matrix = compute_confusion_matrix(valid)
    print(f"\n{C.bold('Confusion Matrix')}  (rows = expected, cols = predicted)")
    print(f"  {'':>10}", end="")
    for t in TIERS:
        print(f"{t:>9}", end="")
    print()
    for exp in TIERS:
        print(f"  {exp:>10}", end="")
        for pred in TIERS:
            val = matrix[exp][pred]
            if val == 0:
                print(f"{C.dim('·'):>9}", end="")
            elif exp == pred:
                print(f"{C.green(str(val)):>9}", end="")
            else:
                print(f"{C.yellow(str(val)):>9}", end="")
        print()

    # Per-tier precision / recall / F1
    tier_metrics = compute_tier_metrics(matrix)
    print(f"\n{C.bold('Per-Tier Metrics')}")
    print(f"  {'TIER':<10}{'PREC':>8}{'RECALL':>8}{'F1':>8}{'TP':>6}{'FP':>6}{'FN':>6}")
    for tier in TIERS:
        m = tier_metrics[tier]
        p_str = f"{m['precision']:.0%}"
        r_str = f"{m['recall']:.0%}"
        f_str = f"{m['f1']:.0%}"
        color = C.green if m["f1"] >= 0.75 else (C.yellow if m["f1"] >= 0.5 else C.red)
        print(f"  {tier:<10}{p_str:>8}{r_str:>8}{color(f_str):>8}{m['tp']:>6}{m['fp']:>6}{m['fn']:>6}")

    # Weighted F1
    total_support = sum(tier_metrics[t]["tp"] + tier_metrics[t]["fn"] for t in TIERS)
    if total_support > 0:
        weighted_f1 = sum(
            tier_metrics[t]["f1"] * (tier_metrics[t]["tp"] + tier_metrics[t]["fn"]) / total_support
            for t in TIERS
        )
        print(f"  {'WEIGHTED':<10}{'':>8}{'':>8}{C.bold(f'{weighted_f1:.0%}'):>8}")

    # Misclassifications
    misclassed = [r for r in valid if r["label"] != r["expected"]]
    if misclassed:
        print(f"\n{C.bold('Misclassifications')}")
        for r in misclassed:
            preview = textwrap.shorten(r["task"], width=60, placeholder="…")
            rid = r["id"]
            tag = C.yellow(f"#{rid:>2}")
            print(f"  {tag} {r['expected']:>8} → {r['label']:<8}  {C.dim(preview)}")

    print(f"{'═' * 100}\n")
