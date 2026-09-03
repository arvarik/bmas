"""Migrate legacy result summary files through the evaluation API.

The legacy harness saved one ``{run_id}_summary.json`` file per run.
This module reads those files, sends each summary to the daemon, and
writes the migrated record beside the original. The daemon marks every
unmigrated field as unavailable, and this module never writes a
canonical record itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eval.client import EvaluationClient


def find_summaries(results_dir: str | Path) -> list[Path]:
    """List every legacy summary file in one results directory."""
    root = Path(results_dir)
    if not root.is_dir():
        return []
    return sorted(root.glob("*_summary.json"))


def migrate_summary_file(client: EvaluationClient, path: Path) -> dict[str, Any]:
    """Migrate one summary file and write the migrated record beside it."""
    summary = json.loads(Path(path).read_text())
    migrated = client.migrate_legacy_result(summary)
    output = Path(path).with_name(Path(path).stem + ".migrated.json")
    output.write_text(json.dumps(migrated, indent=2, sort_keys=True))
    return {
        "source": str(path),
        "output": str(output),
        "legacy_run_id": migrated.get("legacy_run_id"),
        "unavailable_fields": migrated.get("unavailable_fields", []),
        "record_digest": migrated.get("record_digest"),
    }


def migrate_directory(client: EvaluationClient, results_dir: str | Path) -> list[dict[str, Any]]:
    """Migrate every compatible legacy summary in one directory."""
    return [migrate_summary_file(client, path) for path in find_summaries(results_dir)]
