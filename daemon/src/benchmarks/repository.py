"""Persist versioned benchmark tests, runs, attempts, and scores."""

from __future__ import annotations

import json
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import database as db
from benchmarks.provenance import content_checksum
from benchmarks.scoring import score_output

if TYPE_CHECKING:
    from benchmarks.capacity import CapacityPolicy

# The three frozen scheduler priority bands and their weighted
# round-robin weights. A larger weight receives proportionally more
# dispatch turns; the smallest weight still receives turns, so no band
# starves by construction.
PRIORITY_BANDS = ("expedited", "standard", "deferred")
PRIORITY_BAND_WEIGHTS = {"expedited": 4, "standard": 2, "deferred": 1}
PRIORITY_BAND_PROMOTION = {"deferred": "standard", "standard": "expedited"}
# The frozen starvation limit: an eligible run skipped this many times
# in a row promotes one band and records a scheduler event.
STARVATION_PROMOTION_LIMIT = 25
SEED_CONTROL_LABELS = ("recorded", "best_effort", "applied")


def priority_band_for(priority: int) -> str:
    """Map one numeric run priority onto a frozen priority band."""
    if priority >= 10:
        return "expedited"
    if priority < 0:
        return "deferred"
    return "standard"


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


def _validate_scorer_configuration(
    scorer_id: str,
    configuration: Any,
    schema: dict[str, Any],
) -> None:
    """Reject an invalid scorer configuration before publication."""
    import jsonschema

    if not isinstance(configuration, dict):
        raise BenchmarkConflict(
            f"The configuration for scorer {scorer_id} must be one object"
        )
    if not schema:
        if configuration:
            raise BenchmarkConflict(
                f"The scorer {scorer_id} declares no configuration schema "
                "and accepts only an empty configuration"
            )
        return
    try:
        jsonschema.validate(configuration, schema)
    except jsonschema.ValidationError as error:
        raise BenchmarkConflict(
            f"The configuration for scorer {scorer_id} is invalid: "
            f"{error.message}"
        ) from error


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
                f"SELECT id, configuration_schema FROM benchmark_scorers "
                f"WHERE id IN ({placeholders})",
                scorer_ids,
            )
            if {row["id"] for row in scorer_rows} != set(scorer_ids):
                raise BenchmarkNotFound("One or more scorer versions do not exist")
            # An invalid scorer configuration blocks publication. The
            # validated configuration is the complete effective
            # configuration that scoring receives.
            schemas = {
                str(row["id"]): _json(row["configuration_schema"], {})
                for row in scorer_rows
            }
            for item in scorers:
                _validate_scorer_configuration(
                    str(item["id"]),
                    item.get("configuration", {}),
                    schemas.get(str(item["id"])) or {},
                )

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
                "configuration_checksum, required) VALUES (?, ?, ?, ?, ?, ?)",
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
                        # A required scorer blocks a valid analysis
                        # when it fails; an optional scorer does not.
                        int(bool(scorer.get("required", True))),
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
        [_run_record(row, "execution_plan") for row in runs],
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


def _run_record(row: Any, *json_columns: str) -> dict[str, Any]:
    """Decode one run row; an absent cost amount stays null."""
    record = _record(row, *json_columns, "settled_cost", "cost_bound")
    for column in ("settled_cost", "cost_bound"):
        if record.get(column) == {}:
            record[column] = None
    return record


def _seed_control_label(runtime_id: str) -> str:
    """Read the declared seed support for one runtime.

    ``recorded`` means the runtime only records the seed.
    ``best_effort`` means the runtime forwards the seed without a
    determinism guarantee. ``applied`` means the provider applies the
    seed deterministically. An unknown runtime stays ``recorded``,
    the honest minimum.
    """
    from core import variants

    try:
        record = variants.capability_record(
            variants.resolve_runtime_key(runtime_id),
        )
    except variants.UnknownVariantError:
        return "recorded"
    label = str(
        (record.get("benchmark") or {}).get("seed_strategy") or "recorded"
    )
    return label if label in SEED_CONTROL_LABELS else "recorded"


