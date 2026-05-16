#!/usr/bin/env python3
"""
bMAS Semantic Triage Router — Evaluation Suite
===============================================
Validates the Qwen3-1.7B triage model's classification accuracy against
curated test cases spanning all 4 complexity tiers. Uses vLLM's guided_choice
constrained decoding to guarantee valid tier labels at the token level.

Usage:
  python3 test_triage.py                         # Default: localhost:8001
  python3 test_triage.py --url http://host:8001  # Custom endpoint
  python3 test_triage.py --no-warmup             # Skip warmup request
  python3 test_triage.py --export                # Save JSON results to ./results/
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from src.client import MODEL, GUIDED_CHOICE, SYSTEM_PROMPT, classify
from eval.report import C, print_header, print_table_header, print_row, print_summary
from eval.cases import TEST_CASES


def main() -> None:
    parser = argparse.ArgumentParser(description="bMAS Triage Router Evaluation Suite")
    parser.add_argument("--url", default="http://localhost:8001", help="vLLM base URL (no /v1)")
    parser.add_argument("--no-warmup", action="store_true", help="Skip warmup request")
    parser.add_argument("--export", action="store_true", help="Export JSON results to ./results/")
    parser.add_argument("--retries", type=int, default=2, help="Retries per request on failure")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    print_header(url, len(TEST_CASES))

    # Warmup: first request after vLLM start has CUDA graph compilation overhead
    if not args.no_warmup:
        print(C.dim("Warming up model (first request compiles CUDA graphs)…"), end=" ", flush=True)
        warmup = classify(url, "Hello", retries=3)
        if warmup["error"]:
            print(C.red(f"FAILED: {warmup['error']}"))
            print(C.red("Cannot reach vLLM server. Aborting."))
            sys.exit(1)
        print(C.green(f"done ({warmup['latency_s']:.2f}s)"))
        print()

    print_table_header()

    results: list[dict] = []
    for i, (task, expected) in enumerate(TEST_CASES, 1):
        resp = classify(url, task, retries=args.retries)
        tps = resp["completion_tokens"] / resp["latency_s"] if resp["latency_s"] > 0 else 0

        row = {
            "id": i,
            "task": task,
            "expected": expected,
            "label": resp["label"],
            "correct": resp["label"] == expected,
            "latency_s": resp["latency_s"],
            "tps": tps,
            "prompt_tokens": resp["prompt_tokens"],
            "completion_tokens": resp["completion_tokens"],
            "total_tokens": resp["total_tokens"],
            "finish_reason": resp["finish_reason"],
            "error": resp["error"],
        }
        results.append(row)
        print_row(i, row)

    print_summary(results)

    # Export
    if args.export:
        os.makedirs("results", exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"results/triage_eval_{ts}.json"
        export = {
            "model": MODEL,
            "endpoint": url,
            "timestamp": ts,
            "system_prompt": SYSTEM_PROMPT,
            "guided_choice": GUIDED_CHOICE,
            "results": results,
        }
        with open(path, "w") as f:
            json.dump(export, f, indent=2)
        print(C.dim(f"Results exported → {path}"))


if __name__ == "__main__":
    main()
