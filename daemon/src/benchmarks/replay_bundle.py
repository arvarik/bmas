"""Safe analysis-replay bundles: export, inert import, approved replay.

An export writes one immutable member manifest with paths, classes,
sizes, digests, and the redaction-policy digest, and includes only
the records and content-addressed assets the selected policy
permits: source and dataset manifests, the test revision and run
plan, effective runtime specifications, attempt evidence bundles,
score records, the analysis snapshot with its frozen input and
report, gate results, published schemas, and toolchain metadata. An
import treats the bundle as untrusted content: the archive passes
through the asset-ingestion quarantine path, undeclared, duplicate,
absolute, traversal, link, device, oversized, and excess-expansion
members reject, every member digest verifies, every executable class
enters quarantine and never runs, and credentials and environment
values strip before any record becomes readable. Analysis replay
starts only after an authenticated policy approval, and an execution
repeat needs a new run plan and capability decision. The bundle
guarantees deterministic analysis replay and exact execution
provenance only, never an equal result from another execution.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

from benchmarks import frozen_analysis
from benchmarks.provenance import canonical_json, content_checksum, redact_secrets

BUNDLE_FORMAT = "bmas-analysis-replay-bundle"
BUNDLE_FORMAT_VERSION = 1
EXPORT_POLICIES = ("redacted", "complete")
MEMBER_CLASSES = ("manifest", "record", "schema", "artifact", "toolchain",
                  "executable")
EXECUTABLE_HINTS = ("scorer", "runtime", "environment", "transformation",
                    "command")
_CREDENTIAL_KEYS = ("environment", "env", "credentials", "secrets")


class ReplayBundleError(ValueError):
    """The bundle violates the export or import contract."""


# ── Export ───────────────────────────────────────────────────────────


def _member(path: str, payload: bytes, member_class: str) -> dict[str, Any]:
    return {
        "path": path,
        "class": member_class,
        "size_bytes": len(payload),
        "digest": hashlib.sha256(payload).hexdigest(),
    }


def build_bundle(
    *,
    policy: str,
    run_manifest: dict[str, Any],
    sources: list[dict[str, Any]],
    dataset_manifest: dict[str, Any],
    test_revision: dict[str, Any],
    run_plan: dict[str, Any],
    runtime_specifications: list[dict[str, Any]],
    evidence_bundles: list[dict[str, Any]],
    score_records: list[dict[str, Any]],
    snapshot: dict[str, Any],
    frozen_input: dict[str, Any],
    report: dict[str, Any],
    gate_results: list[dict[str, Any]],
    artifacts: dict[str, bytes],
    schemas: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build one checksummed bundle under the selected export policy.

    The redacted policy strips secrets from every record and omits
    raw artifacts, keeping their digests as secure handles. The
    complete policy includes the artifacts. Every member digest
    verifies before the bundle publishes.
    """
    if policy not in EXPORT_POLICIES:
        raise ReplayBundleError(f"Unknown export policy: {policy!r}")
    redact = redact_secrets if policy == "redacted" else (lambda v: v)
    members: list[tuple[str, bytes, str]] = []

    def add(path: str, value: Any, member_class: str) -> None:
        payload = canonical_json(redact(value)).encode("utf-8")
        members.append((path, payload, member_class))

    add("manifests/run.json", run_manifest, "record")
    add("manifests/sources.json", sources, "record")
    add("manifests/dataset.json", dataset_manifest, "record")
    add("plan/test-revision.json", test_revision, "record")
    add("plan/run-plan.json", run_plan, "record")
    add("plan/runtime-specifications.json", runtime_specifications, "record")
    for bundle in evidence_bundles:
        add(f"evidence/{bundle['attempt_id']}.json", bundle, "record")
    for score in score_records:
        add(f"scores/{score['score_id']}.json", score, "record")
    add("analysis/snapshot.json", snapshot, "record")
    add("analysis/frozen-input.json", frozen_input, "record")
    add("analysis/report.json", report, "record")
    add("analysis/gates.json", gate_results, "record")
    for name, schema in sorted(schemas.items()):
        add(f"schemas/{name}.schema.json", schema, "schema")
    add("toolchain/engine.json", frozen_analysis.engine_digests(),
        "toolchain")
    artifact_handles = []
    for name, payload in sorted(artifacts.items()):
        digest = hashlib.sha256(payload).hexdigest()
        artifact_handles.append({"name": name, "digest": digest,
                                 "included": policy == "complete"})
        if policy == "complete":
            members.append((f"artifacts/{digest}", payload, "artifact"))
    add("artifacts/handles.json", artifact_handles, "record")

    from benchmarks.data_classes import policy_document

    redaction_policy = {"policy": policy, "redactor": policy_document()}
    manifest_members = [
        _member(path, payload, member_class)
        for path, payload, member_class in members
    ]
    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "policy": policy,
        "redaction_policy_digest": content_checksum(redaction_policy),
        "members": manifest_members,
        "claims": {
            "analysis_replay": "deterministic",
            "execution_provenance": "exact",
            "execution_repeat": "not_guaranteed",
        },
    }
    manifest_payload = canonical_json(manifest).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_payload)
        for path, payload, _member_class in members:
            archive.writestr(path, payload)
    archive_bytes = buffer.getvalue()
    verification = verify_members(archive_bytes, manifest)
    if not verification["verified"]:
        raise ReplayBundleError(
            "Export verification failed: " + ", ".join(verification["errors"])
        )
    return {
        "archive": archive_bytes,
        "manifest": manifest,
        "manifest_digest": content_checksum(manifest),
        "bundle_digest": hashlib.sha256(archive_bytes).hexdigest(),
        "member_count": len(manifest_members),
    }


