"""Persist versioned benchmark tests, runs, attempts, and scores."""

from __future__ import annotations

import json
import uuid
from contextlib import suppress
from typing import Any

import database as db
from benchmarks.provenance import content_checksum
from benchmarks.scoring import score_output


class BenchmarkConflict(RuntimeError):
    """A benchmark mutation conflicts with immutable or active state."""


class BenchmarkNotFound(LookupError):
    """A requested benchmark record does not exist."""


def _json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    with suppress(json.JSONDecodeError, TypeError):
        return json.loads(value)
    return fallback


def _record(row: Any, *json_columns: str) -> dict[str, Any]:
    result = dict(row)
    for column in json_columns:
        result[column] = _json(result.get(column), {})
    return result


async def list_scorers() -> list[dict[str, Any]]:
    """Return all registered immutable scorer versions."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM benchmark_scorers ORDER BY name, version"
        )
    return [_record(row, "configuration_schema") for row in rows]


async def dataset_version_item_count(version_id: str) -> int | None:
    """Return the item count for one published dataset version."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT item_count FROM dataset_versions "
            "WHERE id = ? AND status = 'published'",
            (version_id,),
        )
        row = await cursor.fetchone()
    return int(row["item_count"]) if row else None


async def create_test_revision(
    *,
    test_id: str,
    revision_id: str,
    name: str,
    description: str,
    dataset_version_id: str,
    configuration: dict[str, Any],
    arms: list[dict[str, Any]],
    scorers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create and publish one complete immutable test revision."""
    configuration_json = json.dumps(configuration, separators=(",", ":"), sort_keys=True)
    configuration_checksum = content_checksum(configuration)
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            dataset_cursor = await connection.execute(
                "SELECT id FROM dataset_versions WHERE id = ? AND status = 'published'",
                (dataset_version_id,),
            )
            if not await dataset_cursor.fetchone():
                raise BenchmarkNotFound("The published dataset version does not exist")

            scorer_ids = [str(item["id"]) for item in scorers]
            placeholders = ",".join("?" for _ in scorer_ids)
            scorer_rows = await connection.execute_fetchall(
                f"SELECT id FROM benchmark_scorers WHERE id IN ({placeholders})",
                scorer_ids,
            )
            if {row["id"] for row in scorer_rows} != set(scorer_ids):
                raise BenchmarkNotFound("One or more scorer versions do not exist")

            await connection.execute(
                "INSERT INTO benchmark_tests (id, name, description) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "description = excluded.description, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (test_id, name, description),
            )
            revision_cursor = await connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision "
                "FROM benchmark_test_revisions WHERE test_id = ?",
                (test_id,),
            )
            revision_row = await revision_cursor.fetchone()
            revision_number = int(revision_row["next_revision"] if revision_row else 1)
            await connection.execute(
                "INSERT INTO benchmark_test_revisions "
                "(id, test_id, revision, dataset_version_id, status, configuration, "
                "configuration_checksum) VALUES (?, ?, ?, ?, 'draft', ?, ?)",
                (
                    revision_id,
                    test_id,
                    revision_number,
                    dataset_version_id,
                    configuration_json,
                    configuration_checksum,
                ),
            )
            await connection.executemany(
                "INSERT INTO benchmark_test_arms "
                "(id, test_revision_id, name, slug, runtime_id, configuration, "
                "configuration_checksum, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        arm["id"],
                        revision_id,
                        arm["name"],
                        arm["slug"],
                        arm["runtime_id"],
                        json.dumps(arm["configuration"], separators=(",", ":"), sort_keys=True),
                        arm["configuration_checksum"],
                        index,
                    )
                    for index, arm in enumerate(arms)
                ],
            )
            await connection.executemany(
                "INSERT INTO benchmark_test_revision_scorers "
                "(test_revision_id, scorer_id, sort_order, configuration, "
                "configuration_checksum) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        revision_id,
                        scorer["id"],
                        index,
                        json.dumps(
                            scorer.get("configuration", {}),
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        content_checksum(scorer.get("configuration", {})),
                    )
                    for index, scorer in enumerate(scorers)
                ],
            )
            await connection.execute(
                "UPDATE benchmark_test_revisions SET status = 'published', "
                "published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND status = 'draft'",
                (revision_id,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result = await get_test(test_id)
    if result is None:
        raise RuntimeError("The benchmark test disappeared after publication")
    return result


async def list_tests(
    *,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return one searchable page of benchmark tests."""
    clauses = ["test.archived_at IS NULL"]
    params: list[Any] = []
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append("(LOWER(test.name) LIKE ? OR LOWER(test.description) LIKE ?)")
        params.extend((pattern, pattern))
    where = f"WHERE {' AND '.join(clauses)}"
    bounded_limit = min(max(limit, 1), 200)
    bounded_offset = max(offset, 0)
    async with db._connect() as connection:  # noqa: SLF001
        count_cursor = await connection.execute(
            f"SELECT COUNT(*) AS count FROM benchmark_tests AS test {where}", params
        )
        count_row = await count_cursor.fetchone()
        rows = await connection.execute_fetchall(
            "SELECT test.*, revision.id AS latest_revision_id, "
            "revision.revision AS latest_revision, revision.dataset_version_id, "
            "revision.configuration_checksum, revision.published_at, "
            "dataset.name AS dataset_name, version.item_count, "
            "(SELECT COUNT(*) FROM benchmark_test_arms AS arm "
            "WHERE arm.test_revision_id = revision.id) AS arm_count, "
            "(SELECT COUNT(*) FROM benchmark_runs AS run "
            "WHERE run.test_revision_id = revision.id) AS run_count "
            "FROM benchmark_tests AS test "
            "LEFT JOIN benchmark_test_revisions AS revision ON revision.id = ("
            "SELECT candidate.id FROM benchmark_test_revisions AS candidate "
            "WHERE candidate.test_id = test.id ORDER BY candidate.revision DESC LIMIT 1) "
            "LEFT JOIN dataset_versions AS version ON version.id = revision.dataset_version_id "
            "LEFT JOIN datasets AS dataset ON dataset.id = version.dataset_id "
            f"{where} ORDER BY test.updated_at DESC LIMIT ? OFFSET ?",
            [*params, bounded_limit, bounded_offset],
        )
    return [dict(row) for row in rows], int(count_row["count"] if count_row else 0)


async def _revision_children(
    connection: Any,
    revision_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    arms = await connection.execute_fetchall(
        "SELECT * FROM benchmark_test_arms WHERE test_revision_id = ? "
        "ORDER BY sort_order, id",
        (revision_id,),
    )
    scorers = await connection.execute_fetchall(
        "SELECT scorer.*, link.configuration, link.configuration_checksum, "
        "link.sort_order FROM benchmark_test_revision_scorers AS link "
        "JOIN benchmark_scorers AS scorer ON scorer.id = link.scorer_id "
        "WHERE link.test_revision_id = ? ORDER BY link.sort_order, scorer.id",
        (revision_id,),
    )
    runs = await connection.execute_fetchall(
        "SELECT * FROM benchmark_runs WHERE test_revision_id = ? "
        "ORDER BY created_at DESC LIMIT 50",
        (revision_id,),
    )
    return (
        [_record(row, "configuration") for row in arms],
        [_record(row, "configuration", "configuration_schema") for row in scorers],
        [_record(row, "execution_plan") for row in runs],
    )


async def get_test(test_id: str) -> dict[str, Any] | None:
    """Return one benchmark test with revisions, arms, scorers, and runs."""
    async with db._connect() as connection:  # noqa: SLF001
        test_cursor = await connection.execute(
            "SELECT * FROM benchmark_tests WHERE id = ?", (test_id,)
        )
        test_row = await test_cursor.fetchone()
        if not test_row:
            return None
        revision_rows = await connection.execute_fetchall(
            "SELECT revision.*, dataset.id AS dataset_id, dataset.name AS dataset_name, "
            "version.version AS dataset_version, version.checksum AS dataset_checksum, "
            "version.item_count FROM benchmark_test_revisions AS revision "
            "JOIN dataset_versions AS version ON version.id = revision.dataset_version_id "
            "JOIN datasets AS dataset ON dataset.id = version.dataset_id "
            "WHERE revision.test_id = ? ORDER BY revision.revision DESC",
            (test_id,),
        )
        revisions: list[dict[str, Any]] = []
        for row in revision_rows:
            revision = _record(row, "configuration")
            arms, scorers, runs = await _revision_children(connection, revision["id"])
            revision["arms"] = arms
            revision["scorers"] = scorers
            revision["runs"] = runs
            revisions.append(revision)
    result = _record(test_row, "metadata")
    result["revisions"] = revisions
    return result


async def create_run(
    *,
    run_id: str,
    revision_id: str,
    test_id: str | None = None,
    idempotency_key: str | None,
    operator_note: str = "",
) -> tuple[dict[str, Any], bool]:
    """Materialize one deterministic run and all initial attempts."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                existing_cursor = await connection.execute(
                    "SELECT run.id, run.test_revision_id, revision.test_id "
                    "FROM benchmark_runs AS run "
                    "JOIN benchmark_test_revisions AS revision "
                    "ON revision.id = run.test_revision_id "
                    "WHERE run.idempotency_key = ?",
                    (idempotency_key,),
                )
                existing = await existing_cursor.fetchone()
                if existing:
                    if existing["test_revision_id"] != revision_id or (
                        test_id is not None and existing["test_id"] != test_id
                    ):
                        raise BenchmarkConflict(
                            "The idempotency key belongs to a different test revision"
                        )
                    await connection.rollback()
                    result = await get_run(existing["id"])
                    if result is None:
                        raise RuntimeError("The idempotent run disappeared")
                    return result, False

            revision_cursor = await connection.execute(
                "SELECT revision.*, version.checksum AS dataset_checksum "
                "FROM benchmark_test_revisions AS revision "
                "JOIN dataset_versions AS version ON version.id = revision.dataset_version_id "
                "WHERE revision.id = ? AND revision.status = 'published' "
                "AND (? IS NULL OR revision.test_id = ?)",
                (revision_id, test_id, test_id),
            )
            revision_row = await revision_cursor.fetchone()
            if not revision_row:
                raise BenchmarkNotFound("The published test revision does not exist")
            revision = _record(revision_row, "configuration")
            arms = await connection.execute_fetchall(
                "SELECT * FROM benchmark_test_arms WHERE test_revision_id = ? "
                "ORDER BY sort_order, id",
                (revision_id,),
            )
            items = await connection.execute_fetchall(
                "SELECT * FROM dataset_items WHERE dataset_version_id = ? "
                "ORDER BY sort_order, id",
                (revision["dataset_version_id"],),
            )
            scorer_rows = await connection.execute_fetchall(
                "SELECT scorer_id, configuration_checksum "
                "FROM benchmark_test_revision_scorers WHERE test_revision_id = ? "
                "ORDER BY sort_order, scorer_id",
                (revision_id,),
            )
            if not arms or not items or not scorer_rows:
                raise BenchmarkConflict(
                    "The test revision needs an arm, dataset item, and scorer"
                )

            repetitions = int(revision["configuration"].get("repetitions", 1))
            base_seed = int(revision["configuration"].get("seed", 0))
            plan = {
                "schema_version": "1",
                "test_revision_id": revision_id,
                "revision_checksum": revision["configuration_checksum"],
                "dataset_version_id": revision["dataset_version_id"],
                "dataset_checksum": revision["dataset_checksum"],
                "repetitions": repetitions,
                "seed": base_seed,
                "arms": [
                    {
                        "id": arm["id"],
                        "runtime_id": arm["runtime_id"],
                        "configuration_checksum": arm["configuration_checksum"],
                    }
                    for arm in arms
                ],
                "scorers": [
                    {
                        "id": scorer["scorer_id"],
                        "configuration_checksum": scorer["configuration_checksum"],
                    }
                    for scorer in scorer_rows
                ],
            }
            plan_checksum = content_checksum(plan)
            total_trials = len(arms) * len(items)
            total_attempts = total_trials * repetitions
            await connection.execute(
                "INSERT INTO benchmark_runs "
                "(id, test_revision_id, status, execution_plan, "
                "execution_plan_checksum, total_trials, total_attempts, "
                "operator_note, idempotency_key) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    revision_id,
                    json.dumps(plan, separators=(",", ":"), sort_keys=True),
                    plan_checksum,
                    total_trials,
                    total_attempts,
                    operator_note,
                    idempotency_key,
                ),
            )
            for arm_index, arm_row in enumerate(arms):
                arm = _record(arm_row, "configuration")
                for item_index, item in enumerate(items):
                    trial_id = f"trial-{uuid.uuid4().hex}"
                    await connection.execute(
                        "INSERT INTO benchmark_trials "
                        "(id, run_id, test_arm_id, dataset_item_id) VALUES (?, ?, ?, ?)",
                        (trial_id, run_id, arm["id"], item["id"]),
                    )
                    for repeat_index in range(1, repetitions + 1):
                        attempt_id = f"attempt-{uuid.uuid4().hex}"
                        random_seed = (
                            base_seed
                            + arm_index * 1_000_000
                            + item_index * 1_000
                            + repeat_index
                        )
                        snapshot = {
                            "schema_version": "1",
                            "run_id": run_id,
                            "trial_id": trial_id,
                            "attempt_id": attempt_id,
                            "repeat_index": repeat_index,
                            "retry_index": 0,
                            "random_seed": random_seed,
                            "runtime_id": arm["runtime_id"],
                            "runtime_configuration": arm["configuration"],
                            "dataset_item_id": item["id"],
                        }
                        await connection.execute(
                            "INSERT INTO benchmark_attempts "
                            "(id, trial_id, attempt_number, repeat_index, retry_index, "
                            "random_seed, execution_snapshot, snapshot_checksum) "
                            "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                            (
                                attempt_id,
                                trial_id,
                                repeat_index,
                                repeat_index,
                                random_seed,
                                json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
                                content_checksum(snapshot),
                            ),
                        )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result = await get_run(run_id)
    if result is None:
        raise RuntimeError("The benchmark run disappeared after creation")
    return result, True


async def list_runs(
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return one benchmark run page with test identity."""
    clauses = ["run.archived_at IS NULL"]
    params: list[Any] = []
    if status:
        clauses.append("run.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}"
    bounded_limit = min(max(limit, 1), 200)
    bounded_offset = max(offset, 0)
    async with db._connect() as connection:  # noqa: SLF001
        count_cursor = await connection.execute(
            f"SELECT COUNT(*) AS count FROM benchmark_runs AS run {where}", params
        )
        count_row = await count_cursor.fetchone()
        rows = await connection.execute_fetchall(
            "SELECT run.*, test.id AS test_id, test.name AS test_name, "
            "revision.revision, dataset.name AS dataset_name, "
            "COALESCE((SELECT SUM(task.total_cost_usd) FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "JOIN tasks AS task ON task.id = attempt.task_id "
            "WHERE trial.run_id = run.id), 0) AS total_cost_usd "
            "FROM benchmark_runs AS run "
            "JOIN benchmark_test_revisions AS revision ON revision.id = run.test_revision_id "
            "JOIN benchmark_tests AS test ON test.id = revision.test_id "
            "JOIN dataset_versions AS version ON version.id = revision.dataset_version_id "
            "JOIN datasets AS dataset ON dataset.id = version.dataset_id "
            f"{where} ORDER BY run.created_at DESC LIMIT ? OFFSET ?",
            [*params, bounded_limit, bounded_offset],
        )
    return (
        [_record(row, "execution_plan") for row in rows],
        int(count_row["count"] if count_row else 0),
    )


async def get_run(run_id: str) -> dict[str, Any] | None:
    """Return one run with trials, attempts, scores, and provenance."""
    async with db._connect() as connection:  # noqa: SLF001
        run_cursor = await connection.execute(
            "SELECT run.*, test.id AS test_id, test.name AS test_name, "
            "revision.revision, revision.configuration AS test_configuration, "
            "revision.configuration_checksum AS test_configuration_checksum, "
            "dataset.id AS dataset_id, dataset.name AS dataset_name, "
            "version.version AS dataset_version, version.checksum AS dataset_checksum "
            "FROM benchmark_runs AS run "
            "JOIN benchmark_test_revisions AS revision ON revision.id = run.test_revision_id "
            "JOIN benchmark_tests AS test ON test.id = revision.test_id "
            "JOIN dataset_versions AS version ON version.id = revision.dataset_version_id "
            "JOIN datasets AS dataset ON dataset.id = version.dataset_id "
            "WHERE run.id = ?",
            (run_id,),
        )
        run_row = await run_cursor.fetchone()
        if not run_row:
            return None
        rows = await connection.execute_fetchall(
            "SELECT trial.id AS trial_id, trial.status AS trial_status, "
            "item.id AS dataset_item_id, item.item_key, item.input, "
            "item.expected_output, item.subject, item.split, item.tags, "
            "arm.id AS arm_id, arm.name AS arm_name, arm.slug AS arm_slug, "
            "arm.runtime_id, attempt.*, task.result_summary, task.total_cost_usd, "
            "task.total_tokens, task.duration_ms, task.model_used, "
            "task.terminal_kind AS task_terminal_kind "
            "FROM benchmark_trials AS trial "
            "JOIN dataset_items AS item ON item.id = trial.dataset_item_id "
            "JOIN benchmark_test_arms AS arm ON arm.id = trial.test_arm_id "
            "JOIN benchmark_attempts AS attempt ON attempt.trial_id = trial.id "
            "LEFT JOIN tasks AS task ON task.id = attempt.task_id "
            "WHERE trial.run_id = ? "
            "ORDER BY arm.sort_order, item.sort_order, attempt.repeat_index, "
            "attempt.retry_index",
            (run_id,),
        )
        scores = await connection.execute_fetchall(
            "SELECT score.*, scorer.name AS scorer_name, scorer.version AS scorer_version, "
            "scorer.kind AS scorer_kind FROM benchmark_scores AS score "
            "JOIN benchmark_scorers AS scorer ON scorer.id = score.scorer_id "
            "JOIN benchmark_attempts AS attempt ON attempt.id = score.attempt_id "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ? ORDER BY score.created_at, score.id",
            (run_id,),
        )
    result = _record(run_row, "execution_plan", "test_configuration")
    result["attempts"] = [
        _record(row, "execution_snapshot", "tags") for row in rows
    ]
    result["scores"] = [_record(row, "evidence") for row in scores]
    return result


async def count_active_attempts() -> int:
    """Return the number of benchmark attempts awaiting task completion."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM benchmark_attempts WHERE status = 'running'"
        )
        row = await cursor.fetchone()
    return int(row["count"] if row else 0)


async def claim_next_attempt() -> dict[str, Any] | None:
    """Claim the next queued attempt within its per-run concurrency limit."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            rows = await connection.execute_fetchall(
                "SELECT attempt.*, trial.run_id, trial.dataset_item_id, "
                "arm.runtime_id, arm.configuration AS arm_configuration, "
                "item.input, revision.configuration AS test_configuration, "
                "run.status AS run_status, "
                "(SELECT COUNT(*) FROM benchmark_attempts AS active_attempt "
                "JOIN benchmark_trials AS active_trial ON active_trial.id = active_attempt.trial_id "
                "WHERE active_trial.run_id = run.id AND active_attempt.status = 'running') "
                "AS active_count "
                "FROM benchmark_attempts AS attempt "
                "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                "JOIN benchmark_runs AS run ON run.id = trial.run_id "
                "JOIN benchmark_test_arms AS arm ON arm.id = trial.test_arm_id "
                "JOIN dataset_items AS item ON item.id = trial.dataset_item_id "
                "JOIN benchmark_test_revisions AS revision ON revision.id = run.test_revision_id "
                "WHERE attempt.status = 'queued' AND run.status IN ('queued','running') "
                "ORDER BY run.created_at, arm.sort_order, item.sort_order, "
                "attempt.repeat_index LIMIT 100"
            )
            selected = None
            for row in rows:
                configuration = _json(row["test_configuration"], {})
                limit = max(1, min(int(configuration.get("max_concurrency", 1)), 32))
                if int(row["active_count"] or 0) < limit:
                    selected = row
                    break
            if selected is None:
                await connection.rollback()
                return None
            cursor = await connection.execute(
                "UPDATE benchmark_attempts SET status = 'running', "
                "claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "WHERE id = ? AND status = 'queued'",
                (selected["id"],),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                return None
            await connection.execute(
                "UPDATE benchmark_trials SET status = 'running', "
                "started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "WHERE id = ?",
                (selected["trial_id"],),
            )
            await connection.execute(
                "UPDATE benchmark_runs SET status = 'running', "
                "started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "state_revision = state_revision + 1 WHERE id = ? AND status = 'queued'",
                (selected["run_id"],),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result = _record(selected, "arm_configuration", "test_configuration", "execution_snapshot")
    return result


async def attach_attempt_task(
    attempt_id: str,
    task_id: str,
    execution_snapshot: dict[str, Any],
    snapshot_checksum: str,
) -> None:
    """Attach the admitted task and its actual configuration snapshot."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET task_id = ?, execution_snapshot = ?, "
            "snapshot_checksum = ? WHERE id = ? AND status = 'running' AND task_id IS NULL",
            (
                task_id,
                json.dumps(execution_snapshot, separators=(",", ":"), sort_keys=True),
                snapshot_checksum,
                attempt_id,
            ),
        )
        await connection.commit()
        if cursor.rowcount != 1:
            raise BenchmarkConflict("The attempt no longer accepts a task")


async def release_attempt(attempt_id: str, error: str | None = None) -> None:
    """Return a temporary admission failure to the durable queue."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_attempts SET status = 'queued', claimed_at = NULL, "
            "started_at = NULL, error_message = ? "
            "WHERE id = ? AND status = 'running' AND task_id IS NULL",
            (error[:1000] if error else None, attempt_id),
        )
        await connection.commit()


async def fail_unadmitted_attempt(
    attempt_id: str,
    category: str,
    error: str,
) -> None:
    """Fail an attempt that cannot satisfy its runtime contract."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_attempts SET status = 'failed', failure_category = ?, "
            "error_message = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND task_id IS NULL",
            (category, error[:2000], attempt_id),
        )
        await connection.commit()
    await refresh_run_for_attempt(attempt_id)


async def fail_active_attempt(
    attempt_id: str,
    category: str,
    error: str,
) -> None:
    """Fail an admitted attempt and exclude it from deterministic scoring."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "UPDATE benchmark_attempts SET status = 'failed', failure_category = ?, "
                "error_message = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND status = 'running'",
                (category, error[:2000], attempt_id),
            )
            if cursor.rowcount == 1:
                scorers = await _attempt_scorers(connection, attempt_id)
                await connection.executemany(
                    "INSERT INTO benchmark_scores "
                    "(id, attempt_id, scorer_id, status, explanation, evidence) "
                    "VALUES (?, ?, ?, 'excluded', ?, ?) "
                    "ON CONFLICT(attempt_id, scorer_id) DO NOTHING",
                    [
                        (
                            f"score-{uuid.uuid4().hex}",
                            attempt_id,
                            scorer["id"],
                            error[:1000],
                            json.dumps({"failure_category": category}),
                        )
                        for scorer in scorers
                    ],
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    await refresh_run_for_attempt(attempt_id)


async def active_attempts() -> list[dict[str, Any]]:
    """Return active benchmark attempts with task and limit state."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT attempt.*, trial.run_id, run.status AS run_status, "
            "revision.configuration AS test_configuration, task.status AS task_status, "
            "task.terminal_kind, task.result_summary, task.error_message AS task_error, "
            "task.total_cost_usd, task.started_at AS task_started_at "
            ", COALESCE((SELECT SUM(cost_task.total_cost_usd) "
            "FROM benchmark_attempts AS cost_attempt "
            "JOIN benchmark_trials AS cost_trial ON cost_trial.id = cost_attempt.trial_id "
            "JOIN tasks AS cost_task ON cost_task.id = cost_attempt.task_id "
            "WHERE cost_trial.run_id = run.id), 0) AS run_cost_usd "
            "FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "JOIN benchmark_runs AS run ON run.id = trial.run_id "
            "JOIN benchmark_test_revisions AS revision ON revision.id = run.test_revision_id "
            "LEFT JOIN tasks AS task ON task.id = attempt.task_id "
            "WHERE attempt.status = 'running'"
        )
    return [_record(row, "test_configuration", "execution_snapshot") for row in rows]


async def _attempt_scorers(
    connection: Any,
    attempt_id: str,
) -> list[dict[str, Any]]:
    rows = await connection.execute_fetchall(
        "SELECT scorer.*, link.configuration FROM benchmark_scorers AS scorer "
        "JOIN benchmark_test_revision_scorers AS link ON link.scorer_id = scorer.id "
        "JOIN benchmark_runs AS run ON run.test_revision_id = link.test_revision_id "
        "JOIN benchmark_trials AS trial ON trial.run_id = run.id "
        "JOIN benchmark_attempts AS attempt ON attempt.trial_id = trial.id "
        "WHERE attempt.id = ? ORDER BY link.sort_order, scorer.id",
        (attempt_id,),
    )
    return [_record(row, "configuration", "configuration_schema") for row in rows]


async def finish_attempt_from_task(attempt_id: str) -> bool:
    """Persist terminal task state and all versioned scorer results."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT attempt.*, task.status AS task_status, task.terminal_kind, "
                "task.result_summary, task.error_message AS task_error, "
                "item.expected_output FROM benchmark_attempts AS attempt "
                "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                "JOIN dataset_items AS item ON item.id = trial.dataset_item_id "
                "LEFT JOIN tasks AS task ON task.id = attempt.task_id "
                "WHERE attempt.id = ? AND attempt.status = 'running'",
                (attempt_id,),
            )
            row = await cursor.fetchone()
            if not row or row["task_status"] not in {"completed", "failed"}:
                await connection.rollback()
                return False
            terminal_kind = str(row["terminal_kind"] or row["task_status"])
            attempt_status = terminal_kind if terminal_kind in {"completed", "cancelled"} else "failed"
            failure_category = None if attempt_status == "completed" else (
                "cancelled" if attempt_status == "cancelled" else "execution"
            )
            await connection.execute(
                "UPDATE benchmark_attempts SET status = ?, failure_category = ?, "
                "error_message = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (attempt_status, failure_category, row["task_error"], attempt_id),
            )
            scorers = await _attempt_scorers(connection, attempt_id)
            for scorer in scorers:
                if attempt_status == "completed":
                    try:
                        score = score_output(
                            scorer=scorer,
                            expected_output=str(row["expected_output"] or ""),
                            actual_output=str(row["result_summary"] or ""),
                        )
                    except Exception as error:
                        score = {
                            "status": "error",
                            "score": None,
                            "passed": None,
                            "extracted_output": None,
                            "explanation": str(error)[:1000],
                            "evidence": {},
                        }
                else:
                    score = {
                        "status": "excluded",
                        "score": None,
                        "passed": None,
                        "extracted_output": None,
                        "explanation": f"Attempt ended as {attempt_status}",
                        "evidence": {"failure_category": failure_category},
                    }
                await connection.execute(
                    "INSERT INTO benchmark_scores "
                    "(id, attempt_id, scorer_id, status, score, passed, "
                    "extracted_output, explanation, evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(attempt_id, scorer_id) DO NOTHING",
                    (
                        f"score-{uuid.uuid4().hex}",
                        attempt_id,
                        scorer["id"],
                        score["status"],
                        score["score"],
                        None if score["passed"] is None else int(score["passed"]),
                        score["extracted_output"],
                        score["explanation"],
                        json.dumps(score["evidence"], separators=(",", ":"), sort_keys=True),
                    ),
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    await refresh_run_for_attempt(attempt_id)
    return True


async def _refresh_trial(connection: Any, trial_id: str) -> None:
    latest_rows = await connection.execute_fetchall(
        "SELECT attempt.status FROM benchmark_attempts AS attempt "
        "WHERE attempt.trial_id = ? AND attempt.retry_index = ("
        "SELECT MAX(candidate.retry_index) FROM benchmark_attempts AS candidate "
        "WHERE candidate.trial_id = attempt.trial_id "
        "AND candidate.repeat_index = attempt.repeat_index)",
        (trial_id,),
    )
    statuses = [str(row["status"]) for row in latest_rows]
    terminal = {"completed", "failed", "cancelled", "excluded"}
    if statuses and all(status in terminal for status in statuses):
        if all(status == "completed" for status in statuses):
            status = "completed"
        elif all(status == "cancelled" for status in statuses):
            status = "cancelled"
        else:
            status = "failed"
        completed_at = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    elif any(status == "running" for status in statuses):
        status = "running"
        completed_at = "NULL"
    else:
        status = "queued"
        completed_at = "NULL"
    await connection.execute(
        f"UPDATE benchmark_trials SET status = ?, completed_at = {completed_at} WHERE id = ?",
        (status, trial_id),
    )


async def refresh_run_for_attempt(attempt_id: str) -> None:
    """Recalculate one trial and run from the latest attempt per repetition."""
    async with db._connect() as connection:  # noqa: SLF001
        trial_cursor = await connection.execute(
            "SELECT trial_id FROM benchmark_attempts WHERE id = ?", (attempt_id,)
        )
        trial_row = await trial_cursor.fetchone()
        if not trial_row:
            return
        trial_id = str(trial_row["trial_id"])
        await _refresh_trial(connection, trial_id)
        run_cursor = await connection.execute(
            "SELECT run_id FROM benchmark_trials WHERE id = ?", (trial_id,)
        )
        run_row = await run_cursor.fetchone()
        if not run_row:
            return
        await _refresh_run(connection, str(run_row["run_id"]))
        await connection.commit()


async def _refresh_run(connection: Any, run_id: str) -> None:
    status_cursor = await connection.execute(
        "SELECT status FROM benchmark_runs WHERE id = ?", (run_id,)
    )
    current = await status_cursor.fetchone()
    if not current:
        return
    latest_rows = await connection.execute_fetchall(
        "SELECT attempt.status FROM benchmark_attempts AS attempt "
        "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
        "WHERE trial.run_id = ? AND attempt.retry_index = ("
        "SELECT MAX(candidate.retry_index) FROM benchmark_attempts AS candidate "
        "WHERE candidate.trial_id = attempt.trial_id "
        "AND candidate.repeat_index = attempt.repeat_index)",
        (run_id,),
    )
    statuses = [str(row["status"]) for row in latest_rows]
    terminal = {"completed", "failed", "cancelled", "excluded"}
    complete_count = sum(status in terminal for status in statuses)
    next_status = str(current["status"])
    completed_at = None
    if statuses and complete_count == len(statuses):
        completed_at = True
        if next_status in {"cancelling", "cancelled"}:
            next_status = "cancelled"
        elif all(status == "completed" for status in statuses):
            next_status = "completed"
        elif all(status == "failed" for status in statuses):
            next_status = "failed"
        else:
            next_status = "partial"
    await connection.execute(
        "UPDATE benchmark_runs SET status = ?, completed_attempts = ?, "
        "completed_trials = (SELECT COUNT(*) FROM benchmark_trials "
        "WHERE run_id = ? AND status IN ('completed','failed','cancelled','excluded')), "
        "completed_at = CASE WHEN ? THEN "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END, "
        "state_revision = state_revision + 1 WHERE id = ?",
        (next_status, complete_count, run_id, int(bool(completed_at)), run_id),
    )


async def recover_orphan_attempts() -> int:
    """Return claimed attempts without tasks to the queue after restart."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET status = 'queued', claimed_at = NULL, "
            "started_at = NULL WHERE status = 'running' AND task_id IS NULL"
        )
        await connection.commit()
        return cursor.rowcount


async def set_run_state(
    run_id: str,
    action: str,
    *,
    cancel_reason: str = "operator_request",
) -> list[str]:
    """Pause, resume, or cancel one run and return active task identifiers."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT status FROM benchmark_runs WHERE id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if not row:
                raise BenchmarkNotFound("Benchmark run not found")
            current = str(row["status"])
            if action == "pause" and current in {"queued", "running"}:
                next_status = "paused"
            elif action == "resume" and current == "paused":
                next_status = "running"
            elif action == "cancel" and current in {"queued", "running", "paused"}:
                next_status = "cancelling"
                queued_rows = await connection.execute_fetchall(
                    "SELECT attempt.id, attempt.trial_id FROM benchmark_attempts AS attempt "
                    "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                    "WHERE trial.run_id = ? AND attempt.status = 'queued'",
                    (run_id,),
                )
                await connection.execute(
                    "UPDATE benchmark_attempts SET status = 'cancelled', "
                    "failure_category = 'cancelled', "
                    "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id IN (SELECT attempt.id FROM benchmark_attempts AS attempt "
                    "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                    "WHERE trial.run_id = ? AND attempt.status = 'queued')",
                    (run_id,),
                )
                for queued in queued_rows:
                    scorers = await _attempt_scorers(connection, str(queued["id"]))
                    await connection.executemany(
                        "INSERT INTO benchmark_scores "
                        "(id, attempt_id, scorer_id, status, explanation, evidence) "
                        "VALUES (?, ?, ?, 'excluded', 'Run cancelled before admission', ?) "
                        "ON CONFLICT(attempt_id, scorer_id) DO NOTHING",
                        [
                            (
                                f"score-{uuid.uuid4().hex}",
                                queued["id"],
                                scorer["id"],
                                json.dumps({"failure_category": "cancelled"}),
                            )
                            for scorer in scorers
                        ],
                    )
                    await _refresh_trial(connection, str(queued["trial_id"]))
            else:
                raise BenchmarkConflict(
                    f"The run cannot {action} from state {current}"
                )
            await connection.execute(
                "UPDATE benchmark_runs SET status = ?, cancel_reason = CASE "
                "WHEN ? = 'cancel' THEN ? ELSE cancel_reason END, "
                "state_revision = state_revision + 1 WHERE id = ?",
                (next_status, action, cancel_reason, run_id),
            )
            if action == "cancel":
                await _refresh_run(connection, run_id)
            task_rows = await connection.execute_fetchall(
                "SELECT attempt.task_id FROM benchmark_attempts AS attempt "
                "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                "WHERE trial.run_id = ? AND attempt.status = 'running' "
                "AND attempt.task_id IS NOT NULL",
                (run_id,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return [str(item["task_id"]) for item in task_rows]


async def retry_failed_attempts(run_id: str) -> int:
    """Create one new immutable retry for each failed latest repetition."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            run_cursor = await connection.execute(
                "SELECT status FROM benchmark_runs WHERE id = ?", (run_id,)
            )
            run = await run_cursor.fetchone()
            if not run:
                raise BenchmarkNotFound("Benchmark run not found")
            if run["status"] not in {"failed", "partial", "cancelled"}:
                raise BenchmarkConflict("Only a terminal run can retry failed attempts")
            rows = await connection.execute_fetchall(
                "SELECT attempt.* FROM benchmark_attempts AS attempt "
                "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                "WHERE trial.run_id = ? AND attempt.status IN ('failed','cancelled') "
                "AND attempt.retry_index = (SELECT MAX(candidate.retry_index) "
                "FROM benchmark_attempts AS candidate WHERE candidate.trial_id = attempt.trial_id "
                "AND candidate.repeat_index = attempt.repeat_index)",
                (run_id,),
            )
            for row in rows:
                prior_snapshot = _json(row["execution_snapshot"], {})
                planned_snapshot = prior_snapshot.get("benchmark_plan", prior_snapshot)
                snapshot = dict(planned_snapshot) if isinstance(planned_snapshot, dict) else {}
                retry_index = int(row["retry_index"]) + 1
                attempt_cursor = await connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS number "
                    "FROM benchmark_attempts WHERE trial_id = ?",
                    (row["trial_id"],),
                )
                number_row = await attempt_cursor.fetchone()
                attempt_id = f"attempt-{uuid.uuid4().hex}"
                snapshot.update({"attempt_id": attempt_id, "retry_index": retry_index})
                await connection.execute(
                    "INSERT INTO benchmark_attempts "
                    "(id, trial_id, attempt_number, repeat_index, retry_index, random_seed, "
                    "execution_snapshot, snapshot_checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        row["trial_id"],
                        int(number_row["number"]),
                        row["repeat_index"],
                        retry_index,
                        row["random_seed"],
                        json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
                        content_checksum(snapshot),
                    ),
                )
            if rows:
                await connection.execute(
                    "UPDATE benchmark_runs SET status = 'queued', completed_at = NULL, "
                    "cancel_reason = NULL, state_revision = state_revision + 1 WHERE id = ?",
                    (run_id,),
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return len(rows)
