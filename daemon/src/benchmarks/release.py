"""The evaluation release decision: default only after proven gates.

Evaluation becomes the default generation only after the complete
manifest run proves every release gate. The release evidence file
binds the promoting commit, the manifest result run, its digest, and
the gate report. The daemon reads that file to report the default
generation, and it never promotes on its own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

EVIDENCE_PATH = (
    Path(__file__).resolve().parents[3]
    / "conformance" / "release" / "evaluation-default.json"
)


def evaluation_default_status(
    evidence_path: Path = EVIDENCE_PATH,
) -> dict[str, Any]:
    """Report which generation is the default and why."""
    if not evidence_path.is_file():
        return {
            "default_generation": "legacy",
            "promoted": False,
            "reason": "no verified release evidence exists",
        }
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except ValueError:
        return {
            "default_generation": "legacy",
            "promoted": False,
            "reason": "the release evidence is unreadable",
        }
    gates = evidence.get("gates") or {}
    failing = sorted(
        name for name, gate in gates.items() if gate.get("status") != "passed"
    )
    body = {key: value for key, value in evidence.items() if key != "digest"}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    verified = evidence.get("digest") == expected and not failing
    return {
        "default_generation": "current" if verified else "legacy",
        "promoted": verified,
        "reason": (
            "every release gate passed in the recorded complete run"
            if verified else
            "the release evidence failed verification: "
            + (", ".join(failing) or "digest mismatch")
        ),
        "commit": evidence.get("commit"),
        "manifest_run_id": evidence.get("manifest_run_id"),
        "gate_count": len(gates),
    }