def verify_members(
    archive_bytes: bytes, manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify every declared member digest and reject undeclared ones."""
    errors: list[str] = []
    declared = {member["path"]: member for member in manifest["members"]}
    if len(declared) != len(manifest["members"]):
        errors.append("duplicate manifest member")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            errors.append("duplicate archive member")
        present = set(names) - {"manifest.json"}
        for path in sorted(present - set(declared)):
            errors.append(f"undeclared member: {path}")
        for path in sorted(set(declared) - present):
            errors.append(f"missing member: {path}")
        for path in sorted(present & set(declared)):
            payload = archive.read(path)
            if hashlib.sha256(payload).hexdigest() != declared[path]["digest"]:
                errors.append(f"digest mismatch: {path}")
            if len(payload) != declared[path]["size_bytes"]:
                errors.append(f"size mismatch: {path}")
    return {"verified": not errors, "errors": errors}


# ── Import: untrusted, quarantined, inert ────────────────────────────


@dataclass
class ImportedBundle:
    """One inert imported bundle awaiting policy approval."""

    import_id: str
    manifest: dict[str, Any]
    records: dict[str, Any]
    quarantined_members: list[str]
    stripped_fields: list[str]
    ingestion_state: str
    replay_approved: bool = False
    approval: dict[str, Any] | None = None
    executed_members: list[str] = field(default_factory=list)


def _strip_credentials(value: Any, path: str, stripped: list[str]) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _CREDENTIAL_KEYS:
                stripped.append(f"{path}.{key}" if path else str(key))
                continue
            cleaned[key] = _strip_credentials(
                item, f"{path}.{key}" if path else str(key), stripped,
            )
        return redact_secrets(cleaned)
    if isinstance(value, list):
        return [_strip_credentials(item, f"{path}[]", stripped)
                for item in value]
    return value


def import_bundle(
    archive_bytes: bytes, *, limits: dict[str, Any] | None = None,
) -> ImportedBundle:
    """Import one bundle as untrusted content without executing it.

    The archive passes through the asset-ingestion quarantine path
    first, so traversal, absolute, link, device, nested, oversized,
    and excess-expansion members reject there. Every member digest
    verifies, undeclared and duplicate members reject, executable
    classes stay quarantined, and credentials strip before any record
    becomes readable. The import writes through no evaluation path.
    """
    from benchmarks.asset_ingestion import ingest_asset

    ingestion = ingest_asset(
        original_name="replay-bundle.zip",
        declared_media_type="application/zip",
        content=archive_bytes,
        limits=limits,
    )
    if ingestion.state != "accepted":
        raise ReplayBundleError(
            "The bundle archive failed quarantine: "
            + ", ".join(ingestion.rejection_reasons)
        )
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            payloads = {
                name: archive.read(name)
                for name in archive.namelist() if name != "manifest.json"
            }
    except (KeyError, ValueError, zipfile.BadZipFile) as error:
        raise ReplayBundleError(
            f"The bundle carries no readable manifest: {error}"
        ) from error
    if manifest.get("format") != BUNDLE_FORMAT:
        raise ReplayBundleError("The bundle declares an unknown format")
    verification = verify_members(archive_bytes, manifest)
    if not verification["verified"]:
        raise ReplayBundleError(
            "Member verification failed: " + ", ".join(verification["errors"])
        )
    records: dict[str, Any] = {}
    quarantined: list[str] = []
    stripped: list[str] = []
    for member in manifest["members"]:
        path = str(member["path"])
        member_class = str(member["class"])
        if member_class not in MEMBER_CLASSES:
            raise ReplayBundleError(
                f"The member {path} declares an unknown class"
            )
        executable = member_class == "executable" or any(
            hint in path.lower() and path.endswith((".py", ".sh", ".wasm"))
            for hint in EXECUTABLE_HINTS
        )
        if executable or member_class == "artifact":
            # Executable and asset members enter quarantine; import
            # never runs a scorer, runtime, environment,
            # transformation, or command.
            quarantined.append(path)
            continue
        try:
            value = json.loads(payloads[path].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            quarantined.append(path)
            continue
        records[path] = _strip_credentials(value, path, stripped)
    return ImportedBundle(
        import_id="import-" + hashlib.sha256(archive_bytes).hexdigest()[:16],
        manifest=manifest,
        records=records,
        quarantined_members=sorted(quarantined),
        stripped_fields=sorted(stripped),
        ingestion_state=ingestion.state,
    )


def approve_replay(
    imported: ImportedBundle, *, actor: str, policy_version: str,
) -> ImportedBundle:
    """Record one authenticated policy approval for analysis replay."""
    if not actor or not actor.strip():
        raise ReplayBundleError(
            "Analysis replay approval requires one authenticated actor"
        )
    if not policy_version:
        raise ReplayBundleError("The approval names its policy version")
    imported.replay_approved = True
    imported.approval = {"actor": actor, "policy_version": policy_version}
    return imported


def replay_from_bundle(imported: ImportedBundle) -> dict[str, Any]:
    """Rebuild the report from the imported evidence after approval."""
    if not imported.replay_approved:
        raise ReplayBundleError(
            "Analysis replay reads imported evidence only after an "
            "authenticated policy approval"
        )
    snapshot = imported.records.get("analysis/snapshot.json")
    frozen_input = imported.records.get("analysis/frozen-input.json")
    if snapshot is None or frozen_input is None:
        raise ReplayBundleError(
            "The bundle lacks the analysis snapshot or its frozen input"
        )
    specification = snapshot["estimand"]
    report = frozen_analysis.compute_report(specification, frozen_input)
    equal = (
        frozen_input["input_digest"] == snapshot["io_checksums"]["input"]
        and content_checksum(report) == snapshot["io_checksums"]["output"]
        and report["results_digest"] == snapshot["results_digest"]
    )
    return {
        "analysis_replayable": equal,
        "claim": "analysis_replayable" if equal else "analysis_not_replayable",
        "results_digest": report["results_digest"],
        "expected_results_digest": snapshot["results_digest"],
        "report": report,
        "execution_repeat": "not_guaranteed_by_this_bundle",
        "executed_members": list(imported.executed_members),
    }


def execution_repeat_requirements(imported: ImportedBundle) -> dict[str, Any]:
    """State what an execution repeat needs; never start one here."""
    return {
        "import_id": imported.import_id,
        "requires": ["new_run_plan", "new_capability_decision"],
        "started": False,
        "reason": (
            "an execution repeat runs agents, providers, tools, and "
            "environments again and can return different results"
        ),
    }


# ── Export from the canonical store ──────────────────────────────────


async def export_run_bundle(
    run_id: str, *, policy: str = "redacted", snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Assemble one bundle for a stored run from the canonical records.

    The analysis members rebuild from the stored snapshot's frozen
    specification so the bundle carries the exact frozen input and
    report the snapshot digests describe.
    """
    import database as db
    from benchmarks import evaluation_records, repository
    from benchmarks.evaluation_contracts import RECORD_SCHEMAS

    run = await repository.get_run(run_id)
    if run is None:
        raise ReplayBundleError(f"The run {run_id} does not exist")
    async with db._connect() as connection:  # noqa: SLF001
        if snapshot_id is None:
            cursor = await connection.execute(
                "SELECT id FROM analysis_snapshots WHERE run_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ReplayBundleError(
                    f"The run {run_id} has no analysis snapshot"
                )
            snapshot_id = str(row["id"])
        evidence_rows = await connection.execute_fetchall(
            "SELECT record FROM attempt_evidence_bundles WHERE attempt_id IN "
            "(SELECT attempt.id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ?) ORDER BY attempt_id",
            (run_id,),
        )
        score_rows = await connection.execute_fetchall(
            "SELECT record FROM score_records WHERE attempt_id IN "
            "(SELECT attempt.id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ?) ORDER BY id",
            (run_id,),
        )
        gate_rows = await connection.execute_fetchall(
            "SELECT id, status, report_checksum, superseded_at "
            "FROM benchmark_gate_evaluations WHERE candidate_run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        )
    stored = await evaluation_records.get_record(
        "analysis-snapshot", snapshot_id,
    )
    if stored is None or str(stored["run_id"]) != run_id:
        raise ReplayBundleError(f"The snapshot {snapshot_id} does not exist")
    snapshot = stored["record"]
    specification = snapshot["estimand"]
    planned = int((snapshot.get("filters") or {}).get(
        "planned_repetitions",
    ) or 1)
    frozen_input = frozen_analysis.freeze_input(
        run, specification, planned_repetitions=planned,
    )
    report = frozen_analysis.compute_report(specification, frozen_input)
    return build_bundle(
        policy=policy,
        run_manifest={
            "run_id": run_id,
            "status": run.get("status"),
            "execution_plan_checksum": run.get("execution_plan_checksum"),
            "test_configuration_checksum": run.get(
                "test_configuration_checksum",
            ),
        },
        sources=[],
        dataset_manifest={
            "dataset_id": run.get("dataset_id"),
            "dataset_version": run.get("dataset_version"),
            "dataset_checksum": run.get("dataset_checksum"),
        },
        test_revision={
            "test_revision_id": run.get("test_revision_id"),
            "configuration": run.get("test_configuration") or {},
        },
        run_plan=run.get("execution_plan") or {},
        runtime_specifications=[
            {"arm_id": arm.get("id"), "runtime_id": arm.get("runtime_id"),
             "configuration": arm.get("configuration") or {}}
            for arm in run.get("arms") or []
        ],
        evidence_bundles=[json.loads(row["record"]) for row in evidence_rows],
        score_records=[json.loads(row["record"]) for row in score_rows],
        snapshot=snapshot,
        frozen_input=frozen_input,
        report=report,
        gate_results=[dict(row) for row in gate_rows],
        artifacts={},
        schemas=dict(RECORD_SCHEMAS),
    )
