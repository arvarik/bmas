"""One facade, one authority: backfill, dual-read, cutover, rollback.

Every canonical mutation from either generation routes through one
facade to one writer. Backfill copies compatible legacy records twice
with idempotent cursors and equal digests, dual-read selects one
complete source and records every fallback, an injected digest
mismatch stops cutover with the exact source row, rollback before
cutover returns to the legacy writer, rollback after cutover keeps
the current authority with read-only legacy projections, and the
destructive contract refuses before every deletion gate passes. The
legacy ``eval/`` package performs no canonical write.
"""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import (
    evaluation_migration,
    evaluation_records,
    facade,
    repository,
)
from benchmarks.legacy_adapters import scorer_spec_from_scorer
from benchmarks.provenance import content_checksum


@pytest_asyncio.fixture
async def authority_db(tmp_path, monkeypatch):
    path = str(tmp_path / "authority.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    facade.reset_metrics()
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-authority",
        version_id="version-authority",
        name="Authority data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="authority-checksum",
        schema={"version": "1"},
        source_filename="authority.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="authority-source-checksum",
        source_path="/tmp/authority.jsonl",
        version_metadata={},
        items=[{
            "id": "item-authority",
            "item_key": "one",
            "input": "What is 20 plus 22?",
            "expected_output": "42",
            "subject": "math",
            "split": "test",
            "tags": [],
            "metadata": {},
        }],
    )
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    await facade.execute(
        "create_test_revision",
        {
            "test_id": "test-authority",
            "revision_id": "revision-authority",
            "name": "authority",
            "description": "",
            "dataset_version_id": "version-authority",
            "configuration": {"repetitions": 1, "seed": 1},
            "arms": [{
                "id": "arm-authority",
                "name": "Classic",
                "slug": "classic",
                "runtime_id": "classic",
                "configuration": envelope,
                "configuration_checksum": content_checksum(envelope),
            }],
            "scorers": [{"id": "scorer-exact-match-v1",
                         "configuration": {}}],
        },
        generation="legacy",
    )
    for run_id in ("run-base", "run-candidate"):
        await facade.execute(
            "create_run",
            {"run_id": run_id, "revision_id": "revision-authority",
             "idempotency_key": None},
            generation="legacy",
        )
        await _complete_run(path, run_id)
    return path


async def _complete_run(database_path: str, run_id: str) -> None:
    async with aiosqlite.connect(database_path) as connection:
        connection.row_factory = aiosqlite.Row
        rows = await connection.execute_fetchall(
            "SELECT attempt.id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ?",
            (run_id,),
        )
        for index, row in enumerate(rows):
            task_id = f"task-{run_id}-{index}"
            await connection.execute(
                "INSERT INTO tasks (id, label, full_input, status, "
                "terminal_kind, result_summary, total_cost_usd, "
                "total_tokens, duration_ms) VALUES (?, 'Benchmark', "
                "'Question', 'completed', 'completed', '42', 0.01, 100, "
                "1000)",
                (task_id,),
            )
            await connection.execute(
                "UPDATE benchmark_attempts SET status = 'running', "
                "task_id = ?, lease_token = 'lease', "
                "lease_expires_at = '2100-01-01T00:00:00.000Z' "
                "WHERE id = ?",
                (task_id, str(row["id"])),
            )
        await connection.commit()
        attempt_ids = [str(row["id"]) for row in rows]
    for attempt_id in attempt_ids:
        assert await repository.finish_attempt_from_task(
            attempt_id, "lease",
        )


