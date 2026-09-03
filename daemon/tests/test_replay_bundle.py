"""Safe analysis-replay bundles: export, inert import, approved replay.

An export verifies every member digest before publication and
carries the redaction-policy digest. An import passes the archive
through quarantine, rejects undeclared, duplicate, absolute,
traversal, link, device, oversized, and excess-expansion members,
quarantines every executable and asset member without running it,
strips credentials and environment values, stays inert until an
authenticated approval, and then rebuilds the report from the
imported evidence to equal digests in a clean environment. An
execution repeat is never started from a bundle.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
import pytest_asyncio
from test_evidence_capture import make_attempts
from test_frozen_analysis import oracle_spec

import database as db
from benchmarks import frozen_analysis, replay_bundle
from benchmarks.replay_bundle import (
    ReplayBundleError,
    approve_replay,
    build_bundle,
    execution_repeat_requirements,
    import_bundle,
    replay_from_bundle,
)


def _bundle(policy="redacted", **overrides):
    _, spec, frozen = oracle_spec()
    report = frozen_analysis.compute_report(spec, frozen)
    snapshot = frozen_analysis.snapshot_record(
        specification=spec, frozen_input=frozen, report=report,
        run_checksum="a" * 64, evidence_checksum="b" * 64,
        provenance=frozen_analysis.execution_provenance([]),
        replayable=True,
    )
    arguments = {
        "policy": policy,
        "run_manifest": {"run_id": "run-oracle", "api_key": "sk-secret",
                         "environment": {"HOME": "/root"}},
        "sources": [{"source_id": "source-a", "pinned_revision": "abc"}],
        "dataset_manifest": {"dataset_id": "dataset-a"},
        "test_revision": {"revision_id": "revision-a"},
        "run_plan": {"plan_id": "plan-a"},
        "runtime_specifications": [{"arm_id": "left"}],
        "evidence_bundles": [{"attempt_id": "attempt-a", "trace_digest":
                              "c" * 64}],
        "score_records": [{"score_id": "score-a"}],
        "snapshot": snapshot,
        "frozen_input": frozen,
        "report": report,
        "gate_results": [{"id": "gate-a", "status": "passed"}],
        "artifacts": {"trace.json": b'{"events": []}'},
        "schemas": {"score-record": {"type": "object"}},
    }
    arguments.update(overrides)
    return build_bundle(**arguments)


def _rezip(archive_bytes: bytes, mutate) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    entries = mutate(entries)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for name, payload in entries.items():
            target.writestr(name, payload)
    return buffer.getvalue()


# ── Export ───────────────────────────────────────────────────────────


def test_export_writes_a_verified_manifest_with_every_class():
    built = _bundle()
    manifest = built["manifest"]
    assert manifest["format"] == "bmas-analysis-replay-bundle"
    assert manifest["policy"] == "redacted"
    assert len(manifest["redaction_policy_digest"]) == 64
    classes = {member["class"] for member in manifest["members"]}
    assert classes == {"record", "schema", "toolchain"}
    paths = {member["path"] for member in manifest["members"]}
    assert {"manifests/run.json", "plan/run-plan.json",
            "evidence/attempt-a.json", "scores/score-a.json",
            "analysis/snapshot.json", "analysis/frozen-input.json",
            "analysis/report.json", "analysis/gates.json",
            "schemas/score-record.schema.json",
            "toolchain/engine.json", "artifacts/handles.json"} <= paths
    for member in manifest["members"]:
        assert len(member["digest"]) == 64
        assert member["size_bytes"] > 0
    assert manifest["claims"]["execution_repeat"] == "not_guaranteed"
    assert len(built["bundle_digest"]) == 64


def test_redacted_policy_strips_secrets_and_keeps_artifact_handles():
    redacted = _bundle("redacted")
    with zipfile.ZipFile(io.BytesIO(redacted["archive"])) as archive:
        run_manifest = json.loads(archive.read("manifests/run.json"))
        handles = json.loads(archive.read("artifacts/handles.json"))
        names = archive.namelist()
    assert run_manifest["api_key"] == "[redacted]"
    assert handles[0]["included"] is False
    assert not any(name.startswith("artifacts/") and name !=
                   "artifacts/handles.json" for name in names)
    complete = _bundle("complete")
    with zipfile.ZipFile(io.BytesIO(complete["archive"])) as archive:
        assert any(
            name.startswith("artifacts/") and len(name) == len("artifacts/") + 64
            for name in archive.namelist()
        )
    with pytest.raises(ReplayBundleError, match="Unknown export policy"):
        _bundle("partial")


# ── Import safety ────────────────────────────────────────────────────


def test_import_stays_inert_and_strips_credentials():
    imported = import_bundle(_bundle()["archive"])
    assert imported.replay_approved is False
    assert imported.ingestion_state == "accepted"
    assert imported.executed_members == []
    run_manifest = imported.records["manifests/run.json"]
    assert "environment" not in run_manifest
    assert run_manifest["api_key"] == "[redacted]"
    assert "manifests/run.json.environment" in imported.stripped_fields


def test_import_quarantines_executable_and_asset_members():
    built = _bundle("complete")

    def add_executable(entries):
        manifest = json.loads(entries["manifest.json"])
        payload = b"import os\nos.system('rm -rf /')\n"
        import hashlib

        manifest["members"].append({
            "path": "scorer/custom_scorer.py", "class": "executable",
            "size_bytes": len(payload),
            "digest": hashlib.sha256(payload).hexdigest(),
        })
        entries["scorer/custom_scorer.py"] = payload
        entries["manifest.json"] = replay_bundle.canonical_json(
            manifest,
        ).encode()
        return entries

    imported = import_bundle(_rezip(built["archive"], add_executable))
    assert "scorer/custom_scorer.py" in imported.quarantined_members
    assert any(path.startswith("artifacts/") for path in
               imported.quarantined_members)
    assert "scorer/custom_scorer.py" not in imported.records
    assert imported.executed_members == []


@pytest.mark.parametrize(("name", "mutate", "message"), [
    ("undeclared", lambda e: {**e, "extra/undeclared.json": b"{}"},
     "undeclared member"),
    ("tampered", lambda e: {**e, "manifests/run.json": b'{"x": 1}'},
     "digest mismatch"),
    ("missing", lambda e: {k: v for k, v in e.items()
                           if k != "plan/run-plan.json"},
     "missing member"),
])
def test_import_rejects_undeclared_tampered_and_missing_members(
    name, mutate, message,
):
    with pytest.raises(ReplayBundleError, match=message):
        import_bundle(_rezip(_bundle()["archive"], mutate))


def test_import_rejects_traversal_absolute_and_oversized_archives():
    def traversal(entries):
        return {**entries, "../escape.json": b"{}"}

    with pytest.raises(ReplayBundleError, match="path_traversal"):
        import_bundle(_rezip(_bundle()["archive"], traversal))

    def absolute(entries):
        return {**entries, "/abs.json": b"{}"}

    with pytest.raises(ReplayBundleError, match="absolute_path"):
        import_bundle(_rezip(_bundle()["archive"], absolute))
    with pytest.raises(ReplayBundleError, match="too_many_entries"):
        import_bundle(_bundle()["archive"], limits={"max_entries": 3})
    with pytest.raises(ReplayBundleError, match="byte_limit"):
        import_bundle(_bundle()["archive"], limits={"max_bytes": 100})


def test_import_rejects_an_unknown_format():
    def other_format(entries):
        manifest = json.loads(entries["manifest.json"])
        manifest["format"] = "other"
        entries["manifest.json"] = replay_bundle.canonical_json(
            manifest,
        ).encode()
        return entries

    with pytest.raises(ReplayBundleError, match="unknown format"):
        import_bundle(_rezip(_bundle()["archive"], other_format))


# ── Approval and replay ──────────────────────────────────────────────


def test_replay_needs_an_authenticated_approval():
    imported = import_bundle(_bundle()["archive"])
    with pytest.raises(ReplayBundleError, match="authenticated policy"):
        replay_from_bundle(imported)
    with pytest.raises(ReplayBundleError, match="authenticated actor"):
        approve_replay(imported, actor=" ", policy_version="1")
    approve_replay(imported, actor="operator-a", policy_version="1")
    assert imported.approval == {"actor": "operator-a",
                                 "policy_version": "1"}


def test_clean_environment_rebuilds_the_report_to_equal_digests():
    built = _bundle()
    # A clean environment holds only the bundle bytes.
    imported = import_bundle(built["archive"])
    approve_replay(imported, actor="operator-a", policy_version="1")
    replayed = replay_from_bundle(imported)
    assert replayed["analysis_replayable"] is True
    assert replayed["results_digest"] == replayed["expected_results_digest"]
    assert replayed["claim"] == "analysis_replayable"
    assert replayed["execution_repeat"] == "not_guaranteed_by_this_bundle"
    assert replayed["executed_members"] == []
    requirements = execution_repeat_requirements(imported)
    assert requirements["started"] is False
    assert requirements["requires"] == ["new_run_plan",
                                        "new_capability_decision"]


def test_tampered_frozen_input_breaks_the_replay_claim():
    built = _bundle()
    imported = import_bundle(built["archive"])
    imported.records["analysis/frozen-input.json"]["slots"]["right"]["a1"][
        "1"
    ]["value"] = 0.0
    approve_replay(imported, actor="operator-a", policy_version="1")
    assert replay_from_bundle(imported)["analysis_replayable"] is False


# ── Export from the canonical store ──────────────────────────────────


@pytest_asyncio.fixture
async def export_db(tmp_path, monkeypatch):
    path = str(tmp_path / "export.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await make_attempts(2)
    return path


@pytest.mark.asyncio
async def test_stored_run_exports_and_replays_in_a_clean_environment(
    export_db,
):
    from test_frozen_analysis import comparison, spec_for

    spec = spec_for(
        {"math": ["item-0", "item-1"]}, resample_count=9,
        comparison_family={"family_id": "primary", "comparisons": [
            comparison(baseline_arm="arm-evidence",
                       candidate_arm="arm-evidence"),
        ]},
    )
    stored = await frozen_analysis.freeze_and_store(
        "run-evidence", specification=spec, planned_repetitions=1,
    )
    built = await replay_bundle.export_run_bundle(
        "run-evidence", snapshot_id=stored["snapshot_id"],
    )
    assert built["member_count"] > 10
    imported = import_bundle(built["archive"])
    approve_replay(imported, actor="operator-a", policy_version="1")
    replayed = replay_from_bundle(imported)
    assert replayed["analysis_replayable"] is True
    with pytest.raises(ReplayBundleError, match="does not exist"):
        await replay_bundle.export_run_bundle("run-missing")
