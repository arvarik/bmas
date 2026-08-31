"""Foundation Stage 0D: SQLite readiness, consistent backup, and restore.

The runtime journal lives in one SQLite database on one host and one
local filesystem. This module enforces that boundary: readiness
rejects unsupported SQLite versions, unsafe database state, and
network storage. Backups use the SQLite Online Backup API, verify the
snapshot and every referenced artifact, and publish only after both
pass. A restore replays every retained chain and opens every
replay-critical artifact.

The single-host operational facts are documented in
``docs/reference/storage-authority.md``.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

import database as db
import runtime_journal
from core.digest_profile import digest_bytes
from core.failpoints import failpoint

if TYPE_CHECKING:
    from core.asset_store import ArtifactStore

BACKUP_TOOL_VERSION = "1"

# The minimum SQLite library version for production journal writers.
MINIMUM_SQLITE_VERSION = (3, 40, 0)

# Filesystem types that are safe for SQLite WAL databases, and the
# network types that are never safe. Every other type is unknown and
# requires an explicit operator confirmation, because this check
# cannot prove every mount type.
LOCAL_FILESYSTEM_TYPES = frozenset(
    {"apfs", "hfs", "ext4", "ext3", "xfs", "btrfs", "zfs", "f2fs",
     "tmpfs", "overlay", "ufsd"}
)
NETWORK_FILESYSTEM_TYPES = frozenset(
    {"nfs", "nfs4", "smbfs", "cifs", "smb", "afpfs", "webdav", "fuse.sshfs",
     "9p", "glusterfs", "ceph", "lustre"}
)


class BackupError(RuntimeError):
    """A backup or restore step failed before publication."""


class StorageReadinessError(RuntimeError):
    """The storage topology or SQLite state is unsupported."""


def detect_filesystem_type(path: str) -> str | None:
    """Return the filesystem type of one path, or None when unknown."""
    try:
        if sys.platform == "linux" and os.path.exists("/proc/mounts"):
            best_match = ""
            best_type: str | None = None
            resolved = os.path.realpath(path)
            with open("/proc/mounts", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mount_point, fs_type = parts[1], parts[2]
                    if resolved.startswith(mount_point) and len(
                        mount_point,
                    ) > len(best_match):
                        best_match = mount_point
                        best_type = fs_type
            return best_type
        if sys.platform == "darwin":
            output = subprocess.run(
                ["/sbin/mount"], capture_output=True, text=True, timeout=10,
            ).stdout
            resolved = os.path.realpath(path)
            best_match = ""
            best_type = None
            for line in output.splitlines():
                if " on " not in line or " (" not in line:
                    continue
                _, remainder = line.split(" on ", 1)
                mount_point, details = remainder.rsplit(" (", 1)
                fs_type = details.split(",", 1)[0].strip(")")
                if resolved.startswith(mount_point) and len(
                    mount_point,
                ) > len(best_match):
                    best_match = mount_point
                    best_type = fs_type
            return best_type
    except Exception:
        return None
    return None


def classify_filesystem(fs_type: str | None) -> str:
    """Classify one filesystem type as local, network, or unknown."""
    if fs_type is None:
        return "unknown"
    normalized = fs_type.lower()
    if normalized in NETWORK_FILESYSTEM_TYPES:
        return "network"
    if normalized in LOCAL_FILESYSTEM_TYPES:
        return "local"
    return "unknown"


async def storage_readiness(
    *,
    database_path: str | None = None,
    operator_confirmed_storage: bool = False,
    sqlite_version: tuple[int, int, int] | None = None,
    filesystem_type: str | None = None,
) -> dict[str, Any]:
    """Check the storage topology and SQLite state for journal writers.

    The check fails closed: an unsupported SQLite version, a non-WAL
    journal mode, a failed quick check, or a network filesystem blocks
    readiness. An unknown filesystem type requires an explicit operator
    confirmation, because this check cannot prove every mount type.
    """
    path = database_path or db.DB_PATH
    checks: list[dict[str, Any]] = []

    version = sqlite_version or sqlite3.sqlite_version_info
    checks.append(
        {
            "check": "sqlite_version",
            "value": ".".join(str(part) for part in version),
            "ok": tuple(version) >= MINIMUM_SQLITE_VERSION,
            "detail": "The minimum supported version is "
            + ".".join(str(part) for part in MINIMUM_SQLITE_VERSION),
        }
    )

    journal_mode = "unreadable"
    quick_check = "unreadable"
    try:
        async with aiosqlite.connect(
            f"file:{path}?mode=ro", uri=True, timeout=15.0,
        ) as connection:
            cursor = await connection.execute("PRAGMA journal_mode")
            mode_row = await cursor.fetchone()
            if mode_row:
                journal_mode = str(mode_row[0]).lower()
            cursor = await connection.execute("PRAGMA quick_check")
            check_row = await cursor.fetchone()
            if check_row:
                quick_check = str(check_row[0]).lower()
    except sqlite3.Error:
        pass
    checks.append(
        {
            "check": "journal_mode",
            "value": journal_mode,
            "ok": journal_mode == "wal",
            "detail": "The journal requires WAL mode",
        }
    )
    checks.append(
        {
            "check": "quick_check",
            "value": quick_check,
            "ok": quick_check == "ok",
            "detail": "The database must pass the SQLite quick check",
        }
    )

    directory = os.path.dirname(os.path.abspath(path)) or "."
    writable = os.access(directory, os.W_OK) and (
        not os.path.exists(path) or os.access(path, os.W_OK)
    )
    checks.append(
        {
            "check": "writable_storage",
            "value": directory,
            "ok": writable,
            "detail": "The database file and directory must be writable",
        }
    )

    fs_type = filesystem_type
    if fs_type is None:
        fs_type = detect_filesystem_type(directory)
    fs_class = classify_filesystem(fs_type)
    if fs_class == "network":
        fs_ok = False
        fs_detail = (
            "Network filesystems do not support the SQLite WAL sharing "
            "model; use one local filesystem"
        )
    elif fs_class == "local":
        fs_ok = True
        fs_detail = "Local filesystem"
    else:
        fs_ok = operator_confirmed_storage
        fs_detail = (
            "Unknown storage type; this check cannot prove every mount "
            "type, so an operator confirmation is required"
        )
    checks.append(
        {
            "check": "storage_topology",
            "value": fs_type or "unknown",
            "ok": fs_ok,
            "detail": fs_detail,
        }
    )

    ready = all(check["ok"] for check in checks)
    return {"ready": ready, "checks": checks}


def require_storage_readiness(report: dict[str, Any]) -> None:
    """Raise when one readiness check failed."""
    if not report["ready"]:
        failed = [
            f"{check['check']}={check['value']} ({check['detail']})"
            for check in report["checks"]
            if not check["ok"]
        ]
        raise StorageReadinessError(
            "The storage is not ready for journal writers: "
            + "; ".join(failed)
        )


def validate_offline_copy(
    directory: Path, *, source_cleanly_closed: bool,
) -> dict[str, Any]:
    """Validate one offline physical copy of the database.

    The WAL is persistent database state. A copy taken after a clean
    close needs no WAL, because a clean close removes the WAL. A
    crash-state copy must retain the WAL file; a crash-state copy that
    omits the WAL is rejected, because it silently drops committed
    transactions.
    """
    directory = Path(directory)
    main_files = sorted(
        candidate
        for candidate in directory.iterdir()
        if candidate.suffix == ".db"
    )
    if len(main_files) != 1:
        raise BackupError("The offline copy must hold one database file")
    main = main_files[0]
    wal = main.with_name(main.name + "-wal")
    # Record the WAL presence first: opening the copy recovers the WAL
    # into the main file and removes the WAL file afterward.
    wal_present = wal.exists()
    if not source_cleanly_closed and not wal_present:
        raise BackupError(
            "The offline copy omits a required WAL file; copy the WAL or "
            "close the database cleanly first"
        )
    connection = sqlite3.connect(main)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if str(result[0]).lower() != "ok":
            raise BackupError("The offline copy failed verification")
    finally:
        connection.close()
    return {
        "database": str(main),
        "wal_present": wal_present,
        "source_cleanly_closed": source_cleanly_closed,
    }


async def create_backup(
    backup_root: Path,
    *,
    artifact_store: ArtifactStore | None = None,
    replay_critical_digests: tuple[str, ...] = (),
    application_commit: str = "unknown",
    database_time: str | None = None,
) -> dict[str, Any]:
    """Create one consistent, verified backup.

    The database snapshot uses the SQLite Online Backup API — never a
    copy of only the live main file. Referenced replay-critical
    artifacts copy next to the snapshot, every digest verifies, and
    the backup publishes only after the database and the artifact set
    both pass. An incomplete backup stays in the staging directory.
    """
    backup_root = Path(backup_root)
    staging = backup_root / "staging" / f"backup-{uuid.uuid4()}"
    staging.mkdir(parents=True, exist_ok=False)
    published_root = backup_root / "published"
    published_root.mkdir(parents=True, exist_ok=True)
    started_at = database_time or await db.database_utc_now()
    start_clock = time.monotonic()

    snapshot_path = staging / "journal.db"
    failpoint("backup.before_database_snapshot")
    async with (
        aiosqlite.connect(db.DB_PATH, timeout=15.0) as source,
        aiosqlite.connect(snapshot_path, timeout=15.0) as target,
    ):
        await source.backup(target)
    failpoint("backup.after_database_snapshot")

    # Record the highest journal cursor and every active chain head.
    connection = sqlite3.connect(snapshot_path)
    connection.row_factory = sqlite3.Row
    try:
        cursor_row = connection.execute(
            "SELECT COALESCE(MAX(journal_cursor), 0) AS cursor "
            "FROM runtime_journal"
        ).fetchone()
        highest_cursor = int(cursor_row["cursor"])
        chain_rows = connection.execute(
            "SELECT run_id, chain_epoch, chain_head_digest FROM runs "
            "ORDER BY run_id"
        ).fetchall()
        chain_heads = [
            {
                "run_id": str(row["run_id"]),
                "chain_epoch": int(row["chain_epoch"]),
                "chain_head_digest": str(row["chain_head_digest"]),
            }
            for row in chain_rows
        ]
        schema_row = connection.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        ).fetchone()
        schema_version = int(schema_row["version"])
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if str(integrity[0]).lower() != "ok":
            raise BackupError("The database snapshot failed verification")
    finally:
        connection.close()

    snapshot_digest = digest_bytes(
        "backup-database", snapshot_path.read_bytes(),
    )

    # Copy every referenced immutable artifact and verify each digest.
    failpoint("backup.before_artifact_copy")
    artifact_records = []
    artifact_dir = staging / "artifacts"
    artifact_dir.mkdir()
    for content_digest in replay_critical_digests:
        if artifact_store is None:
            raise BackupError(
                "Replay-critical artifacts require an artifact store"
            )
        payload = artifact_store.read_object(content_digest)
        if payload.get("redacted"):
            raise BackupError(
                f"The replay-critical artifact {content_digest} is erased"
            )
        target_path = artifact_dir / content_digest
        target_path.write_bytes(payload["payload"])
        copied_digest = digest_bytes(
            "artifact-content", target_path.read_bytes(),
        )
        if copied_digest != content_digest:
            raise BackupError(
                f"The copied artifact {content_digest} failed verification"
            )
        artifact_records.append(
            {
                "content_digest": content_digest,
                "size_bytes": target_path.stat().st_size,
                "retention_class": "replay_required",
            }
        )
    failpoint("backup.after_artifact_copy")

    completed_at = database_time or await db.database_utc_now()
    manifest = {
        "backup_id": staging.name,
        "database_snapshot_digest": snapshot_digest,
        "database_schema_version": schema_version,
        "highest_journal_cursor": highest_cursor,
        "chain_heads": chain_heads,
        "artifacts": artifact_records,
        "encryption_key_references": [],
        "application_commit": application_commit,
        "sqlite_version": sqlite3.sqlite_version,
        "backup_tool_version": BACKUP_TOOL_VERSION,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(time.monotonic() - start_clock, 3),
        "verification_result": "verified",
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    failpoint("backup.before_publish")
    published = published_root / staging.name
    os.replace(staging, published)
    return {"published_path": str(published), "manifest": manifest}


async def restore_backup(
    published_path: Path, target_dir: Path,
) -> dict[str, Any]:
    """Restore one published backup into an isolated location.

    The restore verifies the manifest digests, replays every retained
    chain, opens every replay-critical artifact, and reports the
    recovery point and recovery time.
    """
    start_clock = time.monotonic()
    published_path = Path(published_path)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(
        (published_path / "manifest.json").read_text(encoding="utf-8"),
    )

    snapshot_source = published_path / "journal.db"
    if digest_bytes(
        "backup-database", snapshot_source.read_bytes(),
    ) != manifest["database_snapshot_digest"]:
        raise BackupError("The backup database digest does not match")
    restored_db = target_dir / "journal.db"
    shutil.copyfile(snapshot_source, restored_db)

    opened_artifacts = []
    for artifact in manifest["artifacts"]:
        source = published_path / "artifacts" / artifact["content_digest"]
        payload = source.read_bytes()
        if digest_bytes("artifact-content", payload) != (
            artifact["content_digest"]
        ):
            raise BackupError(
                "A restored replay-critical artifact failed verification"
            )
        opened_artifacts.append(artifact["content_digest"])

    # Replay every retained chain from the restored database.
    original_path = db.DB_PATH
    db.DB_PATH = str(restored_db)
    try:
        result = await runtime_journal.replay()
    finally:
        db.DB_PATH = original_path
    if result.last_cursor != manifest["highest_journal_cursor"]:
        raise BackupError(
            "The restored journal does not reach the manifest cursor"
        )

    return {
        "restored_database": str(restored_db),
        "recovery_point_cursor": result.last_cursor,
        "recovery_seconds": round(time.monotonic() - start_clock, 3),
        "replay_status": result.status,
        "opened_artifacts": opened_artifacts,
        "projection_digest": result.state_digest,
    }
