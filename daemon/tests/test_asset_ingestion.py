"""Secure asset ingestion tests: quarantine, detection, and limits.

The suite runs every documented input through the pipeline: agreeing
media types, a harmless mismatch, an executable behind an image
extension, the standard malware test signature, archives with too
many entries, excess expansion, deep or recursive nesting, traversal
and absolute paths, links and devices, an extraction timeout, and a
scanner failure or stale signature. It then proves that only an
accepted asset links into a dataset, that member digests stay
immutable, and that the extraction step touches no network and no
secrets.
"""

from __future__ import annotations

import io
import json
import zipfile

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import evaluation_records
from benchmarks.asset_ingestion import (
    EICAR_TEST_SIGNATURE,
    ScanReport,
    detect_media_type,
    ingest_asset,
    store_ingestion,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _zip_bytes(
    entries: dict[str, bytes], *, compress: bool = False,
) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest_asyncio.fixture
async def asset_db(tmp_path, monkeypatch):
    path = str(tmp_path / "assets.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return path


# ── Media type comparison ────────────────────────────────────────────


def test_agreeing_media_types_accept():
    outcome = ingest_asset(
        original_name="diagram.png",
        declared_media_type="image/png",
        content=PNG_BYTES,
    )
    assert outcome.state == "accepted"
    assert outcome.record["detected_media_type"] == "image/png"
    assert outcome.rejection_reasons == []


def test_harmless_text_mismatch_accepts():
    outcome = ingest_asset(
        original_name="table.csv",
        declared_media_type="text/csv",
        content=b"a,b\n1,2\n",
    )
    assert outcome.state == "accepted"


def test_executable_with_image_extension_rejects():
    outcome = ingest_asset(
        original_name="photo.png",
        declared_media_type="image/png",
        content=b"MZ" + b"\x00" * 64,
    )
    assert outcome.state == "rejected"
    assert outcome.rejection_reasons == [
        "executable_masquerading_as_image/png",
    ]


def test_dangerous_type_mismatch_rejects():
    outcome = ingest_asset(
        original_name="report.pdf",
        declared_media_type="application/pdf",
        content=PNG_BYTES,
    )
    assert outcome.state == "rejected"
    assert any(
        reason.startswith("dangerous_type_mismatch")
        for reason in outcome.rejection_reasons
    )


def test_detection_covers_documented_magics():
    assert detect_media_type(PNG_BYTES) == "image/png"
    assert detect_media_type(b"\x7fELF rest") == "application/x-executable"
    assert detect_media_type(b"%PDF-1.7") == "application/pdf"
    assert detect_media_type(b"plain words") == "text/plain"
    assert detect_media_type(b"\xff\xfe\x00\x01") == (
        "application/octet-stream"
    )


# ── Scanner outcomes ─────────────────────────────────────────────────


def test_malware_test_signature_rejects():
    outcome = ingest_asset(
        original_name="note.txt",
        declared_media_type="text/plain",
        content=EICAR_TEST_SIGNATURE.encode("ascii"),
    )
    assert outcome.state == "rejected"
    assert outcome.rejection_reasons == [
        "malware_signature:bmas-reference-scanner",
    ]


def test_scanner_failure_stays_quarantined():
    outcome = ingest_asset(
        original_name="note.txt",
        declared_media_type="text/plain",
        content=b"content",
        scanner=lambda _content: ScanReport(
            engine="offline", signature_version="0", result="failed",
        ),
    )
    assert outcome.state == "quarantined"
    assert outcome.rejection_reasons == ["scanner_failed"]


def test_stale_signature_version_stays_quarantined():
    outcome = ingest_asset(
        original_name="note.txt",
        declared_media_type="text/plain",
        content=b"content",
        scanner=lambda _content: ScanReport(
            engine="aged", signature_version="2024.01",
            result="clean", signature_age_days=90,
        ),
    )
    assert outcome.state == "quarantined"
    assert outcome.rejection_reasons == [
        "stale_signature_version:2024.01",
    ]


# ── Archive limits and member policy ─────────────────────────────────


def test_archive_with_too_many_entries_rejects():
    content = _zip_bytes({f"file-{i}.txt": b"x" for i in range(4)})
    outcome = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=content,
        limits={"max_entries": 3},
    )
    assert outcome.state == "rejected"
    assert "too_many_entries:4" in outcome.rejection_reasons


def test_excess_expansion_or_ratio_rejects():
    bomb = _zip_bytes({"zeros.txt": b"\x00" * 200_000}, compress=True)
    ratio_limited = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=bomb,
        limits={"max_compression_ratio": 10.0},
    )
    assert ratio_limited.state == "rejected"
    assert any(
        reason.startswith("compression_ratio_limit")
        for reason in ratio_limited.rejection_reasons
    )
    size_limited = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=bomb,
        limits={"max_expanded_bytes": 100_000},
    )
    assert size_limited.state == "rejected"
    assert any(
        reason.startswith("expanded_bytes_limit")
        for reason in size_limited.rejection_reasons
    )


def test_deep_and_recursive_nesting_rejects():
    deep = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=_zip_bytes({"a/b/c/d/deep.txt": b"x"}),
        limits={"max_depth": 3},
    )
    assert deep.state == "rejected"
    assert "nesting_too_deep:5" in deep.rejection_reasons

    recursive = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=_zip_bytes({"inner.zip": _zip_bytes({"x.txt": b"x"})}),
    )
    assert recursive.state == "rejected"
    assert "nested_archives:1" in recursive.rejection_reasons


