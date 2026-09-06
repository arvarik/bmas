"""Scheduled restore tests measured against recovery objectives.

A backup that nobody restores proves nothing. The restore test creates
one consistent backup, restores it into an isolated directory, replays
every chain, and compares the measured recovery time and recovery
point against the declared objectives. Every outcome lands in the
backup records, so the Recovery Center backup health queue shows a
failed restore test the same way it shows a failed backup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import database as db
import journal_backup
import recovery_center

if TYPE_CHECKING:
    from core.asset_store import ArtifactStore

logger = logging.getLogger("bmas.daemon.restore_test")

# Recovery time objective and recovery point objective for one restore.
RESTORE_OBJECTIVES: dict[str, float] = {
    "recovery_time_seconds_max": 60.0,
    "recovery_point_lag_max": 0,
}
RESTORE_TEST_KIND = "restore_test"
RESTORE_TEST_INTERVAL_SECONDS = float(
    os.getenv("BMAS_RESTORE_TEST_INTERVAL_SECONDS", "0") or 0,
)
BACKUP_ROOT = Path(os.getenv("BMAS_BACKUP_ROOT", "/data/backups"))


async def live_journal_cursor() -> int:
    """Return the highest committed journal cursor."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(journal_cursor), 0) FROM runtime_journal",
        )
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def run_restore_test(
    backup_root: Path,
    *,
    artifact_store: ArtifactStore | None = None,
    replay_critical_digests: tuple[str, ...] = (),
    objectives: dict[str, float] | None = None,
    application_commit: str = "unknown",
    database_time: str | None = None,
    keep_restored: bool = False,
) -> dict[str, Any]:
    """Back up, restore, measure, and record one restore test."""
    targets = {**RESTORE_OBJECTIVES, **(objectives or {})}
    backup_root = Path(backup_root)
    live_cursor = await live_journal_cursor()
    backup = await journal_backup.create_backup(
        backup_root,
        artifact_store=artifact_store,
        replay_critical_digests=tuple(replay_critical_digests),
        application_commit=application_commit,
        database_time=database_time,
    )
    backup_id = str(backup["manifest"]["backup_id"])
    published_path = Path(backup["published_path"])
    restored_dir = backup_root / "restore-tests" / backup_id
    try:
        report = await journal_backup.restore_backup(published_path, restored_dir)
    finally:
        if not keep_restored:
            shutil.rmtree(restored_dir, ignore_errors=True)
    recovery_seconds = float(report["recovery_seconds"])
    recovery_point_cursor = int(report["recovery_point_cursor"])
    lag = live_cursor - recovery_point_cursor
    findings: list[str] = []
    if recovery_seconds > float(targets["recovery_time_seconds_max"]):
        findings.append("recovery_time_exceeded")
    if lag > int(targets["recovery_point_lag_max"]):
        findings.append("recovery_point_lag_exceeded")
    if report.get("replay_status") != "complete":
        findings.append("replay_incomplete")
    state = "succeeded" if not findings else "failed"
    measured = {
        "recovery_seconds": recovery_seconds,
        "recovery_point_cursor": recovery_point_cursor,
        "live_cursor": live_cursor,
        "recovery_point_lag": lag,
        "replay_status": report.get("replay_status"),
        "opened_artifacts": len(report.get("opened_artifacts", [])),
    }
    details = {
        "source_backup_id": backup_id,
        "objectives": targets,
        "measured": measured,
        "findings": findings,
    }
    outcome = await recovery_center.register_backup_outcome(
        backup_id=f"{backup_id}-restore-test",
        kind=RESTORE_TEST_KIND,
        state=state,
        published_path=str(published_path),
        details=details,
        database_time=database_time,
    )
    return {
        "backup_id": backup_id,
        "published_path": str(published_path),
        "state": state,
        "findings": findings,
        "objectives": targets,
        "measured": measured,
        "outcome": outcome,
    }


async def restore_test_loop(
    *,
    interval_seconds: float = RESTORE_TEST_INTERVAL_SECONDS,
    backup_root: Path = BACKUP_ROOT,
    iterations: int | None = None,
) -> None:
    """Run the restore test on a fixed cadence until cancelled."""
    completed = 0
    while iterations is None or completed < iterations:
        try:
            result = await run_restore_test(backup_root)
            logger.info(
                "Restore test %s: %s (recovery %.3fs, lag %d)",
                result["backup_id"], result["state"],
                result["measured"]["recovery_seconds"],
                result["measured"]["recovery_point_lag"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The scheduled restore test failed to run")
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        await asyncio.sleep(interval_seconds)