def _frozen_estimand(
    configuration: dict[str, Any],
    *,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze the complete statistical estimand into the run plan.

    The estimand names the target population, the primary paired
    estimand, the task-family field, the family and case weights, and
    the predeclared repetition reductions. A zero-sum weight vector
    rejects here, before any admission.
    """
    statistics = configuration.get("statistics") or {}
    if not isinstance(statistics, dict):
        raise BenchmarkConflict(
            "The statistics configuration must be one object"
        )
    family_field = str(statistics.get("family_field") or "subject")
    families: dict[str, list[str]] = {}
    for item in items:
        family = str(item.get(family_field) or "default")
        families.setdefault(family, []).append(
            str(item.get("item_key") or item.get("id")),
        )

    raw_family_weights = statistics.get("family_weights") or {}
    family_weights: dict[str, float] = {}
    for name, weight in raw_family_weights.items():
        value = float(weight)
        if value < 0:
            raise BenchmarkConflict("A family weight cannot be negative")
        family_weights[str(name)] = value
    if family_weights and all(
        family_weights.get(name, 1.0) == 0 for name in families
    ):
        raise BenchmarkConflict(
            "The family weight vector sums to zero; the estimand covers "
            "no family"
        )

    raw_case_weights = statistics.get("case_weights") or {}
    case_weights: dict[str, float] = {}
    for name, weight in raw_case_weights.items():
        value = float(weight)
        if value < 0:
            raise BenchmarkConflict("A case weight cannot be negative")
        case_weights[str(name)] = value
    for family, keys in families.items():
        if family_weights and family_weights.get(family, 1.0) == 0:
            continue
        if case_weights and all(
            case_weights.get(key, 1.0) == 0 for key in keys
        ):
            raise BenchmarkConflict(
                f"Every case weight in the included family {family} is "
                "zero; the estimand covers no case there"
            )

    binary_reduction = str(
        statistics.get("binary_reduction") or "strict_majority"
    )
    if binary_reduction not in {"strict_majority", "all", "at_least_k"}:
        raise BenchmarkConflict(
            f"Unknown binary case reduction: {binary_reduction}"
        )
    at_least_k = statistics.get("at_least_k")
    if binary_reduction == "at_least_k":
        if not isinstance(at_least_k, int) or at_least_k < 1:
            raise BenchmarkConflict(
                "The at_least_k reduction needs one positive integer k"
            )
    else:
        at_least_k = None
    return {
        "target_population": "declared dataset cases",
        "primary_estimand": "paired-difference-in-weighted-case-means",
        "family_field": family_field,
        "families": {
            name: sorted(keys) for name, keys in sorted(families.items())
        },
        "family_weights": family_weights,
        "case_weights": case_weights,
        "binary_reduction": binary_reduction,
        "at_least_k": at_least_k,
        "fractional_reduction": "mean",
        "min_family_cases": int(statistics.get("min_family_cases") or 5),
    }


async def create_run(
    *,
    run_id: str,
    revision_id: str,
    test_id: str | None = None,
    idempotency_key: str | None,
    operator_note: str = "",
    priority: int = 0,
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
            estimand = _frozen_estimand(
                revision["configuration"],
                items=[dict(item) for item in items],
            )
            # One sorted outcome-mapping set per experiment. Admission
            # rejects here when any arm's runtime pair has no
            # registered mapping, and each arm pins its exact member.
            from benchmarks import outcome_mappings

            try:
                mapping_set = outcome_mappings.mapping_set_for_arms(
                    [dict(arm) for arm in arms],
                )
            except outcome_mappings.OutcomeMappingError as error:
                raise BenchmarkConflict(str(error)) from error
            plan = {
                "schema_version": "1",
                "test_revision_id": revision_id,
                "revision_checksum": revision["configuration_checksum"],
                "dataset_version_id": revision["dataset_version_id"],
                "dataset_checksum": revision["dataset_checksum"],
                "repetitions": repetitions,
                "seed": base_seed,
                "seed_scope": "item-repetition",
                "arm_order": {
                    "strategy": "rotated_interleave",
                    "rotation": "slot_index_modulo_arm_count",
                },
                "estimand": estimand,
                "outcome_mapping_set": mapping_set,
                "arms": [
                    {
                        "id": arm["id"],
                        "runtime_id": arm["runtime_id"],
                        "configuration_checksum": arm["configuration_checksum"],
                        "outcome_mapping": outcome_mappings.member_for_arm(
                            mapping_set, str(arm["runtime_id"]),
                        ),
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
                "operator_note, idempotency_key, priority, priority_band) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    revision_id,
                    json.dumps(plan, separators=(",", ":"), sort_keys=True),
                    plan_checksum,
                    total_trials,
                    total_attempts,
                    operator_note,
                    idempotency_key,
                    priority,
                    priority_band_for(priority),
                ),
            )
            arm_count = len(arms)
            seed_controls = {
                str(arm["runtime_id"]): _seed_control_label(
                    str(arm["runtime_id"]),
                )
                for arm in arms
            }
            for arm_index, arm_row in enumerate(arms):
                arm = _record(arm_row, "configuration")
                seed_control = seed_controls[str(arm["runtime_id"])]
                for item_index, item in enumerate(items):
                    trial_id = f"trial-{uuid.uuid4().hex}"
                    await connection.execute(
                        "INSERT INTO benchmark_trials "
                        "(id, run_id, test_arm_id, dataset_item_id) VALUES (?, ?, ?, ?)",
                        (trial_id, run_id, arm["id"], item["id"]),
                    )
                    for repeat_index in range(1, repetitions + 1):
                        attempt_id = f"attempt-{uuid.uuid4().hex}"
                        # One shared seed per case and repetition. Every
                        # arm receives the same value, so paired slots
                        # keep the case, repetition, and seed relation.
                        random_seed = (
                            base_seed + item_index * 1_000 + repeat_index
                        )
                        # The stored arm-order schedule. Each slot
                        # rotates its arm order, and the rank
                        # interleaves arms inside the run.
                        slot_index = (
                            item_index * repetitions + repeat_index - 1
                        )
                        arm_position = (
                            arm_index - slot_index
                        ) % arm_count
                        schedule_rank = slot_index * arm_count + arm_position
                        snapshot = {
                            "schema_version": "1",
                            "run_id": run_id,
                            "trial_id": trial_id,
                            "attempt_id": attempt_id,
                            "repeat_index": repeat_index,
                            "retry_index": 0,
                            "random_seed": random_seed,
                            "seed_scope": "item-repetition",
                            "seed_control": seed_control,
                            "runtime_id": arm["runtime_id"],
                            "runtime_configuration": arm["configuration"],
                            "dataset_item_id": item["id"],
                        }
                        await connection.execute(
                            "INSERT INTO benchmark_attempts "
                            "(id, trial_id, attempt_number, repeat_index, retry_index, "
                            "random_seed, seed_control, schedule_rank, "
                            "execution_snapshot, snapshot_checksum) "
                            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                            (
                                attempt_id,
                                trial_id,
                                repeat_index,
                                repeat_index,
                                random_seed,
                                seed_control,
                                schedule_rank,
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


# One shared cost aggregation for the run list and the run detail. It
# sums every attempt's task cost, so failed attempts and retries count.
_RUN_COST_SQL = (
    "COALESCE((SELECT SUM(task.total_cost_usd) "
    "FROM benchmark_attempts AS attempt "
    "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
    "JOIN tasks AS task ON task.id = attempt.task_id "
    "WHERE trial.run_id = run.id), 0)"
)

# The named primary metric: the first required scorer by sort order.
_PRIMARY_SCORER_SQL = (
    "(SELECT link.scorer_id FROM benchmark_test_revision_scorers AS link "
    "WHERE link.test_revision_id = run.test_revision_id "
    "AND link.required = 1 "
    "ORDER BY link.sort_order, link.scorer_id LIMIT 1)"
)

# One row per current attempt: the highest retry per repetition slot.
_CURRENT_ATTEMPT_SQL = (
    "attempt.retry_index = (SELECT MAX(candidate.retry_index) "
    "FROM benchmark_attempts AS candidate "
    "WHERE candidate.trial_id = attempt.trial_id "
    "AND candidate.repeat_index = attempt.repeat_index)"
)

_PRIMARY_METRIC_MEAN_SQL = (
    "(SELECT AVG(score.score) FROM benchmark_scores AS score "
    "JOIN benchmark_attempts AS attempt ON attempt.id = score.attempt_id "
    "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
    "WHERE trial.run_id = run.id AND score.status = 'scored' "
    "AND score.score IS NOT NULL "
    f"AND score.scorer_id = {_PRIMARY_SCORER_SQL} "
    f"AND {_CURRENT_ATTEMPT_SQL})"
)

_PRIMARY_METRIC_COUNT_SQL = _PRIMARY_METRIC_MEAN_SQL.replace(
    "AVG(score.score)", "COUNT(score.score)", 1,
)

_FAILED_ATTEMPT_COUNT_SQL = (
    "(SELECT COUNT(*) FROM benchmark_attempts AS attempt "
    "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
    "WHERE trial.run_id = run.id AND attempt.status = 'failed' "
    f"AND {_CURRENT_ATTEMPT_SQL})"
)

# One current attempt of a required scorer failed to score.
_REQUIRED_SCORER_ERROR_SQL = (
    "EXISTS(SELECT 1 FROM benchmark_scores AS score "
    "JOIN benchmark_attempts AS attempt ON attempt.id = score.attempt_id "
    "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
    "JOIN benchmark_test_revision_scorers AS link "
    "ON link.test_revision_id = run.test_revision_id "
    "AND link.scorer_id = score.scorer_id "
    "WHERE trial.run_id = run.id AND link.required = 1 "
    "AND score.status = 'error' "
    f"AND {_CURRENT_ATTEMPT_SQL})"
)

_TERMINAL_RUN_STATUSES = {"completed", "partial", "failed", "cancelled"}


def _effective_statuses(
    run_status: str,
    scoring_status: str,
    analysis_status: str,
    has_required_scorer_error: bool,
) -> tuple[str, str]:
    """Derive the effective scoring and analysis statuses of one run.

    Execution completion stays a separate state. A failed required
    scorer fails scoring and blocks a valid analysis immediately. A
    legacy terminal row with the pending default derives completed
    scoring, so a pre-migration run keeps its old behavior.
    """
    terminal = run_status in _TERMINAL_RUN_STATUSES
    scoring = scoring_status
    if has_required_scorer_error:
        scoring = "failed"
    elif terminal and scoring == "pending":
        scoring = "completed"
    analysis = analysis_status
    if scoring == "failed":
        analysis = "blocked"
    elif terminal and scoring == "completed":
        analysis = "valid"
    return scoring, analysis


def _apply_effective_statuses(record: dict[str, Any]) -> dict[str, Any]:
    scoring, analysis = _effective_statuses(
        str(record.get("status") or ""),
        str(record.get("scoring_status") or "pending"),
        str(record.get("analysis_status") or "pending"),
        bool(record.pop("has_required_scorer_error", 0)),
    )
    record["scoring_status"] = scoring
    record["analysis_status"] = analysis
    return record


async def list_runs(
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return one benchmark run page with server aggregates.

    Each row carries the shared cost total, the named primary metric,
    the failed-work count, and the effective scoring and analysis
    statuses, so no browser computes its own aggregate.
    """
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
            f"{_RUN_COST_SQL} AS total_cost_usd, "
            f"{_PRIMARY_SCORER_SQL} AS primary_scorer_id, "
            "(SELECT scorer.name FROM benchmark_scorers AS scorer "
            f"WHERE scorer.id = {_PRIMARY_SCORER_SQL}) AS primary_scorer_name, "
            f"{_PRIMARY_METRIC_MEAN_SQL} AS primary_metric_mean, "
            f"{_PRIMARY_METRIC_COUNT_SQL} AS primary_metric_count, "
            f"{_FAILED_ATTEMPT_COUNT_SQL} AS failed_attempts, "
            f"{_REQUIRED_SCORER_ERROR_SQL} AS has_required_scorer_error "
            "FROM benchmark_runs AS run "
            "JOIN benchmark_test_revisions AS revision ON revision.id = run.test_revision_id "
            "JOIN benchmark_tests AS test ON test.id = revision.test_id "
            "JOIN dataset_versions AS version ON version.id = revision.dataset_version_id "
            "JOIN datasets AS dataset ON dataset.id = version.dataset_id "
            f"{where} ORDER BY run.created_at DESC LIMIT ? OFFSET ?",
            [*params, bounded_limit, bounded_offset],
        )
    return (
        [
            _apply_effective_statuses(_run_record(row, "execution_plan"))
            for row in rows
        ],
        int(count_row["count"] if count_row else 0),
    )


