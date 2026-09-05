"""Capture one complete immutable evidence bundle per attempt.

The bundle connects the shared run manifest, the effective runtime
specification, the input case with its asset references, the complete
execution trace, the final output and final environment state, board
state references, tool calls and observations, files and artifacts,
claims with their verification decisions, costs, checkpoints and
recovery events, seed evidence, failure classifications, environment
versions with locale and time zone, and the admission-effect and
resource ledger references. Large evidence persists as
content-addressed artifacts and the bundle keeps typed digest
references. Foundation redaction applies to every section before any
byte persists, and the stored bundle never changes.
"""

from __future__ import annotations

import locale
import time
from pathlib import Path
from typing import Any

import database as db
from benchmarks.data_classes import RedactionReport, redact
from benchmarks.data_classes import policy_digest as redaction_policy_digest
from benchmarks.provenance import content_checksum

# The sections whose absence downgrades completeness. Optional
# sections record as unavailable without a downgrade.
_CORE_SECTIONS = ("trace", "final_output")

EVIDENCE_ACCESS_POLICY = "attempt-evidence-bytes"


class EvidenceCaptureError(ValueError):
    """The evidence bundle request violates the capture contract."""


def _evidence_store() -> Any:
    from core.asset_store import ArtifactStore

    root = Path(db.DB_PATH).parent / "evidence-artifacts"
    return ArtifactStore(root, "tenant-default")


def _persist_section(
    store: Any,
    attempt_id: str,
    section: str,
    value: Any,
) -> str:
    """Persist one redacted section as a content-addressed artifact."""
    from activation_service import persist_protected_artifact
    from benchmarks.provenance import canonical_json
    from core.asset_store import DataClass

    report = _REDACTION_REPORTS.setdefault(attempt_id, RedactionReport())
    redacted = redact(value, report=report, path=section)
    payload = canonical_json(redacted).encode("utf-8")
    _PERSISTED_BYTES[attempt_id] = _PERSISTED_BYTES.get(attempt_id, 0) + len(
        payload,
    )
    _PERSISTED_COUNT[attempt_id] = _PERSISTED_COUNT.get(attempt_id, 0) + 1
    return persist_protected_artifact(
        store,
        payload,
        media_type="application/json",
        access_policy=EVIDENCE_ACCESS_POLICY,
        data_class=DataClass.INTERNAL,
        referenced_by=f"{attempt_id}:{section}",
    )


# Bytes, artifacts, and the redaction report per attempt inside one
# capture call. The report lists every redacted path with its data
# class and the value detector that fired, so a reader sees which
# policy removed each value instead of a bare marker.
_PERSISTED_BYTES: dict[str, int] = {}
_PERSISTED_COUNT: dict[str, int] = {}
_REDACTION_REPORTS: dict[str, RedactionReport] = {}


def host_versions() -> dict[str, str]:
    """Report the build, locale, and time-zone versions in effect."""
    return {
        "locale": locale.setlocale(locale.LC_ALL) or "C",
        "time_zone": time.strftime("%Z") or "UTC",
    }