async def _prepare_gate_with_exception() -> str:
    await facade.execute(
        "create_baseline",
        {
            "baseline_id": "baseline-authority",
            "run_id": "run-base",
            "name": "Authority baseline",
            "description": "",
            "rules": [{
                "id": "floor",
                "metric": "arm.classic.score.scorer-exact-match-v1",
                "operator": "gte",
                "value": 0.0,
            }],
            "created_by": "operator",
        },
        generation="legacy",
    )
    evaluation, created = await facade.execute(
        "evaluate_gate",
        {
            "baseline_id": "baseline-authority",
            "candidate_run_id": "run-candidate",
            "display_exceptions": [{
                "scope": (
                    "secondary_display:arm.classic.score.scorer-latency"
                ),
                "author": "operator",
                "expires_at": "2100-01-01T00:00:00Z",
                "reason": "The latency scorer is unavailable.",
            }],
        },
        generation="legacy",
    )
    assert created
    return str(evaluation["id"])


@pytest.mark.asyncio
async def test_every_generation_routes_through_the_one_facade(
    authority_db,
):
    metrics = facade.metrics_snapshot()
    # The fixture's legacy commands already flowed through the facade.
    assert metrics["generations"]["legacy"] >= 3
    assert metrics["commands"]["create_run"] == 2
    with pytest.raises(facade.FacadeCommandError):
        await facade.execute("create_run", {}, generation="mystery")
    with pytest.raises(facade.FacadeCommandError):
        await facade.execute("mystery_command", {}, generation="legacy")
    # A current command from the legacy generation adapter rejects:
    # each command belongs to exactly one adapter boundary.
    with pytest.raises(facade.FacadeCommandError):
        await facade.execute("import_source", {}, generation="legacy")


@pytest.mark.asyncio
async def test_backfill_runs_twice_with_idempotent_cursors(authority_db):
    gate_id = await _prepare_gate_with_exception()
    first = await evaluation_migration.run_backfill()
    assert first["scorer_specs"]["copied"] >= 3
    assert first["run_plans"]["copied"] == 2
    assert first["attempt_evidence"]["copied"] == 2
    assert first["display_exceptions"]["copied"] == 1
    state = await evaluation_migration.get_state()
    cursors = dict(state["cursors"])
    assert sorted(cursors) == sorted(
        evaluation_migration.BACKFILL_TARGETS,
    )

    second = await evaluation_migration.run_backfill()
    # The second run copies nothing, verifies every record by digest,
    # and leaves the cursors unchanged.
    for target, counts in second.items():
        assert counts["copied"] == 0, target
        assert counts["mismatched"] == 0, target
        assert counts["verified"] >= 1, target
    state = await evaluation_migration.get_state()
    assert state["cursors"] == cursors
    exceptions = await evaluation_records.get_record(
        "run-plan", "plan-run-base",
    )
    assert exceptions is not None
    del gate_id


@pytest.mark.asyncio
async def test_both_adapters_represent_one_backfilled_record_equally(
    authority_db,
):
    await evaluation_migration.run_backfill()
    scorers = {
        scorer["id"]: scorer
        for scorer in await repository.list_scorers()
    }
    legacy_row = scorers["scorer-exact-match-v1"]
    current = await facade.read_scorer_spec(
        "scorer-exact-match-v1", str(legacy_row["version"]),
    )
    assert current is not None
    assert current["source"] == "current"
    represented = current["record"]
    # The current record and the legacy adapter view represent the
    # same values.
    adapted = scorer_spec_from_scorer(legacy_row)
    assert represented == adapted
    assert represented["direction"] == legacy_row["direction"]
    assert represented["scale"] == legacy_row["scale"]


@pytest.mark.asyncio
async def test_dual_read_falls_back_to_one_complete_legacy_record(
    authority_db,
):
    # Nothing backfilled: the read selects the complete legacy record
    # and records the fallback durably.
    result = await facade.read_run_plan("run-base")
    assert result is not None
    assert result["source"] == "legacy"
    assert result["record"]["plan_id"] == "plan-run-base"
    fallbacks = await evaluation_migration.list_events("fallback")
    assert [event["payload"]["kind"] for event in fallbacks] == [
        "run-plan",
    ]

    evidence = await facade.read_attempt_evidence(
        await _one_attempt_id(),
    )
    assert evidence is not None
    assert evidence["source"] == "legacy"
    assert evidence["record"]["completeness"]["level"] == "partial_legacy"


