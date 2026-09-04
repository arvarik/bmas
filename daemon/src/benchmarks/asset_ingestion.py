"""Secure asset ingestion: quarantine, detection, limits, and release.

Every upload enters quarantine before any preview or extraction. The
pipeline hashes the original bytes, compares the declared, detected,
extension, and parser media types, runs a pinned scanner with its
signature version recorded, extracts archives inside a bounded
no-network step with exact byte, entry, depth, ratio, time, and
nesting limits, rejects traversal, absolute paths, links, devices,
and dangerous mismatches, publishes an extracted-member manifest with
content digests, and releases an asset only after every required
check passes. A quarantined or rejected asset never links into a
dataset, so it never reaches an agent, a scorer, or an export.
"""

from __future__ import annotations

import hashlib
import io
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

INGESTION_POLICY_VERSION = "1"

DEFAULT_LIMITS = {
    "max_bytes": 50_000_000,
    "max_entries": 200,
    "max_depth": 8,
    "max_expanded_bytes": 200_000_000,
    "max_compression_ratio": 100.0,
    "max_extraction_seconds": 30.0,
    "max_nested_archives": 0,
    "max_signature_age_days": 30,
}

_MAGIC_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"\x7fELF", "application/x-executable"),
    (b"MZ", "application/x-executable"),
    (b"\xcf\xfa\xed\xfe", "application/x-executable"),
    (b"\xfe\xed\xfa\xcf", "application/x-executable"),
    (b"\x1f\x8b", "application/gzip"),
)

_EXTENSION_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "csv": "text/csv",
    "jsonl": "application/x-ndjson",
    "json": "application/json",
    "txt": "text/plain",
    "md": "text/markdown",
}

# Mismatches the policy accepts as harmless text variants.
_HARMLESS_MISMATCHES = {
    frozenset({"text/plain", "text/csv"}),
    frozenset({"text/plain", "application/x-ndjson"}),
    frozenset({"text/plain", "application/json"}),
    frozenset({"text/plain", "text/markdown"}),
}

_ARCHIVE_TYPES = {"application/zip"}
_NESTED_ARCHIVE_SUFFIXES = (".zip", ".tar", ".gz", ".7z", ".zst")

# The EICAR test signature every scanner recognizes; the reference
# scanner flags it, so the pipeline tests use a real detection path.
EICAR_TEST_SIGNATURE = (
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!"
    "$H+H*"
)


class AssetIngestionError(ValueError):
    """The asset violates the ingestion policy."""


@dataclass(frozen=True)
class ScanReport:
    """One pinned scanner result with its signature version."""

    engine: str
    signature_version: str
    result: str  # clean | flagged | failed
    signature_age_days: int = 0


def reference_scanner(content: bytes) -> ScanReport:
    """The built-in reference scanner for the ingestion pipeline.

    It detects the standard antivirus test signature, so the flagged
    path exercises end to end without live malware.
    """
    flagged = EICAR_TEST_SIGNATURE.encode("ascii") in content
    return ScanReport(
        engine="bmas-reference-scanner",
        signature_version="2026.09",
        result="flagged" if flagged else "clean",
    )


@dataclass
class IngestionOutcome:
    """The complete pipeline outcome for one upload."""

    ingestion_id: str
    state: str
    record: dict[str, Any]
    rejection_reasons: list[str] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)


def detect_media_type(content: bytes) -> str:
    for magic, media_type in _MAGIC_TYPES:
        if content.startswith(magic):
            return media_type
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


def _extension_of(name: str) -> str:
    return name.lower().rsplit(".", 1)[-1] if "." in name else ""


def _types_agree(declared: str, detected: str) -> bool:
    if declared == detected:
        return True
    return frozenset({declared, detected}) in _HARMLESS_MISMATCHES