def test_traversal_absolute_link_and_device_entries_reject():
    traversal = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=_zip_bytes({"../escape.txt": b"x"}),
    )
    assert "path_traversal:../escape.txt" in traversal.rejection_reasons

    absolute = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=_zip_bytes({"/etc/absolute.txt": b"x"}),
    )
    assert any(
        reason.startswith("absolute_path")
        for reason in absolute.rejection_reasons
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("link.txt")
        link.external_attr = (0o120777 << 16)
        archive.writestr(link, b"target")
        device = zipfile.ZipInfo("device.txt")
        device.external_attr = (0o020666 << 16)
        archive.writestr(device, b"")
    special = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=buffer.getvalue(),
    )
    assert special.state == "rejected"
    assert "link_entry:link.txt" in special.rejection_reasons
    assert "device_entry:device.txt" in special.rejection_reasons


def test_extraction_timeout_rejects():
    ticks = iter([0.0, 100.0, 200.0])
    outcome = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=_zip_bytes({"one.txt": b"x", "two.txt": b"y"}),
        clock=lambda: next(ticks),
        limits={"max_extraction_seconds": 30.0},
    )
    assert outcome.state == "rejected"
    assert "extraction_timeout" in outcome.rejection_reasons


def test_accepted_archive_publishes_member_manifest_digests():
    outcome = ingest_asset(
        original_name="bundle.zip",
        declared_media_type="application/zip",
        content=_zip_bytes({"one.txt": b"alpha", "two.txt": b"beta"}),
    )
    assert outcome.state == "accepted"
    names = [member["name"] for member in outcome.members]
    assert names == ["one.txt", "two.txt"]
    for member in outcome.members:
        assert len(member["content_digest"]) == 64
    assert outcome.record["archive"]["entry_count"] == 2
    assert len(
        outcome.record["extraction"]["output_manifest_digest"],
    ) == 64


def test_extraction_step_has_no_network_and_no_secrets():
    import inspect

    from benchmarks import asset_ingestion

    source = inspect.getsource(asset_ingestion)
    for name in ("socket", "http", "urllib", "requests", "aiohttp",
                 "os.environ", "getenv"):
        assert name not in source


# ── Storage: quarantine first, then the declared transitions ─────────


@pytest.mark.asyncio
async def test_accepted_asset_stores_and_links(asset_db):
    outcome = ingest_asset(
        original_name="diagram.png",
        declared_media_type="image/png",
        content=PNG_BYTES,
    )
    stored = await store_ingestion(outcome)
    assert stored["state"] == "accepted"
    row = await evaluation_records.get_record(
        "asset-ingestion-record", stored["ingestion_id"],
    )
    assert row["state"] == "accepted"
    # The stored record content still says quarantined: the record is
    # the immutable pipeline history, the column is the live state.
    assert row["record"]["state"] == "quarantined"


@pytest.mark.asyncio
async def test_quarantined_and_rejected_assets_never_link(asset_db):
    rejected = await store_ingestion(ingest_asset(
        original_name="photo.png",
        declared_media_type="image/png",
        content=b"MZ" + b"\x00" * 64,
    ))
    quarantined = await store_ingestion(ingest_asset(
        original_name="note.txt",
        declared_media_type="text/plain",
        content=b"content",
        scanner=lambda _content: ScanReport(
            engine="offline", signature_version="0", result="failed",
        ),
    ))
    for stored, state in (
        (rejected, "rejected"), (quarantined, "quarantined"),
    ):
        with pytest.raises(
            evaluation_records.EvaluationStorageError,
            match=f"is {state}",
        ):
            await evaluation_records.link_case_asset(
                "draft-any", "case-any", stored["ingestion_id"],
            )
    with pytest.raises(
        evaluation_records.EvaluationStorageError, match="does not exist",
    ):
        await evaluation_records.link_case_asset(
            "draft-any", "case-any", "ingestion-missing",
        )


@pytest.mark.asyncio
async def test_ingestion_record_content_is_immutable(asset_db):
    stored = await store_ingestion(ingest_asset(
        original_name="diagram.png",
        declared_media_type="image/png",
        content=PNG_BYTES,
    ))
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE asset_ingestion_records SET record = ? "
                "WHERE id = ?",
                (json.dumps({"tampered": True}), stored["ingestion_id"]),
            )
        with pytest.raises(
            aiosqlite.IntegrityError, match="state transition",
        ):
            await connection.execute(
                "UPDATE asset_ingestion_records SET state = 'quarantined' "
                "WHERE id = ?",
                (stored["ingestion_id"],),
            )


@pytest.mark.asyncio
async def test_accepted_asset_links_into_a_draft(asset_db):
    from test_evaluation_contracts import (
        valid_benchmark_source,
        valid_dataset_draft,
        valid_evaluation_case,
    )

    from benchmarks import facade

    await facade.execute(
        "import_source", {"record": valid_benchmark_source()},
    )
    await facade.execute(
        "create_draft",
        {"record": valid_dataset_draft(), "source_id": "source-gsm8k"},
    )
    await facade.execute(
        "add_draft_case",
        {"record": valid_evaluation_case(), "draft_id": "draft-alpha"},
    )
    stored = await store_ingestion(ingest_asset(
        original_name="diagram.png",
        declared_media_type="image/png",
        content=PNG_BYTES,
    ))
    link_id = await evaluation_records.link_case_asset(
        "draft-alpha", "example-001", stored["ingestion_id"],
    )
    assert link_id.startswith("case-asset-")
