"""The runner records artifacts from a snapshot copy.

A kept test stack keeps writing its logs after a failed group, and a
later group can rewrite a shared path. The record must stay valid
however the live file changes after the attempt ends.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def _runner():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("run_test_manifest", SCRIPTS / "run-test-manifest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_artifacts_are_snapshotted_before_hashing(tmp_path):
    runner = _runner()
    import manifestlib

    repo_root = tmp_path
    live = repo_root / "mission-control" / "test-results" / "stack" / "daemon.log"
    live.parent.mkdir(parents=True)
    live.write_text("first line\n")
    attempt_dir = repo_root / "test-results" / "run-a" / "groups" / "group-a" / "attempt-0"
    attempt_dir.mkdir(parents=True)
    group = {"id": "group-a", "artifacts": ["mission-control/test-results/**"]}

    records = runner.collect_artifacts(repo_root, group, attempt_dir)
    assert len(records) == 1
    record = records[0]
    assert record["path"] == "test-results/run-a/groups/group-a/attempt-0/artifacts/mission-control/test-results/stack/daemon.log"
    snapshot = repo_root / record["path"]
    assert snapshot.is_file()
    # The live file keeps changing after the attempt; the record stays valid.
    live.write_text("first line\nsecond line\n")
    assert manifestlib.file_sha256(snapshot) == record["sha256"]
    assert manifestlib.file_sha256(live) != record["sha256"]
    # A file already inside the attempt directory records in place.
    inside = attempt_dir / "report.json"
    inside.write_text("{}")
    group_inside = {"id": "group-a", "artifacts": ["test-results/run-a/groups/group-a/attempt-0/report.json"]}
    inside_records = runner.collect_artifacts(repo_root, group_inside, attempt_dir)
    assert inside_records[0]["path"] == "test-results/run-a/groups/group-a/attempt-0/report.json"
