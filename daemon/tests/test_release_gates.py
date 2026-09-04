"""Release gates: evaluation becomes the default only after proof.

The gate script maps every documented release gate to manifest groups
and reads one complete manifest result as read-only data. A missing
or failed group leaves its gate unproven or failed, promotion refuses
until every gate passes, and the daemon reports the default
generation only from verified release evidence.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import release

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release-gates.py"


def _load_gate_map() -> dict[str, list[str]]:
    namespace: dict = {}
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("RELEASE_GATES")
    end = source.index("\n}\n", start) + 3
    exec(source[start:end], namespace)  # noqa: S102 — the mapping is a literal.
    return namespace["RELEASE_GATES"]


def _result(states: dict[str, str]) -> dict:
    return {
        "schema_id": "bmas.test_manifest_result",
        "run_id": "run-release",
        "profile_id": "complete",
        "groups": [{"group_id": name, "state": state}
                   for name, state in states.items()],
    }


def _run(result_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--result", str(result_path), *extra],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )


def test_every_gate_names_registered_manifest_groups():
    import yaml

    manifest = yaml.safe_load(
        (REPO_ROOT / "test-manifest.yaml").read_text(encoding="utf-8"),
    )
    registered = {group["id"] for group in manifest["groups"]}
    gate_map = _load_gate_map()
    assert len(gate_map) == 38
    for gate, groups in gate_map.items():
        for group in groups:
            assert group in registered, f"{gate} names {group}"


def test_unproven_and_failed_gates_block_promotion(tmp_path):
    gate_map = _load_gate_map()
    every_group = sorted({g for groups in gate_map.values() for g in groups})
    states = dict.fromkeys(every_group, "passed")
    states["daemon.performance-contract"] = "skipped"
    states.pop("repo.source-naming")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result(states)))
    completed = _run(result_path, "--report", str(tmp_path / "gates.json"))
    assert completed.returncode == 1, completed.stdout
    report = json.loads((tmp_path / "gates.json").read_text())
    assert report["gates"]["performance_limits"]["status"] == "unproven"
    assert report["gates"]["source_naming"]["status"] == "unproven"
    assert report["all_passed"] is False
    states["repo.source-naming"] = "failed"
    states["daemon.performance-contract"] = "passed"
    result_path.write_text(json.dumps(_result(states)))
    completed = _run(result_path, "--report", str(tmp_path / "gates.json"))
    assert completed.returncode == 1
    report = json.loads((tmp_path / "gates.json").read_text())
    assert report["gates"]["source_naming"]["status"] == "failed"


def test_promotion_writes_verified_evidence(tmp_path, monkeypatch):
    gate_map = _load_gate_map()
    every_group = sorted({g for groups in gate_map.values() for g in groups})
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result(dict.fromkeys(every_group, "passed"))))
    evidence = tmp_path / "evaluation-default.json"
    source = SCRIPT.read_text(encoding="utf-8").replace(
        'EVIDENCE = ROOT / "conformance" / "release" / "evaluation-default.json"',
        f'EVIDENCE = Path({str(evidence)!r})',
    )
    script = tmp_path / "release-gates.py"
    script.write_text(source)
    completed = subprocess.run(
        [sys.executable, str(script), "--result", str(result_path), "--promote"],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PROMOTED" in completed.stdout
    status = release.evaluation_default_status(evidence)
    assert status["default_generation"] == "current"
    assert status["promoted"] is True
    assert status["gate_count"] == 38
    # Tampered evidence never promotes.
    document = json.loads(evidence.read_text())
    document["gates"]["source_naming"]["status"] = "failed"
    evidence.write_text(json.dumps(document))
    tampered = release.evaluation_default_status(evidence)
    assert tampered["default_generation"] == "legacy"
    assert tampered["promoted"] is False


def test_missing_evidence_keeps_the_legacy_default(tmp_path):
    status = release.evaluation_default_status(tmp_path / "missing.json")
    assert status["default_generation"] == "legacy"
    assert status["promoted"] is False
    assert pytest  # keep the import for the fixture decorators
