#!/usr/bin/env python3
"""Block direct legacy database and result writes in continuous integration.

The check enforces the one evaluation authority:

1. The legacy ``eval/`` package imports no database module, executes
   no SQL, and imports nothing from the daemon benchmark package. It
   reaches canonical records only through the API client.
2. Only the declared writer modules mutate the canonical evaluation
   tables. Every other daemon module that names one of those tables in
   an INSERT, UPDATE, or DELETE statement fails the check.

Run from the repository root:

    python3 scripts/check-legacy-writes.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT / "eval"
DAEMON_ROOT = ROOT / "daemon" / "src"

LEGACY_PROHIBITED = (
    ("sql insert", re.compile(r"INSERT\s+INTO", re.IGNORECASE)),
    ("sql update", re.compile(r"UPDATE\s+\w+\s+SET", re.IGNORECASE)),
    ("sql delete", re.compile(r"DELETE\s+FROM", re.IGNORECASE)),
    ("aiosqlite import", re.compile(r"import\s+aiosqlite")),
    ("sqlite3 import", re.compile(r"import\s+sqlite3")),
    ("database import", re.compile(r"import\s+database")),
    ("benchmarks import", re.compile(r"from\s+benchmarks\s+import")),
    ("benchmarks import", re.compile(r"import\s+benchmarks")),
)

EVALUATION_TABLES = (
    "benchmark_sources", "dataset_drafts", "dataset_draft_cases",
    "dataset_transform_recipes", "evaluation_case_assets",
    "scorer_versions", "run_plans", "attempt_evidence_bundles",
    "analysis_snapshots", "gate_display_exceptions", "interaction_specs",
    "contamination_rights_records", "metric_definitions",
    "cost_settlement_versions", "dispatch_rank_history",
    "asset_ingestion_records", "score_records",
    "judge_calibration_records", "failure_classification_records",
    "resource_ledger_entries", "evaluation_migration_state",
    "evaluation_migration_events", "evaluation_readonly_archive",
    "dataset_version_records", "analysis_snapshot_supersessions",
    "judge_anchor_sets", "evaluation_studies",
)
# Modules the deprecation cycle removed after the fallback window
# measured zero legacy use. The record in conformance/evaluation
# names the window; a module that reappears fails the guard.
REMOVED_LEGACY_MODULES = (
    "eval/scorer.py",
    "eval/ab_harness.py",
)
DECLARED_WRITERS = {
    "benchmarks/evaluation_records.py",
    "benchmarks/evaluation_migration.py",
    "database.py",
}
WRITE_PATTERN = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(" + "|".join(EVALUATION_TABLES)
    + r")\b",
    re.IGNORECASE,
)


def scan_legacy_package() -> list[str]:
    findings = []
    for source in sorted(LEGACY_ROOT.rglob("*.py")):
        if "tests" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        for label, pattern in LEGACY_PROHIBITED:
            if pattern.search(text):
                findings.append(f"{source.relative_to(ROOT)}: {label}")
    return findings


def scan_daemon_writers() -> list[str]:
    findings = []
    for source in sorted(DAEMON_ROOT.rglob("*.py")):
        relative = source.relative_to(DAEMON_ROOT).as_posix()
        if relative in DECLARED_WRITERS:
            continue
        text = source.read_text(encoding="utf-8")
        for match in WRITE_PATTERN.finditer(text):
            findings.append(
                f"{source.relative_to(ROOT)}: writes {match.group(2)} "
                "outside the declared writers"
            )
    return findings


def scan_removed_modules() -> list[str]:
    """Fail when a module the deprecation cycle removed reappears."""
    return [
        f"{path}: the deprecation cycle removed this module; see "
        "conformance/evaluation/legacy_removal.json"
        for path in REMOVED_LEGACY_MODULES
        if (ROOT / path).exists()
    ]


def main() -> int:
    findings = (
        scan_legacy_package() + scan_daemon_writers() + scan_removed_modules()
    )
    if findings:
        print("FAIL: direct legacy or canonical evaluation writes:")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print(
        "PASS: the legacy package performs no canonical write and only the "
        "declared modules write evaluation tables."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