async def get_run(run_id: str) -> dict[str, Any] | None:
    """Return one run with trials, attempts, scores, and provenance.

    The detail row uses the same shared cost and primary-metric
    aggregation as the run list, so the two totals always match.
    """
    async with db._connect() as connection:  # noqa: SLF001
        run_cursor = await connection.execute(
            "SELECT run.*, test.id AS test_id, test.name AS test_name, "
            "revision.revision, revision.configuration AS test_configuration, "
            "revision.configuration_checksum AS test_configuration_checksum, "
            "dataset.id AS dataset_id, dataset.name AS dataset_name, "
            "version.version AS dataset_version, version.checksum AS dataset_checksum, "
            f"{_RUN_COST_SQL} AS total_cost_usd, "
            f"{_PRIMARY_SCORER_SQL} AS primary_scorer_id, "
            "(SELECT scorer.name FROM benchmark_scorers AS scorer "
            f"WHERE scorer.id = {_PRIMARY_SCORER_SQL}) AS primary_scorer_name, "
            f"{_PRIMARY_METRIC_MEAN_SQL} AS primary_metric_mean, "
            f"{_PRIMARY_METRIC_COUNT_SQL} AS primary_metric_count, "
            f"{_FAILED_ATTEMPT_COUNT_SQL} AS failed_attempts, "
            f"{_REQUIRED_SCORER_ERROR_SQL} AS has_required_scorer_error "
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
        scorer_links = await connection.execute_fetchall(
            "SELECT scorer.id, scorer.name, scorer.version, "
            "link.configuration_checksum, link.sort_order, link.required "
            "FROM benchmark_test_revision_scorers AS link "
            "JOIN benchmark_scorers AS scorer ON scorer.id = link.scorer_id "
            "WHERE link.test_revision_id = ? "
            "ORDER BY link.sort_order, scorer.id",
            (str(run_row["test_revision_id"]),),
        )
        arm_rows = await connection.execute_fetchall(
            "SELECT id, name, slug, runtime_id, configuration, "
            "configuration_checksum FROM benchmark_test_arms "
            "WHERE test_revision_id = ? ORDER BY sort_order, id",
            (str(run_row["test_revision_id"]),),
        )
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
        reviews = await connection.execute_fetchall(
            "SELECT review.* FROM benchmark_human_reviews AS review "
            "JOIN benchmark_attempts AS attempt ON attempt.id = review.attempt_id "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ? ORDER BY review.created_at, review.id",
            (run_id,),
        )
    result = _apply_effective_statuses(
        _run_record(run_row, "execution_plan", "test_configuration"),
    )
    result["attempts"] = [
        _record(row, "execution_snapshot", "tags") for row in rows
    ]
    result["scores"] = [_record(row, "evidence") for row in scores]
    result["human_reviews"] = [dict(row) for row in reviews]
    result["revision_scorers"] = [dict(row) for row in scorer_links]
    result["arms"] = [_record(row, "configuration") for row in arm_rows]
    result["aggregates"] = _run_aggregates(result)
    return result


def _run_aggregates(run: dict[str, Any]) -> dict[str, Any]:
    """Build the server aggregates for one run detail.

    The primary metric names the first required scorer. Every other
    scorer reports separately as a secondary metric; no aggregate ever
    averages unrelated scorers together.
    """
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for attempt in run.get("attempts") or []:
        key = (str(attempt["trial_id"]), int(attempt.get("repeat_index") or 1))
        current = latest.get(key)
        if current is None or int(attempt.get("retry_index") or 0) > int(
            current.get("retry_index") or 0,
        ):
            latest[key] = attempt
    current_ids = {str(attempt["id"]) for attempt in latest.values()}
    by_scorer: dict[str, list[float]] = {}
    for score in run.get("scores") or []:
        if (
            str(score["attempt_id"]) in current_ids
            and score.get("status") == "scored"
            and score.get("score") is not None
        ):
            by_scorer.setdefault(str(score["scorer_id"]), []).append(
                float(score["score"]),
            )
    names = {
        str(link["id"]): str(link.get("name") or link["id"])
        for link in run.get("revision_scorers") or []
    }
    primary_id = run.get("primary_scorer_id")
    metrics = {
        scorer_id: {
            "scorer_id": scorer_id,
            "scorer_name": names.get(scorer_id, scorer_id),
            "mean": sum(values) / len(values),
            "count": len(values),
        }
        for scorer_id, values in sorted(by_scorer.items())
    }
    return {
        "total_cost_usd": float(run.get("total_cost_usd") or 0),
        "failed_attempts": int(run.get("failed_attempts") or 0),
        "primary_metric": metrics.get(str(primary_id)) if primary_id else None,
        "secondary_metrics": [
            metric
            for scorer_id, metric in metrics.items()
            if scorer_id != str(primary_id)
        ],
    }


async def create_human_review(
    *,
    review_id: str,
    attempt_id: str,
    reviewer_id: str,
    score: float,
    passed: bool,
    note: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """Save one immutable human review with retry-safe identity."""
    try:
        async with db._connect() as connection:  # noqa: SLF001
            attempt_cursor = await connection.execute(
                "SELECT status FROM benchmark_attempts WHERE id = ?",
                (attempt_id,),
            )
            attempt = await attempt_cursor.fetchone()
            if attempt is None:
                raise BenchmarkNotFound("The benchmark attempt does not exist")
            if attempt["status"] != "completed":
                raise BenchmarkConflict("Only a completed attempt accepts a human review")
            await connection.execute(
                "INSERT INTO benchmark_human_reviews "
                "(id, attempt_id, reviewer_id, score, passed, note, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    attempt_id,
                    reviewer_id,
                    score,
                    int(passed),
                    note,
                    idempotency_key,
                ),
            )
            await connection.commit()
    except Exception as error:
        if isinstance(error, (BenchmarkNotFound, BenchmarkConflict)):
            raise
        if "UNIQUE constraint failed" not in str(error):
            raise
        async with db._connect() as connection:  # noqa: SLF001
            cursor = await connection.execute(
                "SELECT * FROM benchmark_human_reviews WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            existing = await cursor.fetchone()
        if existing:
            same = (
                existing["attempt_id"] == attempt_id
                and existing["reviewer_id"] == reviewer_id
                and float(existing["score"]) == score
                and bool(existing["passed"]) is passed
                and existing["note"] == note
            )
            if not same:
                raise BenchmarkConflict(
                    "The idempotency key belongs to another human review"
                ) from error
            return dict(existing), False
        raise BenchmarkConflict(
            "This reviewer already submitted an immutable review"
        ) from error
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM benchmark_human_reviews WHERE id = ?",
            (review_id,),
        )
        saved = await cursor.fetchone()
    if saved is None:
        raise RuntimeError("The human review disappeared after creation")
    return dict(saved), True


async def count_active_attempts() -> int:
    """Return the number of benchmark attempts awaiting task completion."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM benchmark_attempts WHERE status = 'running'"
        )
        row = await cursor.fetchone()
    return int(row["count"] if row else 0)


_ATTEMPT_CONTEXT_SELECT = (
    "SELECT attempt.*, trial.run_id, trial.dataset_item_id, "
    "arm.runtime_id, arm.configuration AS arm_configuration, "
    "item.input, revision.configuration AS test_configuration, "
    "run.status AS run_status, run.priority, run.priority_band, "
    "run.dispatch_tickets, run.starvation_wait, "
    "run.budget_id AS run_budget_id, run.authority_run_id, "
    "run.authority_fence, run.total_attempts AS run_total_attempts, "
    "(SELECT COUNT(*) FROM benchmark_test_arms AS counted_arm "
    "WHERE counted_arm.test_revision_id = run.test_revision_id) AS arm_count, "
    "task.model_used AS task_model_used, "
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
    "LEFT JOIN tasks AS task ON task.id = attempt.task_id "
)


def _attempt_record(row: Any) -> dict[str, Any]:
    """Decode one scheduler attempt row."""
    return _record(
        row,
        "arm_configuration",
        "test_configuration",
        "execution_snapshot",
    )


async def claim_next_attempt(
    worker_id: str = "local",
    *,
    lease_seconds: int = 30,
    capacity_policy: CapacityPolicy | None = None,
) -> dict[str, Any] | None:
    """Claim one queued attempt with an atomic fenced lease."""
    from benchmarks.capacity import CapacityPolicy

    policy = capacity_policy or CapacityPolicy()
    lease_modifier = f"+{max(5, min(lease_seconds, 300))} seconds"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            active_rows = await connection.execute_fetchall(
                _ATTEMPT_CONTEXT_SELECT + "WHERE attempt.status = 'running'"
            )
            active = [_attempt_record(row) for row in active_rows]
            if len(active) >= policy.global_limit:
                await connection.rollback()
                return None
            rows = await connection.execute_fetchall(
                _ATTEMPT_CONTEXT_SELECT
                + "WHERE attempt.status = 'queued' "
                "AND run.status IN ('queued','running') "
                "ORDER BY trial.run_id, attempt.schedule_rank, "
                "attempt.retry_index, attempt.id LIMIT 500"
            )
            # The first eligible attempt per run, in stored arm-order
            # schedule position. The stored schedule decides the order
            # inside a run; the weighted round-robin decides the order
            # across runs.
            eligible: dict[str, Any] = {}
            for row in rows:
                run_key = str(row["run_id"])
                if run_key in eligible:
                    continue
                candidate = _attempt_record(row)
                configuration = candidate["test_configuration"]
                limit = max(
                    1,
                    min(int(configuration.get("max_concurrency", 1)), 32),
                )
                if int(candidate["active_count"] or 0) >= limit:
                    continue
                if policy.allows(candidate, active):
                    eligible[run_key] = row
            if not eligible:
                await connection.rollback()
                return None
            # One transactional weighted round-robin turn. The run with
            # the smallest consumed-tickets-per-weight value wins; the
            # stable digest breaks exact ties. An equal-priority run
            # that just took a turn carries a larger virtual time, so
            # it cannot take a second turn while an equal-priority run
            # waits.
            def _turn_order(run_key: str) -> tuple[float, int, str]:
                # Stride order: the next turn finishes at
                # (tickets + 1) / weight, so a heavier band starts
                # first and receives proportionally more turns.
                row = eligible[run_key]
                band = str(row["priority_band"] or "standard")
                weight = PRIORITY_BAND_WEIGHTS.get(band, 1)
                tickets = int(row["dispatch_tickets"] or 0)
                return (
                    (tickets + 1) / weight,
                    tickets,
                    content_checksum([run_key]),
                )

            selected_run = min(eligible, key=_turn_order)
            selected = eligible[selected_run]
            now_marker = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            for run_key, row in eligible.items():
                if run_key == selected_run:
                    continue
                waits = int(row["starvation_wait"] or 0) + 1
                band = str(row["priority_band"] or "standard")
                promoted = PRIORITY_BAND_PROMOTION.get(band)
                if waits >= STARVATION_PROMOTION_LIMIT and promoted:
                    await connection.execute(
                        "UPDATE benchmark_runs SET priority_band = ?, "
                        "starvation_wait = 0 WHERE id = ?",
                        (promoted, run_key),
                    )
                    await connection.execute(
                        "INSERT INTO benchmark_scheduler_events "
                        "(id, run_id, event_type, payload) VALUES (?, ?, "
                        "'priority_promotion', ?)",
                        (
                            f"scheduler-event-{uuid.uuid4().hex}",
                            run_key,
                            json.dumps(
                                {
                                    "old_band": band,
                                    "new_band": promoted,
                                    "waits": waits,
                                    "starvation_limit":
                                        STARVATION_PROMOTION_LIMIT,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
                else:
                    await connection.execute(
                        "UPDATE benchmark_runs SET starvation_wait = ? "
                        "WHERE id = ?",
                        (waits, run_key),
                    )
            ticket_cursor = await connection.execute(
                "UPDATE benchmark_runs SET dispatch_tickets = "
                "dispatch_tickets + 1, starvation_wait = 0 WHERE id = ? "
                "RETURNING dispatch_tickets",
                (selected_run,),
            )
            ticket_row = await ticket_cursor.fetchone()
            assert ticket_row is not None  # The selected run exists.
            ticket = int(ticket_row["dispatch_tickets"])
            lease_token = uuid.uuid4().hex
            cursor = await connection.execute(
                "UPDATE benchmark_attempts SET status = 'running', "
                f"claimed_at = {now_marker}, "
                f"started_at = COALESCE(started_at, {now_marker}), "
                "lease_owner = ?, lease_token = ?, "
                "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
                "lease_fence = lease_fence + 1 "
                "WHERE id = ? AND status = 'queued'",
                (worker_id, lease_token, lease_modifier, selected["id"]),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                return None
            # One immutable dispatch rank per eligibility generation.
            # A requeued attempt claims again under a new generation,
            # and the earlier rank rows stay durable.
            generation = int(selected["eligibility_generation"] or 1)
            arm_count = max(int(selected["arm_count"] or 1), 1)
            await connection.execute(
                "INSERT INTO benchmark_dispatch_ranks "
                "(id, run_id, attempt_id, eligibility_generation, "
                "priority_band, arm_position, ticket, tie_break_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_id, eligibility_generation) DO NOTHING",
                (
                    f"dispatch-rank-{uuid.uuid4().hex}",
                    selected_run,
                    str(selected["id"]),
                    generation,
                    str(selected["priority_band"] or "standard"),
                    int(selected["schedule_rank"] or 0) % arm_count,
                    ticket,
                    content_checksum(
                        [selected_run, str(selected["id"]), generation],
                    ),
                ),
            )
            await connection.execute(
                "UPDATE benchmark_trials SET status = 'running', "
                "started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "WHERE id = ?",
                (selected["trial_id"],),
            )
            await connection.execute(
                "UPDATE benchmark_runs SET status = 'running', "
                "started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "state_revision = state_revision + 1 "
                "WHERE id = ? AND status = 'queued'",
                (selected["run_id"],),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result = _attempt_record(selected)
    result.update({
        "lease_owner": worker_id,
        "lease_token": lease_token,
        "lease_fence": int(selected["lease_fence"] or 0) + 1,
    })
    return result


async def claim_expired_attempt(
    worker_id: str,
    *,
    lease_seconds: int = 30,
) -> dict[str, Any] | None:
    """Transfer one expired active attempt to a new fenced owner."""
    lease_modifier = f"+{max(5, min(lease_seconds, 300))} seconds"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                _ATTEMPT_CONTEXT_SELECT
                + "WHERE attempt.status = 'running' AND ("
                "attempt.lease_token IS NULL OR attempt.lease_expires_at IS NULL OR "
                "attempt.lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ORDER BY CASE WHEN attempt.task_id IS NULL THEN 0 ELSE 1 END, "
                "attempt.claimed_at, attempt.id LIMIT 1"
            )
            selected = await cursor.fetchone()
            if selected is None:
                await connection.rollback()
                return None
            lease_token = uuid.uuid4().hex
            update = await connection.execute(
                "UPDATE benchmark_attempts SET lease_owner = ?, lease_token = ?, "
                "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
                "claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "lease_fence = lease_fence + 1 "
                "WHERE id = ? AND status = 'running' AND ("
                "lease_token IS NULL OR lease_expires_at IS NULL OR "
                "lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (worker_id, lease_token, lease_modifier, selected["id"]),
            )
            if update.rowcount != 1:
                await connection.rollback()
                return None
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    result = _attempt_record(selected)
    result.update({
        "lease_owner": worker_id,
        "lease_token": lease_token,
        "lease_fence": int(selected["lease_fence"] or 0) + 1,
    })
    return result


async def renew_attempt_lease(
    attempt_id: str,
    lease_token: str,
    *,
    lease_seconds: int = 30,
) -> bool:
    """Extend one lease only when its fence token still matches."""
    modifier = f"+{max(5, min(lease_seconds, 300))} seconds"
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET "
            "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?) "
            "WHERE id = ? AND status = 'running' AND lease_token = ?",
            (modifier, attempt_id, lease_token),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def attach_attempt_task(
    attempt_id: str,
    task_id: str,
    execution_snapshot: dict[str, Any],
    snapshot_checksum: str,
    lease_token: str | None = None,
) -> None:
    """Attach the admitted task and its actual configuration snapshot."""
    async with db._connect() as connection:  # noqa: SLF001
        lease_clause = " AND lease_token = ?" if lease_token else ""
        parameters: list[Any] = [
            task_id,
            json.dumps(execution_snapshot, separators=(",", ":"), sort_keys=True),
            snapshot_checksum,
            attempt_id,
        ]
        if lease_token:
            parameters.append(lease_token)
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET task_id = ?, execution_snapshot = ?, "
            "snapshot_checksum = ? WHERE id = ? AND status = 'running' "
            f"AND task_id IS NULL{lease_clause}",
            parameters,
        )
        await connection.commit()
        if cursor.rowcount != 1:
            raise BenchmarkConflict("The attempt no longer accepts a task")


async def release_attempt(
    attempt_id: str,
    error: str | None = None,
    lease_token: str | None = None,
) -> bool:
    """Return a temporary admission failure to the durable queue."""
    async with db._connect() as connection:  # noqa: SLF001
        lease_clause = " AND lease_token = ?" if lease_token else ""
        parameters: list[Any] = [error[:1000] if error else None, attempt_id]
        if lease_token:
            parameters.append(lease_token)
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET status = 'queued', claimed_at = NULL, "
            "started_at = NULL, error_message = ?, lease_owner = NULL, "
            "lease_token = NULL, lease_expires_at = NULL, "
            "eligibility_generation = eligibility_generation + 1 "
            "WHERE id = ? AND status = 'running' AND task_id IS NULL"
            f"{lease_clause}",
            parameters,
        )
        await connection.commit()
        return cursor.rowcount == 1


async def fail_unadmitted_attempt(
    attempt_id: str,
    category: str,
    error: str,
    lease_token: str | None = None,
) -> bool:
    """Fail an attempt that cannot satisfy its runtime contract."""
    async with db._connect() as connection:  # noqa: SLF001
        lease_clause = " AND lease_token = ?" if lease_token else ""
        parameters: list[Any] = [category, error[:2000], attempt_id]
        if lease_token:
            parameters.append(lease_token)
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET status = 'failed', failure_category = ?, "
            "error_message = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_token = NULL, lease_expires_at = NULL "
            "WHERE id = ? AND task_id IS NULL"
            f"{lease_clause}",
            parameters,
        )
        await connection.commit()
    if cursor.rowcount == 1:
        await refresh_run_for_attempt(attempt_id)
        return True
    return False


async def fail_active_attempt(
    attempt_id: str,
    category: str,
    error: str,
    lease_token: str | None = None,
) -> bool:
    """Fail an admitted attempt and exclude it from deterministic scoring."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            lease_clause = " AND lease_token = ?" if lease_token else ""
            parameters: list[Any] = [category, error[:2000], attempt_id]
            if lease_token:
                parameters.append(lease_token)
            cursor = await connection.execute(
                "UPDATE benchmark_attempts SET status = 'failed', failure_category = ?, "
                "error_message = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "lease_token = NULL, lease_expires_at = NULL "
                "WHERE id = ? AND status = 'running'"
                f"{lease_clause}",
                parameters,
            )
            if cursor.rowcount == 1:
                scorers = await _attempt_scorers(connection, attempt_id)
                await connection.executemany(
                    "INSERT INTO benchmark_scores "
                    "(id, attempt_id, scorer_id, status, explanation, "
                    "evidence, configuration_checksum) "
                    "VALUES (?, ?, ?, 'excluded', ?, ?, ?) "
                    "ON CONFLICT(attempt_id, scorer_id) DO NOTHING",
                    [
                        (
                            f"score-{uuid.uuid4().hex}",
                            attempt_id,
                            scorer["id"],
                            error[:1000],
                            json.dumps({"failure_category": category}),
                            scorer.get("configuration_checksum"),
                        )
                        for scorer in scorers
                    ],
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    if cursor.rowcount == 1:
        await refresh_run_for_attempt(attempt_id)
        return True
    return False


async def active_attempts(worker_id: str | None = None) -> list[dict[str, Any]]:
    """Return active benchmark attempts with task and limit state."""
    async with db._connect() as connection:  # noqa: SLF001
        owner_clause = " AND attempt.lease_owner = ?" if worker_id else ""
        parameters = (worker_id,) if worker_id else ()
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
            f"{owner_clause}",
            parameters,
        )
    return [_record(row, "test_configuration", "execution_snapshot") for row in rows]


async def _attempt_scorers(
    connection: Any,
    attempt_id: str,
) -> list[dict[str, Any]]:
    rows = await connection.execute_fetchall(
        "SELECT scorer.*, link.configuration, link.configuration_checksum "
        "FROM benchmark_scorers AS scorer "
        "JOIN benchmark_test_revision_scorers AS link ON link.scorer_id = scorer.id "
        "JOIN benchmark_runs AS run ON run.test_revision_id = link.test_revision_id "
        "JOIN benchmark_trials AS trial ON trial.run_id = run.id "
        "JOIN benchmark_attempts AS attempt ON attempt.trial_id = trial.id "
        "WHERE attempt.id = ? ORDER BY link.sort_order, scorer.id",
        (attempt_id,),
    )
    return [_record(row, "configuration", "configuration_schema") for row in rows]


async def finish_attempt_from_task(
    attempt_id: str,
    lease_token: str | None = None,
) -> bool:
    """Persist terminal task state and all versioned scorer results."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            lease_clause = " AND attempt.lease_token = ?" if lease_token else ""
            parameters: list[Any] = [attempt_id]
            if lease_token:
                parameters.append(lease_token)
            cursor = await connection.execute(
                "SELECT attempt.*, task.status AS task_status, task.terminal_kind, "
                "task.result_summary, task.error_message AS task_error, "
                "item.expected_output FROM benchmark_attempts AS attempt "
                "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
                "JOIN dataset_items AS item ON item.id = trial.dataset_item_id "
                "LEFT JOIN tasks AS task ON task.id = attempt.task_id "
                "WHERE attempt.id = ? AND attempt.status = 'running'"
                f"{lease_clause}",
                parameters,
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
                "error_message = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "lease_token = NULL, lease_expires_at = NULL "
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
                    "extracted_output, explanation, evidence, "
                    "configuration_checksum) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
                        scorer.get("configuration_checksum"),
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
    error_cursor = await connection.execute(
        "SELECT EXISTS(SELECT 1 FROM benchmark_scores AS score "
        "JOIN benchmark_attempts AS attempt ON attempt.id = score.attempt_id "
        "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
        "JOIN benchmark_test_revision_scorers AS link "
        "ON link.test_revision_id = "
        "(SELECT test_revision_id FROM benchmark_runs WHERE id = ?) "
        "AND link.scorer_id = score.scorer_id "
        "WHERE trial.run_id = ? AND link.required = 1 "
        "AND score.status = 'error' "
        "AND attempt.retry_index = (SELECT MAX(candidate.retry_index) "
        "FROM benchmark_attempts AS candidate "
        "WHERE candidate.trial_id = attempt.trial_id "
        "AND candidate.repeat_index = attempt.repeat_index)) AS has_error",
        (run_id, run_id),
    )
    error_row = await error_cursor.fetchone()
    has_required_error = bool(error_row["has_error"] if error_row else 0)
    terminal_next = next_status in _TERMINAL_RUN_STATUSES
    if has_required_error:
        scoring_status = "failed"
    elif terminal_next:
        scoring_status = "completed"
    elif any(status == "running" for status in statuses):
        scoring_status = "running"
    else:
        scoring_status = "pending"
    if scoring_status == "failed":
        analysis_status = "blocked"
    elif terminal_next and scoring_status == "completed":
        analysis_status = "valid"
    else:
        analysis_status = "pending"
    await connection.execute(
        "UPDATE benchmark_runs SET status = ?, completed_attempts = ?, "
        "scoring_status = ?, analysis_status = ?, "
        "completed_trials = (SELECT COUNT(*) FROM benchmark_trials "
        "WHERE run_id = ? AND status IN ('completed','failed','cancelled','excluded')), "
        "completed_at = CASE WHEN ? THEN "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END, "
        "state_revision = state_revision + 1 WHERE id = ?",
        (
            next_status,
            complete_count,
            scoring_status,
            analysis_status,
            run_id,
            int(bool(completed_at)),
            run_id,
        ),
    )
    # A terminal run ends its cost-bearing work: the run cost moves
    # from provisional to settling. Settlement itself waits for every
    # reservation reconciliation and unknown-charge decision.
    if terminal_next:
        await connection.execute(
            "UPDATE benchmark_runs SET cost_status = 'settling' "
            "WHERE id = ? AND cost_status = 'provisional'",
            (run_id,),
        )


async def recover_orphan_attempts() -> int:
    """Return only expired unadmitted legacy attempts to the queue."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET status = 'queued', claimed_at = NULL, "
            "started_at = NULL, lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL WHERE status = 'running' AND task_id IS NULL "
            "AND lease_token IS NULL"
        )
        await connection.commit()
        return cursor.rowcount