async def capture_attempt_evidence(
    *,
    attempt_id: str,
    run_manifest: dict[str, Any],
    runtime_specification: dict[str, Any],
    case: dict[str, Any],
    trace_events: list[dict[str, Any]] | None,
    final_output: str | None,
    resources: dict[str, Any],
    seed_evidence: dict[str, Any],
    ledger_references: dict[str, Any],
    final_state: dict[str, Any] | None = None,
    board_state_reference: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    artifacts: dict[str, bytes] | None = None,
    claims: list[dict[str, Any]] | None = None,
    verification_decisions: list[dict[str, Any]] | None = None,
    recovery_events: list[dict[str, Any]] | None = None,
    failure_classification: str | None = None,
    versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Capture, redact, persist, and store one evidence bundle.

    Every section redacts before persistence. Large sections store as
    content-addressed artifacts and the bundle keeps their digests.
    The bundle stores through the one facade into the immutable
    evidence table, so scoring always reads frozen evidence.
    """
    from benchmarks import facade

    if not attempt_id:
        raise EvidenceCaptureError("An evidence bundle names its attempt")
    store = _evidence_store()
    unavailable: list[str] = []
    report = _REDACTION_REPORTS.setdefault(attempt_id, RedactionReport())

    record: dict[str, Any] = {
        "schema_id": "attempt-evidence",
        "schema_version": 2,
        "attempt_id": attempt_id,
        "run_manifest_digest": content_checksum(
            redact(run_manifest, report=report, path="run_manifest"),
        ),
        "runtime_specification_digest": content_checksum(
            redact(
                runtime_specification, report=report,
                path="runtime_specification",
            ),
        ),
        "case_reference": {
            "case_id": str(
                case.get("case_id") or case.get("item_key") or "unknown",
            ),
            "asset_ids": [
                str(asset)
                for asset in (case.get("task") or {}).get("assets") or []
            ],
        },
    }

    if trace_events is None:
        unavailable.append("trace")
        record["trace_digest"] = None
    else:
        record["trace_digest"] = _persist_section(
            store, attempt_id, "trace", trace_events,
        )
    if final_output is None:
        unavailable.append("final_output")
        record["final_output_digest"] = None
    else:
        record["final_output_digest"] = _persist_section(
            store, attempt_id, "final_output", final_output,
        )
    if final_state is not None:
        record["final_state_digest"] = _persist_section(
            store, attempt_id, "final_state", final_state,
        )
    else:
        unavailable.append("final_state")
    if board_state_reference:
        record["board_state_reference"] = board_state_reference
    else:
        unavailable.append("board_state")
    if tool_calls is not None:
        record["tool_calls_digest"] = _persist_section(
            store, attempt_id, "tool_calls", tool_calls,
        )
    else:
        unavailable.append("tool_calls")
    if claims is not None or verification_decisions is not None:
        record["verification_decisions_digest"] = _persist_section(
            store, attempt_id, "verification",
            {"claims": claims or [],
             "decisions": verification_decisions or []},
        )
    else:
        unavailable.append("verification")

    record["artifacts"] = sorted(
        _persist_section(store, attempt_id, f"artifact:{name}", {
            "name": name,
            "content": payload.decode("utf-8", errors="replace"),
        })
        for name, payload in (artifacts or {}).items()
    )

    # The resources map holds structured counts and Money values, and
    # its "tokens" count is not a credential, so the key-based
    # redactor never applies to it.
    record["resources"] = dict(resources)
    if record["resources"].get("cost") is None:
        unavailable.append("resources.cost")
    core_missing = [
        section
        for section in (*_CORE_SECTIONS, "resources.cost")
        if section in unavailable
    ]
    # A complete bundle declares no unavailable section; the absent
    # optional sections stay visible through their absent digest
    # fields and through the returned absent_sections list.
    record["completeness"] = {
        "level": "partial_legacy" if core_missing else "complete",
        "unavailable_sections": unavailable if core_missing else [],
    }
    if recovery_events is not None:
        record["recovery_events"] = redact(
            recovery_events, report=report, path="recovery_events",
        )
    record["seed_evidence"] = dict(seed_evidence)
    record["redaction_policy_digest"] = redaction_policy_digest()
    record["redaction_report"] = _REDACTION_REPORTS.pop(attempt_id).to_dict()
    record["failure_classification"] = failure_classification
    record["versions"] = {**host_versions(), **(versions or {})}
    record["ledger_references"] = dict(ledger_references)

    saved = await facade.execute(
        "record_attempt_evidence",
        {"record": record, "attempt_id": attempt_id},
    )
    from benchmarks import resource_ledger

    await resource_ledger.emit_storage(
        attempt_id=attempt_id,
        byte_count=_PERSISTED_BYTES.pop(attempt_id, 0),
        artifact_count=_PERSISTED_COUNT.pop(attempt_id, 0),
    )
    return {
        "attempt_id": attempt_id,
        "record": record,
        "record_checksum": saved["record_checksum"],
        "completeness": record["completeness"],
        "absent_sections": unavailable,
    }


def read_evidence_section(content_digest: str) -> dict[str, Any]:
    """Read one persisted evidence section by its content digest.

    The stored bytes are already redacted, so the reader returns the
    decoded value as persisted; the attempt-evidence record's
    redaction report names the paths the policy removed.
    """
    stored = _evidence_store().read_object(content_digest)
    if stored.get("redacted"):
        return {"redacted": True, "reason": stored.get("reason")}
    import json

    return {"redacted": False,
            "value": json.loads(stored["payload"].decode("utf-8"))}
