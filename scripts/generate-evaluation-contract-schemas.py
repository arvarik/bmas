#!/usr/bin/env python3
"""Generate or verify the published evaluation record schemas.

The daemon module ``benchmarks.evaluation_contracts`` is the single
source of truth for every evaluation record contract. This script
publishes one JSON Schema file per record under
``docs/reference/evaluation-contracts/`` and verifies that the
published files stay equal to the definitions.

Usage:

    python3 scripts/generate-evaluation-contract-schemas.py
    python3 scripts/generate-evaluation-contract-schemas.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "daemon" / "src"))

from benchmarks.evaluation_contracts import RECORD_SCHEMAS

OUTPUT_DIR = REPO_ROOT / "docs" / "reference" / "evaluation-contracts"


def rendered(schema_id: str) -> str:
    """Return the stable formatted schema text for one record."""
    return (
        json.dumps(RECORD_SCHEMAS[schema_id], indent=2, sort_keys=True)
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = {
        f"{schema_id}.schema.json": rendered(schema_id)
        for schema_id in sorted(RECORD_SCHEMAS)
    }
    if arguments.check:
        stale: list[str] = []
        for name, text in expected.items():
            path = OUTPUT_DIR / name
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        existing = {
            path.name for path in OUTPUT_DIR.glob("*.schema.json")
        } if OUTPUT_DIR.exists() else set()
        for orphan in sorted(existing - set(expected)):
            stale.append(f"{orphan} (no matching definition)")
        if stale:
            print(
                "Evaluation contract schemas are stale: "
                + ", ".join(sorted(stale))
                + ". Run scripts/generate-evaluation-contract-schemas.py."
            )
            return 1
        print(f"PASS: {len(expected)} published schemas match the definitions.")
        return 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in expected.items():
        (OUTPUT_DIR / name).write_text(text)
    print(f"Wrote {len(expected)} schemas to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