async def _one_attempt_id() -> str:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT id FROM benchmark_attempts ORDER BY id LIMIT 1",
        )
        row = await cursor.fetchone()
    return str(row["id"])


@pytest.mark.asyncio
async def test_dual_read_never_merges_generations(authority_db):
    # A current-generation plan with a different repetition count
    # exists beside the legacy plan. The read returns the current
    # record alone; no legacy field leaks in.
    run = await repository.get_run("run-base")
    assert run is not None
    from benchmarks.legacy_adapters import run_plan_from_run

    record = run_plan_from_run(run)
    record["repetitions"] = 7
    record["estimand"]["marker"] = "current-only"
    await evaluation_records.save_record(
        record,
        record_id="plan-run-base",
        links={"test_revision_id": str(run["test_revision_id"]),
               "run_id": "run-base"},
    )
    result = await facade.read_run_plan("run-base")
    assert result is not None
    assert result["source"] == "current"
    assert result["record"]["repetitions"] == 7
    assert result["record"]["estimand"]["marker"] == "current-only"
    assert await evaluation_migration.count_events("fallback") == 0


@pytest.mark.asyncio
async def test_an_injected_digest_mismatch_stops_cutover(authority_db):
    await evaluation_migration.run_backfill()
    await evaluation_migration.advance_phase("backfill")
    await evaluation_migration.advance_phase("dual_read")
    # The legacy source changes after the copy: the rerun records the
    # exact source row, and cutover stops on it.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_scorers SET description = 'tampered' "
            "WHERE id = 'scorer-exact-match-v1'",
        )
        await connection.commit()
    rerun = await evaluation_migration.run_backfill()
    assert rerun["scorer_specs"]["mismatched"] == 1
    mismatches = await evaluation_migration.list_events("digest_mismatch")
    assert mismatches[0]["payload"]["source_id"] == (
        "scorer-exact-match-v1:1"
    )
    with pytest.raises(
        evaluation_migration.CutoverRefusedError,
        match="scorer-exact-match-v1:1",
    ):
        await evaluation_migration.advance_phase("cutover")


@pytest.mark.asyncio
async def test_rollback_before_cutover_returns_to_the_legacy_writer(
    authority_db,
):
    await evaluation_migration.run_backfill()
    before = await _legacy_row_count()
    await evaluation_migration.advance_phase("backfill")
    await evaluation_migration.advance_phase("dual_read")
    state = await evaluation_migration.rollback_phase()
    assert state["phase"] == "backfill"
    state = await evaluation_migration.rollback_phase()
    assert state["phase"] == "expand"
    assert state["legacy_readonly"] is False
    # Every legacy record survives the rollback.
    assert await _legacy_row_count() == before
    with pytest.raises(evaluation_migration.MigrationPhaseError):
        await evaluation_migration.rollback_phase()


async def _legacy_row_count() -> int:
    async with db._connect() as connection:  # noqa: SLF001
        total = 0
        for table in ("benchmark_runs", "benchmark_attempts",
                      "benchmark_scores", "dataset_items"):
            cursor = await connection.execute(
                f"SELECT COUNT(*) AS rows_present FROM {table}",
            )
            row = await cursor.fetchone()
            total += int(row["rows_present"])
    return total


@pytest.mark.asyncio
async def test_rollback_after_cutover_keeps_the_current_authority(
    authority_db,
):
    await evaluation_migration.run_backfill()
    await evaluation_migration.advance_phase("backfill")
    await evaluation_migration.advance_phase("dual_read")
    await evaluation_migration.advance_phase("cutover")
    state = await evaluation_migration.rollback_phase()
    # After cutover, rollback keeps the current generation as the
    # data authority; the compatible legacy projections turn
    # read-only for an older application.
    assert state["phase"] == "cutover"
    assert state["legacy_readonly"] is True


