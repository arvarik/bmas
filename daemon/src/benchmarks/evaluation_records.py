"""The one canonical write authority for evaluation contract storage.

Every write into the evaluation record tables passes through this
module. Each stored record validates against its documented contract
first, persists as canonical JSON with its checksum and required
links, and then changes only through the declared lifecycle
transitions that the database triggers also enforce. No other module
writes these tables.

The module also owns the supported predestructive downgrade: it
archives every expansion-only record into the read-only archive, removes the
expansion tables, and preserves every legacy-compatible record unchanged.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import database as db
import migration_negotiation as negotiation
from benchmarks.evaluation_contracts import (
    EvaluationContractError,
    canonical_record_json,
    validate_record,
)

# The storable record kinds. A contract without its own table in this
# expansion (for example the dataset-version and score-record models)
# validates through the contracts module and persists in a later
# phase.
_STORABLE_KINDS: dict[str, dict[str, Any]] = {
    "benchmark-source": {
        "insert_sql": (
            "INSERT INTO benchmark_sources (id, schema_version, record, record_checksum) VALUES (?, ?, ?, ?)"
        ),
        "table": "benchmark_sources",
        "id_field": "source_id",
        "links": (),
    },
    "dataset-draft": {
        "insert_sql": (
            "INSERT INTO dataset_drafts (id, schema_version, record, record_checksum, source_id, parent_version_id) VALUES (?, ?, ?, ?, ?, ?)"
        ),
        "table": "dataset_drafts",
        "id_field": "draft_id",
        "links": ("source_id", "parent_version_id"),
    },
    "evaluation-case": {
        "insert_sql": (
            "INSERT INTO dataset_draft_cases (id, schema_version, record, record_checksum, case_id, draft_id) VALUES (?, ?, ?, ?, ?, ?)"
        ),
        "table": "dataset_draft_cases",
        "id_field": "case_id",
        "links": ("draft_id",),
        "required_links": ("draft_id",),
        "column_values": {"case_id": ("case_id",)},
        # One case identifier can repeat across drafts, so the row
        # identity combines the draft link and the case identifier.
        "identity_link_prefix": "draft_id",
    },
    "scorer-spec": {
        "insert_sql": (
            "INSERT INTO scorer_versions (id, schema_version, record, record_checksum, legacy_scorer_id) VALUES (?, ?, ?, ?, ?)"
        ),
        "table": "scorer_versions",
        "id_field": "scorer_id",
        "links": ("legacy_scorer_id",),
        # One scorer identifier carries many immutable versions.
        "identity_field_suffix": "version",
    },
    "run-plan": {
        "insert_sql": (
            "INSERT INTO run_plans (id, schema_version, record, record_checksum, test_revision_id, run_id) VALUES (?, ?, ?, ?, ?, ?)"
        ),
        "table": "run_plans",
        "id_field": "plan_id",
        "links": ("test_revision_id", "run_id"),
    },
    "attempt-evidence": {
        "insert_sql": (
            "INSERT INTO attempt_evidence_bundles (id, schema_version, record, record_checksum, attempt_id) VALUES (?, ?, ?, ?, ?)"
        ),
        "table": "attempt_evidence_bundles",
        "id_field": "attempt_id",
        "links": ("attempt_id",),
        "required_links": ("attempt_id",),
        "match_fields": {"attempt_id": "attempt_id"},
    },
    "analysis-snapshot": {
        "insert_sql": (
            "INSERT INTO analysis_snapshots (id, schema_version, record, record_checksum, run_id) VALUES (?, ?, ?, ?, ?)"
        ),
        "table": "analysis_snapshots",
        "id_field": "snapshot_id",
        "links": ("run_id",),
        "required_links": ("run_id",),
    },
    "interaction-spec": {
        "insert_sql": (
            "INSERT INTO interaction_specs (id, schema_version, record, record_checksum) VALUES (?, ?, ?, ?)"
        ),
        "table": "interaction_specs",
        "id_field": "spec_id",
        "links": (),
    },
    "contamination-rights-record": {
        "insert_sql": (
            "INSERT INTO contamination_rights_records (id, schema_version, record, record_checksum, dataset_version_id) VALUES (?, ?, ?, ?, ?)"
        ),
        "table": "contamination_rights_records",
        "id_field": "record_id",
        "links": ("dataset_version_id",),
        "required_links": ("dataset_version_id",),
        "match_fields": {"dataset_version_id": "dataset_version_id"},
    },
    "metric-definition": {
        "insert_sql": (
            "INSERT INTO metric_definitions (id, schema_version, record, record_checksum, lifecycle_state, calibration_state) VALUES (?, ?, ?, ?, ?, ?)"
        ),
        "table": "metric_definitions",
        "id_field": "metric_id",
        "links": (),
        "column_values": {
            "lifecycle_state": ("lifecycle_state",),
            "calibration_state": ("calibration", "state"),
        },
    },
    "asset-ingestion-record": {
        "insert_sql": (
            "INSERT INTO asset_ingestion_records (id, schema_version, record, record_checksum, state) VALUES (?, ?, ?, ?, ?)"
        ),
        "table": "asset_ingestion_records",
        "id_field": "ingestion_id",
        "links": (),
        "column_values": {"state": ("state",)},
    },
}

_PUBLISH_SQL = {
    "dataset-draft": (
        "UPDATE dataset_drafts SET status = 'published', published_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND status = 'editing'"
    ),
    "scorer-spec": (
        "UPDATE scorer_versions SET status = 'published', published_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND status = 'draft'"
    ),
    "run-plan": (
        "UPDATE run_plans SET status = 'published', published_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND status = 'draft'"
    ),
    "interaction-spec": (
        "UPDATE interaction_specs SET status = 'published', "
        "published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND status = 'draft'"
    ),
}

# Every expansion table, in child-before-parent order for the
# downgrade. The read-only archive is the preservation vehicle and
# never removes.
# The schema version before the evaluation expansion began. The
# downgrade returns to it and preserves every record.
EXPANSION_BASE_VERSION = 21

EXPANSION_TABLES = (
    ("evaluation_case_assets", "case-asset-link"),
    ("dataset_draft_cases", "evaluation-case"),
    ("dataset_transform_recipes", "transform-recipe"),
    ("dataset_drafts", "dataset-draft"),
    ("benchmark_sources", "benchmark-source"),
    ("scorer_versions", "scorer-spec"),
    ("run_plans", "run-plan"),
    ("attempt_evidence_bundles", "attempt-evidence"),
    ("analysis_snapshots", "analysis-snapshot"),
    ("gate_display_exceptions", "gate-display-exception"),
    ("interaction_specs", "interaction-spec"),
    ("contamination_rights_records", "contamination-rights-record"),
    ("metric_definitions", "metric-definition"),
    ("cost_settlement_versions", "cost-settlement-version"),
    ("dispatch_rank_history", "dispatch-rank"),
    ("asset_ingestion_records", "asset-ingestion-record"),
    ("evaluation_migration_events", "migration-event"),
    ("evaluation_migration_state", "migration-state"),
)


class EvaluationStorageError(RuntimeError):
    """A storage operation violates the evaluation write contract."""


async def save_record(
    record: dict[str, Any],
    *,
    record_id: str | None = None,
    links: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Validate one record and persist it exactly once.

    The record validates against its documented contract before any
    write. The stored row keeps the canonical JSON, the record
    checksum, and the declared foreign-key links, and the database
    enforces those links.
    """
    summary = validate_record(record)
    kind = summary["schema_id"]
    configuration = _STORABLE_KINDS.get(kind)
    if configuration is None:
        raise EvaluationStorageError(
            f"The {kind} record validates through its contract; this "
            "expansion stores no table for it yet"
        )
    provided = dict(links or {})
    unknown_links = set(provided) - set(configuration["links"])
    if unknown_links:
        raise EvaluationStorageError(
            f"Unknown links for {kind}: {sorted(unknown_links)}"
        )
    for required in configuration.get("required_links", ()):
        if not provided.get(required):
            raise EvaluationStorageError(
                f"The {kind} record requires the {required} link"
            )
    for link_name, field in (
        configuration.get("match_fields") or {}
    ).items():
        if str(provided.get(link_name)) != str(record.get(field)):
            raise EvaluationStorageError(
                f"The {kind} record field {field} must equal its "
                f"{link_name} link"
            )
    identity = record_id
    if identity is None:
        identity = str(record[configuration["id_field"]])
        suffix_field = configuration.get("identity_field_suffix")
        if suffix_field:
            identity = f"{identity}:{record[suffix_field]}"
        prefix_link = configuration.get("identity_link_prefix")
        if prefix_link:
            identity = f"{provided[prefix_link]}:{identity}"
    values: list[Any] = [
        identity,
        summary["schema_version"],
        canonical_record_json(record),
        summary["record_checksum"],
    ]
    for path in (configuration.get("column_values") or {}).values():
        value: Any = record
        for step in path:
            value = value[step]
        values.append(str(value))
    for link_name in configuration["links"]:
        values.append(provided.get(link_name))
    async with db._connect() as connection:  # noqa: SLF001
        # One literal statement per kind keeps every write path
        # visible to the durable authority scan.
        await connection.execute(configuration["insert_sql"], values)
        await connection.commit()
    return {
        "id": identity,
        "table": configuration["table"],
        **summary,
    }


