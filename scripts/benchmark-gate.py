#!/usr/bin/env python3
"""Evaluate one benchmark candidate for a continuous integration job."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a benchmark run against an immutable baseline",
    )
    parser.add_argument("baseline_id")
    parser.add_argument("candidate_run_id")
    parser.add_argument("--daemon-url", default="http://localhost:9000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--output", help="Write the JSON evaluation to this file")
    arguments = parser.parse_args()
    url = (
        f"{arguments.daemon_url.rstrip('/')}/benchmarks/baselines/"
        f"{arguments.baseline_id}/evaluate"
    )
    headers = {"Content-Type": "application/json"}
    if arguments.api_key:
        headers["Authorization"] = f"Bearer {arguments.api_key}"
    request = Request(
        url,
        data=json.dumps({"candidate_run_id": arguments.candidate_run_id}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"Benchmark gate request failed: {error}", file=sys.stderr)
        return 3
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if arguments.output:
        Path(arguments.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    status = result.get("status") or (result.get("report") or {}).get("status")
    if status == "passed":
        return 0
    if status == "failed":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