@pytest.mark.asyncio
async def test_the_destructive_contract_refuses_before_every_gate(
    authority_db,
):
    await evaluation_migration.run_backfill()
    await evaluation_migration.advance_phase("backfill")
    await evaluation_migration.advance_phase("dual_read")
    await evaluation_migration.advance_phase("cutover")
    with pytest.raises(
        evaluation_migration.ContractRefusedError, match="unpassed",
    ):
        await evaluation_migration.advance_phase("contract")
    export = await evaluation_migration.compatibility_export("run-base")
    assert export["verified"] is True
    for gate in evaluation_migration.DELETION_GATES:
        await evaluation_migration.record_gate(
            gate, True, actor="operator-a",
        )
    # A decision without measured evidence never opens the contract.
    with pytest.raises(
        evaluation_migration.ContractRefusedError, match="measured gate",
    ):
        await evaluation_migration.advance_phase("contract")
    await evaluation_migration.record_measured_fallback_gate(
        window_start="1970-01-01T00:00:00Z", actor="operator-a",
        threshold=evaluation_migration.DECLARED_FALLBACK_THRESHOLD + 10,
    )
    await evaluation_migration.record_gate(
        "downgrade_fixtures_passed", True, actor="operator-a",
        evidence={"legacy_records": 1, "current_records": 1,
                  "archived_records": 1},
    )
    await evaluation_migration.record_gate(
        "compatibility_export_verified", True, actor="operator-a",
        evidence={"verified_exports": 1},
    )
    state = await evaluation_migration.advance_phase("contract")
    assert state["phase"] == "contract"


@pytest.mark.asyncio
async def test_the_authority_snapshot_reports_the_evidence(authority_db):
    await facade.read_run_plan("run-base")
    snapshot = await evaluation_migration.authority_snapshot()
    assert snapshot["phase"] == "expand"
    assert snapshot["fallback_events"] == 1
    assert snapshot["digest_mismatch_events"] == 0
    assert snapshot["direct_legacy_call_events"] == 0
    metrics = facade.metrics_snapshot()
    assert metrics["dual_read_fallbacks"]["run-plan"] == 1


def test_the_legacy_package_performs_no_canonical_write():
    """Scan the legacy package for prohibited write entry points."""
    legacy_root = Path(__file__).resolve().parents[2] / "eval"
    prohibited = (
        re.compile(r"INSERT\s+INTO", re.IGNORECASE),
        re.compile(r"UPDATE\s+\w+\s+SET", re.IGNORECASE),
        re.compile(r"DELETE\s+FROM", re.IGNORECASE),
        re.compile(r"import\s+aiosqlite"),
        re.compile(r"import\s+sqlite3"),
        re.compile(r"import\s+database"),
        re.compile(r"from\s+benchmarks\s+import"),
    )
    offending: list[str] = []
    for source in sorted(legacy_root.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        for pattern in prohibited:
            if pattern.search(text):
                offending.append(f"{source.name}: {pattern.pattern}")
    assert offending == [], (
        "The legacy eval package writes no canonical record; it calls "
        f"the daemon API as a client. Offending: {offending}"
    )


def test_only_declared_modules_write_evaluation_tables():
    """The authority map pins one writer for every evaluation table."""
    import yaml

    map_path = (
        Path(__file__).resolve().parents[2]
        / "conformance/durable_authority/authority-map.yaml"
    )
    document = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    evaluation_tables = {
        table for table, _kind in evaluation_records.EXPANSION_TABLES
    } | {"evaluation_readonly_archive"}
    allowed = {
        "benchmarks.evaluation_records",
        "benchmarks.evaluation_migration",
        "database",
    }
    for entry in document["sqlite_tables"]:
        if entry["table"] in evaluation_tables:
            assert set(entry["writers"]) <= allowed, entry["table"]