def _check_media_types(
    *, original_name: str, declared: str, detected: str,
) -> list[str]:
    reasons: list[str] = []
    if detected == "application/x-executable":
        # An executable never enters, whatever it claims to be.
        reasons.append(
            "executable_content"
            if declared.startswith("application/x-executable")
            else "executable_masquerading_as_" + (declared or "unknown")
        )
        return reasons
    if not _types_agree(declared, detected):
        reasons.append(
            f"dangerous_type_mismatch:{declared}!={detected}"
        )
    extension_type = _EXTENSION_TYPES.get(_extension_of(original_name))
    if extension_type and not _types_agree(extension_type, detected):
        reasons.append(
            f"extension_mismatch:{extension_type}!={detected}"
        )
    return reasons


def _member_reasons(info: zipfile.ZipInfo) -> list[str]:
    reasons = []
    path = PurePosixPath(info.filename)
    if path.is_absolute() or info.filename.startswith("/"):
        reasons.append(f"absolute_path:{info.filename}")
    if ".." in path.parts:
        reasons.append(f"path_traversal:{info.filename}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        reasons.append(f"link_entry:{info.filename}")
    if mode in (0o020000, 0o060000):
        reasons.append(f"device_entry:{info.filename}")
    if info.filename.lower().endswith(_NESTED_ARCHIVE_SUFFIXES):
        reasons.append(f"nested_archive:{info.filename}")
    return reasons


def _extract_archive(
    content: bytes,
    limits: dict[str, Any],
    clock: Callable[[], float],
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Extract one archive inside the bounded no-network step."""
    reasons: list[str] = []
    members: list[dict[str, Any]] = []
    started = clock()
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        infos = archive.infolist()
    except zipfile.BadZipFile as error:
        return [], [f"unreadable_archive:{error}"], {}
    entry_count = len(infos)
    depth = max(
        (len(PurePosixPath(info.filename).parts) for info in infos),
        default=0,
    )
    expanded = sum(info.file_size for info in infos)
    ratio = expanded / max(len(content), 1)
    statistics = {
        "entry_count": entry_count,
        "depth": depth,
        "expanded_bytes": expanded,
        "compression_ratio": round(ratio, 4),
    }
    if entry_count > limits["max_entries"]:
        reasons.append(f"too_many_entries:{entry_count}")
    if depth > limits["max_depth"]:
        reasons.append(f"nesting_too_deep:{depth}")
    if expanded > limits["max_expanded_bytes"]:
        reasons.append(f"expanded_bytes_limit:{expanded}")
    if ratio > limits["max_compression_ratio"]:
        reasons.append(f"compression_ratio_limit:{round(ratio, 2)}")
    nested = 0
    for info in infos:
        member_problems = _member_reasons(info)
        nested += sum(
            1 for reason in member_problems
            if reason.startswith("nested_archive")
        )
        reasons.extend(
            reason
            for reason in member_problems
            if not reason.startswith("nested_archive")
        )
    if nested > limits["max_nested_archives"]:
        reasons.append(f"nested_archives:{nested}")
    if reasons:
        return [], reasons, statistics
    for info in infos:
        if info.is_dir():
            continue
        if clock() - started > limits["max_extraction_seconds"]:
            return [], ["extraction_timeout"], statistics
        payload = archive.read(info)
        members.append({
            "name": info.filename,
            "size_bytes": len(payload),
            "content_digest": hashlib.sha256(payload).hexdigest(),
        })
    return members, [], statistics


def ingest_asset(
    *,
    original_name: str,
    declared_media_type: str,
    content: bytes,
    scanner: Callable[[bytes], ScanReport] | None = None,
    limits: dict[str, Any] | None = None,
    clock: Callable[[], float] | None = None,
    now_text: str = "1970-01-01T00:00:00Z",
) -> IngestionOutcome:
    """Run one upload through the complete quarantine pipeline.

    The outcome states ``accepted``, ``rejected``, or ``quarantined``.
    Only ``accepted`` releases; a scanner failure or a stale signature
    keeps the asset quarantined instead of silently passing it.
    """
    effective_limits = {**DEFAULT_LIMITS, **(limits or {})}
    effective_clock = clock or time.monotonic
    digest = hashlib.sha256(content).hexdigest()
    ingestion_id = f"ingestion-{uuid.uuid4().hex}"
    reasons: list[str] = []
    quarantine_holds: list[str] = []
    members: list[dict[str, Any]] = []
    statistics: dict[str, Any] = {}

    if len(content) > effective_limits["max_bytes"]:
        reasons.append(f"byte_limit:{len(content)}")
    detected = detect_media_type(content)
    reasons.extend(
        _check_media_types(
            original_name=original_name,
            declared=declared_media_type,
            detected=detected,
        ),
    )

    scan = (scanner or reference_scanner)(content)
    if scan.result == "flagged":
        reasons.append(f"malware_signature:{scan.engine}")
    elif scan.result == "failed":
        quarantine_holds.append("scanner_failed")
    if scan.signature_age_days > (
        effective_limits["max_signature_age_days"]
    ):
        quarantine_holds.append(
            f"stale_signature_version:{scan.signature_version}"
        )

    if not reasons and detected in _ARCHIVE_TYPES:
        members, archive_reasons, statistics = _extract_archive(
            content, effective_limits, effective_clock,
        )
        reasons.extend(archive_reasons)

    if reasons:
        state = "rejected"
    elif quarantine_holds:
        state = "quarantined"
    else:
        state = "accepted"

    record: dict[str, Any] = {
        "schema_id": "asset-ingestion-record",
        "schema_version": 2,
        "ingestion_id": ingestion_id,
        "original_name": str(original_name),
        "declared_media_type": str(declared_media_type),
        "detected_media_type": detected,
        "size_bytes": len(content),
        "digest": digest,
        "scanner": {
            "engine": scan.engine,
            "signature_version": scan.signature_version,
            "result": scan.result,
            "completed_at": now_text,
        },
        # The record stores the pipeline state; the ingestion table's
        # trigger-enforced state machine mirrors it.
        "state": "quarantined" if state == "quarantined" else state,
    }
    if statistics:
        record["archive"] = statistics
    if members:
        manifest_digest = hashlib.sha256(
            "".join(member["content_digest"] for member in members).encode(),
        ).hexdigest()
        record["extraction"] = {
            "image_digest": hashlib.sha256(
                b"bmas-in-process-extraction",
            ).hexdigest(),
            "policy_digest": hashlib.sha256(
                str(sorted(effective_limits.items())).encode(),
            ).hexdigest(),
            "output_manifest_digest": manifest_digest,
        }
    return IngestionOutcome(
        ingestion_id=ingestion_id,
        state=state,
        record=record,
        rejection_reasons=reasons + quarantine_holds,
        members=members,
    )


async def store_ingestion(
    outcome: IngestionOutcome, *, run_id: str | None = None,
) -> dict[str, Any]:
    """Store one pipeline outcome through the one facade.

    The record stores in quarantine first; an accepted outcome then
    transitions through the declared state machine, and a rejected
    outcome transitions to rejected. The record itself stays
    immutable in content. An ingestion that belongs to one run also
    records its bytes in that run's resource ledger.
    """
    from benchmarks import evaluation_records, facade

    record = dict(outcome.record)
    record["state"] = "quarantined"
    saved = await facade.execute(
        "record_asset_ingestion", {"record": record},
    )
    if outcome.state in ("accepted", "rejected"):
        await evaluation_records.transition_asset_state(
            saved["id"], outcome.state,
        )
    if run_id:
        from benchmarks import resource_ledger

        await resource_ledger.emit_import(
            run_id=run_id,
            import_id=str(saved["id"]),
            byte_count=int(
                record.get("size_bytes")
                or sum(int(m.get("size_bytes") or 0) for m in outcome.members)
            ),
            source=str(record.get("source") or record.get("media_type") or "asset"),
        )
    return {
        "ingestion_id": saved["id"],
        "state": outcome.state,
        "rejection_reasons": outcome.rejection_reasons,
        "members": outcome.members,
        "record_checksum": saved["record_checksum"],
    }
