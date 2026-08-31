"""Foundation Stage 0D: SQLite operational, backup, and restore tests.

Readiness rejects unsupported storage topology and unsafe SQLite
state. Backups publish only after the database snapshot and the
artifact set both verify, and a restore replays every retained chain.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from pathlib import Path

import journal_test_support as support
import pytest

import database as db
import journal_backup as backup
import runtime_journal as journal
from core import failpoints
from core.asset_store import ArtifactStore, DataClass, RetentionClass
from core.digest_profile import digest_bytes
from core.failpoints import InjectedFaultError


@pytest.fixture(autouse=True)
def clean_failpoints():
    failpoints.clear()
    yield
    failpoints.clear()


@pytest.fixture()
async def journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "journal.db"))
    await db.init_db()
    return tmp_path


@pytest.mark.asyncio
async def test_readiness_passes_on_local_supported_storage(journal_db):
    report = await backup.storage_readiness(filesystem_type="ext4")
    assert report["ready"] is True
    backup.require_storage_readiness(report)


@pytest.mark.asyncio
async def test_readiness_rejects_an_unsupported_sqlite_version(journal_db):
    report = await backup.storage_readiness(
        filesystem_type="ext4", sqlite_version=(3, 30, 0),
    )
    assert report["ready"] is False
    with pytest.raises(backup.StorageReadinessError, match="sqlite_version"):
        backup.require_storage_readiness(report)


@pytest.mark.asyncio
async def test_readiness_rejects_network_storage(journal_db):
    for fs_type in ("nfs", "cifs", "smbfs"):
        report = await backup.storage_readiness(filesystem_type=fs_type)
        assert report["ready"] is False
        failed = [c for c in report["checks"] if not c["ok"]]
        assert failed[0]["check"] == "storage_topology"


@pytest.mark.asyncio
async def test_unknown_storage_requires_operator_confirmation(journal_db):
    denied = await backup.storage_readiness(filesystem_type="mysteryfs")
    assert denied["ready"] is False
    confirmed = await backup.storage_readiness(
        filesystem_type="mysteryfs", operator_confirmed_storage=True,
    )
    assert confirmed["ready"] is True


@pytest.mark.asyncio
async def test_readiness_rejects_a_non_wal_database(tmp_path, monkeypatch):
    rollback_db = tmp_path / "rollback.db"
    connection = sqlite3.connect(rollback_db)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("CREATE TABLE plain (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    report = await backup.storage_readiness(
        database_path=str(rollback_db), filesystem_type="ext4",
    )
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert "journal_mode" in failed


@pytest.mark.asyncio
async def test_readiness_rejects_read_only_storage(journal_db):
    db_path = Path(db.DB_PATH)
    directory = db_path.parent
    os.chmod(db_path, 0o444)
    os.chmod(directory, 0o555)
    try:
        report = await backup.storage_readiness(filesystem_type="ext4")
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        assert "writable_storage" in failed
    finally:
        os.chmod(directory, 0o755)
        os.chmod(db_path, 0o644)


@pytest.mark.asyncio
async def test_a_read_only_database_leaves_no_partial_write(journal_db):
    await journal.commit_operation(support.admission_operation())
    before = await journal.read_journal()
    # Checkpoint and truncate so the WAL disappears; the read-only
    # directory then rejects the WAL recreation immediately.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute_fetchall("PRAGMA wal_checkpoint(TRUNCATE)")
    db_path = Path(db.DB_PATH)
    directory = db_path.parent
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    for stale in (wal_path, shm_path):
        if stale.exists():
            stale.unlink()
    os.chmod(db_path, 0o444)
    os.chmod(directory, 0o555)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await journal.commit_operation(support.activation_operation())
    finally:
        os.chmod(directory, 0o755)
        os.chmod(db_path, 0o644)
    assert await journal.read_journal() == before


@pytest.mark.asyncio
async def test_concurrent_readers_and_one_writer(journal_db):
    await journal.commit_operation(support.admission_operation())

    async def reader() -> int:
        total = 0
        for _ in range(5):
            total = len(await journal.read_journal())
        return total

    async def writer() -> None:
        for index in range(5):
            await journal.commit_operation(
                support.evidence_operation(
                    idempotency_token=f"evidence-load-{index}",
                    payload={
                        "claim_id": f"claim-{index}",
                        "evidence_state": "verified",
                    },
                )
            )

    results = await asyncio.gather(reader(), writer(), reader(), reader())
    assert min(results[0], results[2], results[3]) >= 1
    records = await journal.read_journal()
    assert len(records) == 6
    journal.verify_chain(records)


@pytest.mark.asyncio
async def test_writer_contention_serializes_without_corruption(journal_db):
    await journal.commit_operation(support.admission_operation())

    async def contend(index: int) -> None:
        await journal.commit_operation(
            support.evidence_operation(
                idempotency_token=f"evidence-contend-{index}",
                payload={
                    "claim_id": f"claim-contend-{index}",
                    "evidence_state": "verified",
                },
            )
        )

    await asyncio.gather(*(contend(index) for index in range(8)))
    records = await journal.read_journal()
    journal.verify_chain(records)
    assert len(records) == 9


@pytest.mark.asyncio
async def test_wal_growth_recovers_through_a_bounded_checkpoint(journal_db):
    # A long-lived reader keeps the WAL alive while the writer commits.
    keeper = sqlite3.connect(db.DB_PATH)
    keeper.execute("SELECT COUNT(*) FROM runtime_journal").fetchone()
    try:
        await journal.commit_operation(support.admission_operation())
        for index in range(30):
            await journal.commit_operation(
                support.evidence_operation(
                    idempotency_token=f"evidence-wal-{index}",
                    payload={
                        "claim_id": f"claim-wal-{index}",
                        "evidence_state": "verified",
                    },
                )
            )
        wal_path = Path(str(db.DB_PATH) + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0
        async with db._connect() as connection:  # noqa: SLF001
            rows = await connection.execute_fetchall(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            )
        assert rows[0][0] == 0
        assert wal_path.stat().st_size == 0
    finally:
        keeper.close()
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_a_crash_during_commit_recovers_cleanly(journal_db):
    await journal.commit_operation(support.admission_operation())
    failpoints.arm("journal.before_commit")
    with pytest.raises(InjectedFaultError):
        await journal.commit_operation(support.activation_operation())
    records = await journal.read_journal()
    assert len(records) == 1
    journal.verify_chain(records)
    assert await journal.commit_operation(support.activation_operation())


@pytest.mark.asyncio
async def test_recovery_with_retained_wal_files(journal_db, tmp_path):
    await support.seed_full_run()

    # A crash-state physical copy retains the WAL file. A keeper
    # connection holds the WAL open the way a running daemon does.
    keeper = sqlite3.connect(db.DB_PATH)
    keeper.execute("SELECT COUNT(*) FROM runtime_journal").fetchone()
    try:
        await journal.commit_operation(
            support.admission_operation("run-wal-retained"),
        )
        copy_dir = tmp_path / "crash-copy"
        copy_dir.mkdir()
        shutil.copyfile(db.DB_PATH, copy_dir / "journal.db")
        wal_source = Path(str(db.DB_PATH) + "-wal")
        assert wal_source.exists()
        shutil.copyfile(wal_source, copy_dir / "journal.db-wal")
    finally:
        keeper.close()

    report = backup.validate_offline_copy(
        copy_dir, source_cleanly_closed=False,
    )
    assert report["wal_present"] is True
    expected = (await journal.replay()).state_digest
    original = db.DB_PATH
    db.DB_PATH = str(copy_dir / "journal.db")
    try:
        recovered = await journal.replay()
    finally:
        db.DB_PATH = original
    assert recovered.state_digest == expected


@pytest.mark.asyncio
async def test_a_copy_that_omits_a_required_wal_is_rejected(
    journal_db, tmp_path,
):
    await support.seed_full_run()
    incomplete = tmp_path / "incomplete-copy"
    incomplete.mkdir()
    shutil.copyfile(db.DB_PATH, incomplete / "journal.db")
    with pytest.raises(backup.BackupError, match="required WAL"):
        backup.validate_offline_copy(
            incomplete, source_cleanly_closed=False,
        )


@pytest.mark.asyncio
async def test_an_offline_copy_after_clean_close_needs_no_wal(
    journal_db, tmp_path,
):
    await support.seed_full_run()
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute_fetchall("PRAGMA wal_checkpoint(TRUNCATE)")
    clean = tmp_path / "clean-copy"
    clean.mkdir()
    shutil.copyfile(db.DB_PATH, clean / "journal.db")
    report = backup.validate_offline_copy(
        clean, source_cleanly_closed=True,
    )
    assert report["wal_present"] is False


def seed_artifact(tmp_path: Path) -> tuple[ArtifactStore, str]:
    store = ArtifactStore(tmp_path / "artifacts", "tenant-a")
    payload = b"replay-critical body"
    staged = store.stage(
        payload,
        declared_digest=digest_bytes("artifact-content", payload),
        declared_size=len(payload),
        media_type="text/plain",
        scanner_result="clean",
        data_class=DataClass.INTERNAL,
        access_policy="task-scope",
        retention_class=RetentionClass.REPLAY_REQUIRED,
    )
    digest = store.promote(staged)
    store.commit_reference(digest, referenced_by="journal:test")
    return store, digest


@pytest.mark.asyncio
async def test_backup_publishes_only_after_full_verification(
    journal_db, tmp_path,
):
    await support.seed_full_run()
    store, digest = seed_artifact(tmp_path)
    result = await backup.create_backup(
        tmp_path / "backups",
        artifact_store=store,
        replay_critical_digests=(digest,),
        application_commit="test-commit",
    )
    manifest = result["manifest"]
    assert manifest["verification_result"] == "verified"
    assert manifest["highest_journal_cursor"] == 12
    assert len(manifest["chain_heads"]) == 1
    assert manifest["artifacts"][0]["content_digest"] == digest
    published = Path(result["published_path"])
    assert published.is_dir()
    assert (published / "journal.db").is_file()
    assert not any((tmp_path / "backups" / "staging").iterdir())


@pytest.mark.asyncio
async def test_a_failure_between_database_and_artifact_copy_stays_staged(
    journal_db, tmp_path,
):
    await support.seed_full_run()
    store, digest = seed_artifact(tmp_path)
    failpoints.arm("backup.before_artifact_copy")
    with pytest.raises(InjectedFaultError):
        await backup.create_backup(
            tmp_path / "backups",
            artifact_store=store,
            replay_critical_digests=(digest,),
        )
    staging = tmp_path / "backups" / "staging"
    published = tmp_path / "backups" / "published"
    assert any(staging.iterdir())
    assert not published.exists() or not any(published.iterdir())


@pytest.mark.asyncio
async def test_restore_replays_chains_and_opens_artifacts(
    journal_db, tmp_path,
):
    await support.seed_full_run()
    expected = await journal.replay()
    store, digest = seed_artifact(tmp_path)
    result = await backup.create_backup(
        tmp_path / "backups",
        artifact_store=store,
        replay_critical_digests=(digest,),
    )
    report = await backup.restore_backup(
        Path(result["published_path"]), tmp_path / "restored",
    )
    assert report["recovery_point_cursor"] == 12
    assert report["projection_digest"] == expected.state_digest
    assert report["replay_status"] == "complete"
    assert report["opened_artifacts"] == [digest]
    assert report["recovery_seconds"] >= 0


@pytest.mark.asyncio
async def test_restore_rejects_a_tampered_backup(journal_db, tmp_path):
    await support.seed_full_run()
    result = await backup.create_backup(tmp_path / "backups")
    published = Path(result["published_path"])
    with open(published / "journal.db", "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        handle.write(b"tamper")
    with pytest.raises(backup.BackupError, match="digest does not match"):
        await backup.restore_backup(published, tmp_path / "restored")


@pytest.mark.asyncio
async def test_the_journal_connection_uses_full_synchronous(journal_db):
    async with journal._journal_connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall("PRAGMA synchronous")
    # FULL is level two; EXTRA is level three.
    assert int(rows[0][0]) >= 2