async def get_record(kind: str, record_id: str) -> dict[str, Any] | None:
    """Read one stored evaluation record with its decoded content."""
    configuration = _STORABLE_KINDS.get(kind)
    if configuration is None:
        raise EvaluationStorageError(f"Unknown record kind: {kind}")
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            f"SELECT * FROM {configuration['table']} WHERE id = ?",
            (record_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    result = dict(row)
    result["record"] = json.loads(result["record"])
    return result


async def publish_record(kind: str, record_id: str) -> None:
    """Move one draft record to its immutable published state."""
    statement = _PUBLISH_SQL.get(kind)
    if statement is None:
        raise EvaluationStorageError(
            f"The {kind} record has no publication lifecycle"
        )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(statement, (record_id,))
        await connection.commit()
        if cursor.rowcount != 1:
            raise EvaluationStorageError(
                f"The {kind} record {record_id} is not an editable draft"
            )


async def transition_asset_state(record_id: str, state: str) -> None:
    """Apply one declared ingestion state transition."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE asset_ingestion_records SET state = ?, "
            "state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (state, record_id),
        )
        await connection.commit()
        if cursor.rowcount != 1:
            raise EvaluationStorageError(
                f"The ingestion record {record_id} does not exist"
            )


async def transition_metric_lifecycle(
    record_id: str, record: dict[str, Any],
) -> None:
    """Apply one declared metric lifecycle transition.

    The updated record revalidates against its contract, and the
    database triggers enforce that a published definition moves only
    to deprecated or withdrawn, while a deprecated or withdrawn
    definition stays readable and unchanged.
    """
    summary = validate_record(record)
    if summary["schema_id"] != "metric-definition":
        raise EvaluationStorageError(
            "Only a metric-definition record transitions here"
        )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE metric_definitions SET lifecycle_state = ?, "
            "calibration_state = ?, record = ?, record_checksum = ?, "
            "published_at = CASE WHEN ? = 'published' THEN "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE published_at END "
            "WHERE id = ?",
            (
                str(record["lifecycle_state"]),
                str(record["calibration"]["state"]),
                canonical_record_json(record),
                summary["record_checksum"],
                str(record["lifecycle_state"]),
                record_id,
            ),
        )
        await connection.commit()
        if cursor.rowcount != 1:
            raise EvaluationStorageError(
                f"The metric definition {record_id} does not exist"
            )


async def save_gate_display_exception(
    gate_evaluation_id: str,
    exception: dict[str, Any],
    *,
    exception_id: str | None = None,
) -> str:
    """Save one immutable gate display exception row.

    A caller with a deterministic identity, such as the idempotent
    backfill, passes its own identifier.
    """
    for field in ("author", "scope", "expires_at", "reason"):
        if not exception.get(field):
            raise EvaluationContractError(
                f"A display exception requires {field}"
            )
    exception_id = exception_id or f"display-exception-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO gate_display_exceptions "
            "(id, gate_evaluation_id, author, scope, expires_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                exception_id,
                gate_evaluation_id,
                str(exception["author"]),
                str(exception["scope"]),
                str(exception["expires_at"]),
                str(exception["reason"]),
            ),
        )
        await connection.commit()
    return exception_id


async def link_case_asset(
    draft_id: str, case_id: str, ingestion_id: str,
) -> str:
    """Link one accepted asset to one draft case."""
    link_id = f"case-asset-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO evaluation_case_assets "
            "(id, draft_id, case_id, ingestion_id) VALUES (?, ?, ?, ?)",
            (link_id, draft_id, case_id, ingestion_id),
        )
        await connection.commit()
    return link_id


async def save_transform_recipe(
    draft_id: str, position: int, recipe: dict[str, Any],
) -> str:
    """Save one ordered transformation recipe step for one draft."""
    if not isinstance(recipe, dict) or not recipe.get("operation"):
        raise EvaluationContractError(
            "A transformation recipe step names its operation"
        )
    from benchmarks.provenance import content_checksum

    recipe_id = f"transform-recipe-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO dataset_transform_recipes "
            "(id, draft_id, position, schema_version, record, "
            "record_checksum) VALUES (?, ?, ?, ?, ?, ?)",
            (
                recipe_id,
                draft_id,
                int(position),
                2,
                json.dumps(recipe, separators=(",", ":"), sort_keys=True),
                content_checksum(recipe),
            ),
        )
        await connection.commit()
    return recipe_id


async def save_cost_settlement_version(
    run_id: str, settlement_version: int, record: dict[str, Any],
) -> str:
    """Save one immutable cost settlement version for one run."""
    from benchmarks.provenance import content_checksum

    record_id = f"cost-settlement-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO cost_settlement_versions "
            "(id, run_id, settlement_version, schema_version, record, "
            "record_checksum) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record_id,
                run_id,
                int(settlement_version),
                2,
                json.dumps(record, separators=(",", ":"), sort_keys=True),
                content_checksum(record),
            ),
        )
        await connection.commit()
    return record_id


async def save_dispatch_rank_history(
    attempt_id: str,
    eligibility_generation: int,
    record: dict[str, Any],
) -> str:
    """Save one immutable dispatch rank history row."""
    from benchmarks.provenance import content_checksum

    record_id = f"dispatch-history-{uuid.uuid4().hex}"
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO dispatch_rank_history "
            "(id, attempt_id, eligibility_generation, schema_version, "
            "record, record_checksum) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record_id,
                attempt_id,
                int(eligibility_generation),
                2,
                json.dumps(record, separators=(",", ":"), sort_keys=True),
                content_checksum(record),
            ),
        )
        await connection.commit()
    return record_id


# ── The supported predestructive downgrade ───────────────────────────


async def downgrade_evaluation_expansion() -> dict[str, Any]:
    """Remove the expansion while preserving every record.

    The downgrade evaluates through the Foundation negotiation gate
    first: it runs before any destructive migration, and it refuses
    unless every new write stays preserved. It then archives every
    expansion-only row into the read-only archive, removes the expansion
    tables in child-before-parent order, and lowers the recorded
    schema version. Every legacy-compatible record stays byte-identical,
    and a later upgrade recreates the empty expansion beside the
    preserved archive.
    """
    async with db._connect() as connection:  # noqa: SLF001
        counts: dict[str, int] = {}
        for table, _kind in EXPANSION_TABLES:
            cursor = await connection.execute(
                f"SELECT COUNT(*) AS rows_present FROM {table}",
            )
            row = await cursor.fetchone()
            counts[table] = int(row["rows_present"]) if row else 0
    new_writes_present = any(counts.values())
    negotiation.evaluate_downgrade(
        negotiation.DowngradePlan(
            from_schema_version=db.SCHEMA_VERSION,
            to_schema_version=EXPANSION_BASE_VERSION,
            new_writes_present=new_writes_present,
            # The archive preserves every expansion-only record as an
            # explicit read-only record.
            new_writes_are_reversible=True,
        ),
    )
    archived = 0
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            for table, kind in EXPANSION_TABLES:
                rows = await connection.execute_fetchall(
                    f"SELECT * FROM {table}",
                )
                for row in rows:
                    payload = dict(row)
                    await connection.execute(
                        "INSERT INTO evaluation_readonly_archive "
                        "(id, record_kind, source_table, record) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            f"{table}:{payload['id']}",
                            kind,
                            table,
                            json.dumps(
                                payload,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
                    archived += 1
                await connection.execute(f"DROP TABLE {table}")
            await connection.execute(
                "DELETE FROM schema_version WHERE version > ?",
                (EXPANSION_BASE_VERSION,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return {
        "archived_records": archived,
        "removed_tables": [table for table, _kind in EXPANSION_TABLES],
        "schema_version": EXPANSION_BASE_VERSION,
    }


async def publish_draft_with_projection(
    draft_id: str,
    *,
    dataset_id: str,
    version_id: str,
    name: str,
    description: str = "",
) -> dict[str, Any]:
    """Publish one draft and its legacy projection in one transaction.

    One unit of work freezes the draft, creates the compatible legacy
    dataset version with every projected case, and records the content
    digest. The projection is a compatibility view of the one
    canonical publication, never a second authority, and a crash
    leaves either both records or neither.
    """
    from benchmarks.legacy_adapters import legacy_item_from_case
    from benchmarks.provenance import content_checksum

    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            draft_cursor = await connection.execute(
                "SELECT * FROM dataset_drafts WHERE id = ?", (draft_id,),
            )
            draft_row = await draft_cursor.fetchone()
            if draft_row is None:
                raise EvaluationStorageError(
                    f"The draft {draft_id} does not exist"
                )
            if str(draft_row["status"]) != "editing":
                raise EvaluationStorageError(
                    f"The draft {draft_id} is not an editable draft"
                )
            case_rows = await connection.execute_fetchall(
                "SELECT * FROM dataset_draft_cases WHERE draft_id = ? "
                "ORDER BY case_id",
                (draft_id,),
            )
            if not case_rows:
                raise EvaluationStorageError(
                    f"The draft {draft_id} publishes at least one case"
                )
            cases = [json.loads(row["record"]) for row in case_rows]
            items = []
            for index, case in enumerate(cases):
                projected = legacy_item_from_case(case)
                items.append({
                    **projected,
                    "id": f"{version_id}:{projected['item_key']}",
                    "sort_order": index,
                })
            content_digest = content_checksum(cases)
            await connection.execute(
                "INSERT INTO datasets (id, name, description, metadata) "
                "VALUES (?, ?, ?, '{}') "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                (dataset_id, name, description),
            )
            version_cursor = await connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM dataset_versions WHERE dataset_id = ?",
                (dataset_id,),
            )
            version_row = await version_cursor.fetchone()
            assert version_row is not None  # An aggregate returns one row.
            await connection.execute(
                "INSERT INTO dataset_versions "
                "(id, dataset_id, version, status, checksum, item_count, "
                "schema_json, source_filename, source_mime, "
                "source_checksum, source_path, metadata) "
                "VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    dataset_id,
                    int(version_row["next_version"]),
                    content_digest,
                    len(items),
                    json.dumps({"version": "2", "source_format": "draft"},
                               sort_keys=True),
                    f"{draft_id}.draft",
                    "application/x-bmas-draft",
                    str(draft_row["record_checksum"]),
                    f"draft://{draft_id}",
                    json.dumps({"published_from_draft": draft_id},
                               sort_keys=True),
                ),
            )
            await connection.executemany(
                "INSERT INTO dataset_items "
                "(id, dataset_version_id, item_key, input, "
                "expected_output, subject, split, tags, metadata, "
                "sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["id"],
                        version_id,
                        item["item_key"],
                        item["input"],
                        item["expected_output"],
                        item["subject"],
                        item["split"],
                        json.dumps(item["tags"], sort_keys=True),
                        json.dumps(item["metadata"], sort_keys=True),
                        item["sort_order"],
                    )
                    for item in items
                ],
            )
            # Publication happens after the items land, while the
            # version row is still a draft, so the immutability
            # trigger sees one legal draft-to-published change.
            await connection.execute(
                "UPDATE dataset_versions SET status = 'published', "
                "published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND status = 'draft'",
                (version_id,),
            )
            await connection.execute(
                "UPDATE dataset_drafts SET status = 'published', "
                "published_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND status = 'editing'",
                (draft_id,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return {
        "draft_id": draft_id,
        "dataset_id": dataset_id,
        "version_id": version_id,
        "content_digest": content_digest,
        "item_count": len(items),
    }