async def register_scheduler_worker(
    worker_id: str,
    hostname: str,
    process_id: int,
) -> None:
    """Register one scheduler replica and refresh an existing identity."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO benchmark_scheduler_workers "
            "(worker_id, hostname, process_id) VALUES (?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET hostname = excluded.hostname, "
            "process_id = excluded.process_id, status = 'active', "
            "last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), stopped_at = NULL",
            (worker_id, hostname, process_id),
        )
        await connection.execute(
            "DELETE FROM benchmark_scheduler_workers "
            "WHERE worker_id != ? "
            "AND last_seen_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days') "
            "AND NOT EXISTS (SELECT 1 FROM benchmark_attempts AS attempt "
            "WHERE attempt.status = 'running' "
            "AND attempt.lease_owner = benchmark_scheduler_workers.worker_id)",
            (worker_id,),
        )
        await connection.commit()


async def heartbeat_scheduler_worker(worker_id: str) -> bool:
    """Refresh one registered scheduler heartbeat."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_scheduler_workers SET "
            "last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE worker_id = ? AND status = 'active'",
            (worker_id,),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def stop_scheduler_worker(worker_id: str) -> None:
    """Mark one scheduler as stopped without releasing active leases."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_scheduler_workers SET status = 'stopped', "
            "last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "stopped_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE worker_id = ?",
            (worker_id,),
        )
        await connection.commit()


async def benchmark_capacity_snapshot(
    capacity_policy: CapacityPolicy,
    *,
    stale_after_seconds: int = 90,
) -> dict[str, Any]:
    """Return queue pressure, resource use, and shared scheduler ownership."""
    async with db._connect() as connection:  # noqa: SLF001
        active_rows = await connection.execute_fetchall(
            _ATTEMPT_CONTEXT_SELECT + "WHERE attempt.status = 'running'"
        )
        queued_rows = await connection.execute_fetchall(
            "SELECT run.priority, COUNT(*) AS count "
            "FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "JOIN benchmark_runs AS run ON run.id = trial.run_id "
            "WHERE attempt.status = 'queued' AND run.status IN ('queued','running') "
            "GROUP BY run.priority ORDER BY run.priority DESC"
        )
        worker_rows = await connection.execute_fetchall(
            "SELECT worker.*, CASE WHEN worker.status = 'active' AND "
            "worker.last_seen_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now', ?) "
            "THEN 0 ELSE 1 END AS stale, "
            "(SELECT COUNT(*) FROM benchmark_attempts AS attempt "
            "WHERE attempt.status = 'running' AND attempt.lease_owner = worker.worker_id) "
            "AS owned_attempts FROM benchmark_scheduler_workers AS worker "
            "ORDER BY worker.last_seen_at DESC LIMIT 100",
            (f"-{max(10, stale_after_seconds)} seconds",),
        )
    active = [_attempt_record(row) for row in active_rows]
    use: dict[str, int] = {}
    for attempt in active:
        for claim in capacity_policy.claims(attempt):
            use[claim] = use.get(claim, 0) + 1
    limits = capacity_policy.limits()
    return {
        "schema_version": "1",
        "global": {
            "active": len(active),
            "limit": capacity_policy.global_limit,
            "available": max(0, capacity_policy.global_limit - len(active)),
        },
        "resources": [
            {
                "key": key,
                "active": use.get(key, 0),
                "limit": limit,
                "available": max(0, limit - use.get(key, 0)),
            }
            for key, limit in sorted(limits.items())
        ],
        "unlimited_active_resources": [
            {"key": key, "active": count}
            for key, count in sorted(use.items())
            if key not in limits
        ],
        "queue": {
            "total": sum(int(row["count"]) for row in queued_rows),
            "by_priority": [dict(row) for row in queued_rows],
        },
        "workers": [dict(row) for row in worker_rows],
        "attempts": [
            {
                "id": attempt["id"],
                "run_id": attempt["run_id"],
                "runtime_id": attempt["runtime_id"],
                "lease_owner": attempt.get("lease_owner"),
                "lease_fence": attempt.get("lease_fence"),
                "lease_expires_at": attempt.get("lease_expires_at"),
                "task_id": attempt.get("task_id"),
            }
            for attempt in active
        ],
    }


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
                        "(id, attempt_id, scorer_id, status, explanation, "
                        "evidence, configuration_checksum) "
                        "VALUES (?, ?, ?, 'excluded', "
                        "'Run cancelled before admission', ?, ?) "
                        "ON CONFLICT(attempt_id, scorer_id) DO NOTHING",
                        [
                            (
                                f"score-{uuid.uuid4().hex}",
                                queued["id"],
                                scorer["id"],
                                json.dumps({"failure_category": "cancelled"}),
                                scorer.get("configuration_checksum"),
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
                    "seed_control, schedule_rank, "
                    "execution_snapshot, snapshot_checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        row["trial_id"],
                        int(number_row["number"]),
                        row["repeat_index"],
                        retry_index,
                        row["random_seed"],
                        row["seed_control"],
                        row["schedule_rank"],
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


# ── Scheduler evidence, admission authority, and run cost ────────────


async def run_dispatch_records(run_id: str) -> dict[str, Any]:
    """Return the immutable dispatch ranks and scheduler events."""
    async with db._connect() as connection:  # noqa: SLF001
        ranks = await connection.execute_fetchall(
            "SELECT * FROM benchmark_dispatch_ranks WHERE run_id = ? "
            "ORDER BY ticket, created_at, id",
            (run_id,),
        )
        events = await connection.execute_fetchall(
            "SELECT * FROM benchmark_scheduler_events WHERE run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        )
    return {
        "ranks": [dict(row) for row in ranks],
        "events": [_record(row, "payload") for row in events],
    }


async def record_scheduler_event(
    run_id: str, event_type: str, payload: dict[str, Any],
) -> None:
    """Save one durable scheduler event for one run."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO benchmark_scheduler_events "
            "(id, run_id, event_type, payload) VALUES (?, ?, ?, ?)",
            (
                f"scheduler-event-{uuid.uuid4().hex}",
                run_id,
                event_type,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        await connection.commit()


async def set_run_authority(
    run_id: str,
    *,
    authority_run_id: str,
    authority_fence: str,
    budget_id: str,
) -> None:
    """Attach the Foundation admission authority exactly once."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_runs SET authority_run_id = ?, "
            "authority_fence = ?, budget_id = ? "
            "WHERE id = ? AND authority_run_id IS NULL",
            (authority_run_id, authority_fence, budget_id, run_id),
        )
        await connection.commit()
        if cursor.rowcount != 1:
            existing = await connection.execute(
                "SELECT authority_run_id, budget_id FROM benchmark_runs "
                "WHERE id = ?",
                (run_id,),
            )
            row = await existing.fetchone()
            if row is None:
                raise BenchmarkNotFound("Benchmark run not found")
            if (
                str(row["authority_run_id"]) != authority_run_id
                or str(row["budget_id"]) != budget_id
            ):
                raise BenchmarkConflict(
                    "The run already carries a different admission authority"
                )


async def record_attempt_admission(
    attempt_id: str,
    *,
    effect_id: str,
    reservation_id: str,
    lease_token: str | None = None,
) -> bool:
    """Link one attempt to its admission effect and reservation."""
    async with db._connect() as connection:  # noqa: SLF001
        lease_clause = " AND lease_token = ?" if lease_token else ""
        parameters: list[Any] = [effect_id, reservation_id, attempt_id]
        if lease_token:
            parameters.append(lease_token)
        cursor = await connection.execute(
            "UPDATE benchmark_attempts SET admission_effect_id = ?, "
            "admission_reservation_id = ? "
            "WHERE id = ? AND status = 'running'"
            f"{lease_clause}",
            parameters,
        )
        await connection.commit()
        return cursor.rowcount == 1


async def record_cost_charge(
    run_id: str,
    *,
    kind: str,
    currency: str,
    amount_nanos: int | None,
    attempt_id: str | None = None,
    provider: str = "",
    source_text: str | None = None,
    source_kind: str = "decimal_string",
    evidence: dict[str, Any] | None = None,
) -> str:
    """Save one charge, estimate, unknown, or not-billable record.

    An unknown price stores a null amount and stays visible; it never
    becomes zero. A not-billable event stores zero with its evidence.
    """
    if kind == "unknown" and amount_nanos is not None:
        raise BenchmarkConflict("An unknown charge stores no amount")
    if kind not in {"unknown", "not_billable"} and amount_nanos is None:
        raise BenchmarkConflict(f"A {kind} record needs an exact amount")
    charge_id = f"cost-charge-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO benchmark_cost_charges "
            "(id, run_id, attempt_id, provider, kind, currency, "
            "amount_nanos, source_text, source_kind, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                charge_id,
                run_id,
                attempt_id,
                provider,
                kind,
                currency,
                amount_nanos,
                source_text,
                source_kind,
                json.dumps(
                    evidence or {}, separators=(",", ":"), sort_keys=True,
                ),
            ),
        )
        await connection.commit()
    return charge_id


async def list_cost_charges(run_id: str) -> list[dict[str, Any]]:
    """Return every recorded cost charge for one run."""
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM benchmark_cost_charges WHERE run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        )
    return [_record(row, "evidence") for row in rows]


async def accept_unknown_charge(
    charge_id: str,
    *,
    operator_id: str,
    bound: dict[str, Any] | None,
) -> None:
    """Record one operator acceptance for one unknown charge.

    The acceptance can carry one conservative upper bound. The amount
    stays null: the unknown price never becomes zero.
    """
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM benchmark_cost_charges WHERE id = ?",
            (charge_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise BenchmarkNotFound("The cost charge does not exist")
        if str(row["kind"]) != "unknown":
            raise BenchmarkConflict("Only an unknown charge takes acceptance")
        evidence = _json(row["evidence"], {})
        evidence["accepted_by"] = operator_id
        if bound is not None:
            evidence["accepted_bound"] = bound
        await connection.execute(
            "UPDATE benchmark_cost_charges SET evidence = ? WHERE id = ?",
            (
                json.dumps(evidence, separators=(",", ":"), sort_keys=True),
                charge_id,
            ),
        )
        await connection.commit()


async def set_run_cost_status(
    run_id: str,
    target: str,
    *,
    settled_cost: dict[str, Any] | None = None,
    cost_bound: dict[str, Any] | None = None,
) -> None:
    """Move one run through the declared cost state machine."""
    from benchmarks.costs import validate_cost_transition

    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT cost_status FROM benchmark_runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise BenchmarkNotFound("Benchmark run not found")
        current = str(row["cost_status"] or "provisional")
        if current == target:
            return
        validate_cost_transition(current, target)
        await connection.execute(
            "UPDATE benchmark_runs SET cost_status = ?, "
            "settled_cost = ?, cost_bound = ?, "
            "cost_settled_at = CASE WHEN ? = 'settled' THEN "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END, "
            "state_revision = state_revision + 1 WHERE id = ?",
            (
                target,
                json.dumps(settled_cost, separators=(",", ":"),
                           sort_keys=True) if settled_cost else None,
                json.dumps(cost_bound, separators=(",", ":"),
                           sort_keys=True) if cost_bound else None,
                target,
                run_id,
            ),
        )
        await connection.commit()


async def supersede_gate_evaluations(
    run_id: str, *, superseded_by: str,
) -> int:
    """Mark every stored gate for one candidate run as superseded.

    The superseded evaluations stay readable; a later evaluation
    creates the next evaluation version.
    """
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE benchmark_gate_evaluations SET "
            "superseded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "superseded_by = ? "
            "WHERE candidate_run_id = ? AND superseded_at IS NULL",
            (superseded_by, run_id),
        )
        await connection.commit()
        return cursor.rowcount


async def record_late_charge(
    run_id: str,
    *,
    currency: str,
    source_text: str,
    provider: str = "",
    evidence: dict[str, Any] | None = None,
) -> str:
    """Record one late provider charge after settlement.

    The late charge parses at the trusted boundary, keeps the original
    provider string with its evidence, reopens settlement, and
    supersedes every stored gate for the run.
    """
    from core.money import Money

    money = Money.from_decimal_string(currency, source_text)
    charge_id = await record_cost_charge(
        run_id,
        kind="late_charge",
        currency=currency,
        amount_nanos=money.amount_nanos,
        provider=provider,
        source_text=source_text,
        source_kind="decimal_string",
        evidence=evidence,
    )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT cost_status FROM benchmark_runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise BenchmarkNotFound("Benchmark run not found")
    if str(row["cost_status"] or "provisional") == "settled":
        await set_run_cost_status(run_id, "settling")
    await supersede_gate_evaluations(run_id, superseded_by=charge_id)
    return charge_id
