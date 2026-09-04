#!/usr/bin/env python3
"""Resolve and verify the pinned test toolchain.

The script reads ``toolchain-pins.yaml``, resolves every component's
exact version on this host, compares it with the pinned version line,
writes the resolved versions as one JSON report, and fails before tests
when a required component is absent or violates its pin. An optional
component records "unresolved" when its binary is absent.

Run from the repository root:

    python3 scripts/check-toolchain.py [--report test-results/toolchain.json] \
        [--require python,sqlite]

``--require`` names the components this consumer must resolve; every
other component records its resolved version and enforces its pin
only when present, so one partition never fails on a tool another
partition owns.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "toolchain-pins.yaml"
_VERSION = re.compile(r"(\d+(?:\.\d+)*)")


def _run(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False,
            cwd=ROOT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    return output.strip() or None


def _chromium_build() -> str | None:
    candidates = [
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ]
    for cache in candidates:
        if cache.is_dir():
            builds = sorted(
                path.name.rsplit("-", 1)[-1]
                for path in cache.iterdir()
                if path.name.startswith("chromium-") and path.is_dir()
            )
            if builds:
                return builds[-1]
    return None


def _distribution_version(name: str) -> str | None:
    """Resolve one installed Python distribution inside this interpreter."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - the standard library ships it
        return None
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _statistics_contract() -> str | None:
    """Resolve the arithmetic contract the analysis engines declare."""
    daemon_source = ROOT / "daemon" / "src"
    if str(daemon_source) not in sys.path:
        sys.path.insert(0, str(daemon_source))
    try:
        from benchmarks import analysis_engine
    except ImportError:
        return None
    return str(analysis_engine.STATISTICS_CONTRACT)


def resolve(component: str, spec: dict) -> str | None:
    argv = list(spec["resolve"])
    if argv[0] == "constant":
        return argv[1]
    if argv[0] == "python-sqlite":
        return sqlite3.sqlite_version
    if argv[0] == "python-distribution":
        return _distribution_version(argv[1])
    if argv[0] == "python-statistics-contract":
        return _statistics_contract()
    if argv[0] == "playwright-browser":
        return _chromium_build()
    if argv[0] == "python3":
        argv[0] = sys.executable
    output = _run(argv)
    if output is None:
        return None
    match = _VERSION.search(output)
    return match.group(1) if match else output


def satisfies(resolved: str, pin: str | list[str]) -> bool:
    """Match one resolved version against one pin or one list of pins."""
    if isinstance(pin, list):
        return any(satisfies(resolved, str(entry)) for entry in pin)
    if not _VERSION.fullmatch(pin):
        return resolved == pin
    return resolved == pin or resolved.startswith(pin + ".")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=None)
    parser.add_argument("--require", default=None,
                        help="comma-separated components this consumer requires")
    args = parser.parse_args()
    document = yaml.safe_load(PINS.read_text(encoding="utf-8"))
    consumer_required = (
        {name.strip() for name in args.require.split(",") if name.strip()}
        if args.require is not None else None
    )
    report: dict[str, dict] = {}
    failures: list[str] = []
    for name, spec in document["components"].items():
        resolved = resolve(name, spec)
        pin = spec["pin"] if isinstance(spec["pin"], list) else str(spec["pin"])
        required = (
            name in consumer_required if consumer_required is not None
            else bool(spec.get("required"))
        )
        if resolved is None:
            state = "unresolved"
            if required:
                failures.append(f"{name}: required component is absent")
        elif satisfies(resolved, pin):
            state = "pinned"
        else:
            state = "violates_pin"
            failures.append(
                f"{name}: resolved {resolved} violates the pin {pin}"
            )
        report[name] = {"pin": pin, "resolved": resolved, "state": state,
                        "required": required}
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_id": "bmas.toolchain_report",
            "contract_version": document["metadata"]["contract_version"],
            "components": report,
        }, indent=2, sort_keys=True) + "\n")
    for name, entry in sorted(report.items()):
        print(f"  {name}: {entry['resolved'] or 'unresolved'} ({entry['state']}, pin {entry['pin']})")
    if failures:
        print("FAIL: the resolved toolchain violates the manifest pins:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: every required toolchain component resolves inside its pin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
