#!/usr/bin/env python3
"""Create the effort-curve benchmark: four arms that differ only in effort.

The test measures whether more deliberation buys more accuracy on one
dataset. Every arm runs the classic runtime; only the effort level
changes (quick / standard / thorough / exhaustive). The Runs page then
shows the accuracy-versus-effort curve for the deployment.

Usage:
    python scripts/seed-effort-curve.py --dataset-version <id> [options]

Run without --dataset-version to list the available dataset versions
and scorers first.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EFFORT_ARMS = [
    {"name": "quick", "configuration": {"submission_overrides": {"effort": "quick"}}},
    {"name": "standard", "configuration": {}},
    {"name": "thorough", "configuration": {"submission_overrides": {"effort": "thorough"}}},
    {"name": "exhaustive", "configuration": {"submission_overrides": {"effort": "exhaustive"}}},
]


def _call(base: str, path: str, api_key: str, payload: dict | None = None) -> dict:
    url = f"{base.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(f"HTTP {error.code} from {path}: {detail}") from error
    except URLError as error:
        raise SystemExit(f"Cannot reach the daemon at {base}: {error.reason}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--daemon-url", default="http://localhost:9000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--dataset-version", help="Dataset version id for the test")
    parser.add_argument("--scorer", help="Scorer id (see the listing when omitted)")
    parser.add_argument("--runtime", default="classic", help="Runtime id for every arm")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--name", default="Effort curve — classic")
    parser.add_argument("--include-exhaustive", action="store_true",
                        help="Add the exhaustive arm (long and costly; off by default)")
    parser.add_argument("--start-run", action="store_true",
                        help="Queue a run for the new test immediately")
    arguments = parser.parse_args()

    if not arguments.dataset_version or not arguments.scorer:
        datasets = _call(arguments.daemon_url, "/datasets", arguments.api_key)
        scorers = _call(arguments.daemon_url, "/benchmarks/scorers", arguments.api_key)
        print("Pass --dataset-version and --scorer. Available options:\n")
        print("Dataset versions:")
        print(json.dumps(datasets, indent=1)[:4000])
        print("\nScorers:")
        print(json.dumps(scorers, indent=1)[:2000])
        return 2

    arms = [
        {"name": arm["name"], "runtime_id": arguments.runtime,
         "configuration": arm["configuration"]}
        for arm in EFFORT_ARMS
        if arguments.include_exhaustive or arm["name"] != "exhaustive"
    ]
    payload = {
        "name": arguments.name,
        "description": (
            "Accuracy versus deliberation: identical runtime and dataset, "
            "only the effort level differs per arm. Measures whether more "
            "rounds under the deliberation contract buy more accuracy."
        ),
        "dataset_version_id": arguments.dataset_version,
        "repetitions": arguments.repetitions,
        "max_concurrency": 1,
        "arms": arms,
        "scorers": [{"id": arguments.scorer}],
    }
    created = _call(arguments.daemon_url, "/benchmarks/tests", arguments.api_key, payload)
    print(json.dumps(created, indent=1))
    test_id = created.get("id")
    revisions = created.get("revisions") or []
    revision_id = revisions[-1].get("id") if revisions else created.get("latest_revision_id")
    if arguments.start_run and test_id and revision_id:
        run = _call(
            arguments.daemon_url,
            f"/benchmarks/tests/{test_id}/revisions/{revision_id}/runs",
            arguments.api_key,
            {"operator_note": "effort-curve seed"},
        )
        print(json.dumps(run, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
