"""Foundation Stage 0A: the durable-authority map matches reality.

The map at ``conformance/durable_authority/authority-map.yaml`` records
every current durable write path. These tests compare the map with the
live migrated schema and with a scan of every SQL write statement in
the daemon source, so the map can hold no unknown writer, no unknown
table, and no unowned field.
"""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite
import pytest
import yaml

import database
from config_schema import StorageConfig
from core import protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "conformance" / "durable_authority" / "authority-map.yaml"
SRC_ROOT = Path(database.__file__).resolve().parent

INSERT_STATEMENT = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
UPDATE_STATEMENT = re.compile(r"UPDATE\s+([A-Za-z_][A-Za-z0-9_]*)\s+SET", re.IGNORECASE)
DELETE_STATEMENT = re.compile(r"DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
VERSION_TOKEN = re.compile(r"(^|[._-])[vV][0-9]+([._-]|$)")


@pytest.fixture(scope="module")
def authority_map() -> dict:
    return yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
async def live_schema(tmp_path, monkeypatch) -> dict[str, list[str]]:
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "authority.db"))
    await database.init_db()
    async with aiosqlite.connect(database.DB_PATH) as db:
        tables = [
            row[0]
            for row in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        schema: dict[str, list[str]] = {}
        for table in tables:
            rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
            schema[table] = [row[1] for row in rows]
    return schema


def scan_sql_writers() -> dict[str, set[str]]:
    """Collect every module that writes each table with literal SQL."""
    writers: dict[str, set[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        module = ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts)
        for pattern in (INSERT_STATEMENT, UPDATE_STATEMENT, DELETE_STATEMENT):
            for match in pattern.finditer(text):
                writers.setdefault(match.group(1), set()).add(module)
    return writers


@pytest.mark.asyncio
async def test_map_covers_every_live_table_and_field(authority_map, live_schema):
    mapped = {entry["table"]: entry for entry in authority_map["sqlite_tables"]}
    assert sorted(mapped) == sorted(live_schema), (
        "The durable-authority map and the live schema disagree on the "
        "table set. Update conformance/durable_authority/authority-map.yaml "
        "in the same change as the schema."
    )
    for table, columns in live_schema.items():
        mapped_fields = [field["name"] for field in mapped[table]["fields"]]
        assert mapped_fields == columns, (
            f"The map and the live schema disagree on the fields of {table}."
        )


def test_every_field_names_one_owner(authority_map):
    for entry in authority_map["sqlite_tables"]:
        assert entry["authority"], entry["table"]
        for field in entry["fields"]:
            label = f"{entry['table']}.{field['name']}"
            assert field.get("owner"), f"{label} names no owner"
            assert isinstance(field["derived"], bool), label
            if field["derived"]:
                assert field.get("reconstruction"), (
                    f"{label} is derived but names no reconstruction rule"
                )
            else:
                assert "reconstruction" not in field, label


def test_every_sql_writer_is_declared(authority_map):
    scanned = scan_sql_writers()
    mapped = {entry["table"]: entry for entry in authority_map["sqlite_tables"]}
    transient = set(authority_map["transient_migration_tables"])

    for table, modules in scanned.items():
        if table in transient:
            continue
        assert table in mapped, (
            f"{sorted(modules)} write the unmapped table {table}. Add the "
            "table to the durable-authority map."
        )
        declared = set(mapped[table]["writers"])
        assert modules == declared, (
            f"The writers of {table} changed: scan found {sorted(modules)}, "
            f"the map declares {sorted(declared)}."
        )

    for table, entry in mapped.items():
        if not entry["writers"]:
            assert table not in scanned, table
            assert entry.get("note"), (
                f"{table} has no writer; the map must say why"
            )
        else:
            assert table in scanned, f"the map declares stale writers for {table}"

    unknown_transient = transient - set(scanned)
    assert not unknown_transient, (
        f"stale transient_migration_tables entries: {sorted(unknown_transient)}"
    )
    assert not transient & set(mapped)


def test_redis_domain_matches_the_protocol_fixture(authority_map):
    domains = {entry["id"]: entry for entry in authority_map["redis_domains"]}
    board = domains["redis.board"]
    fixture_path = REPO_ROOT / board["key_patterns_fixture"]
    assert fixture_path.is_file()
    import json

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    frozen_patterns = [item["pattern"] for item in fixture["record"]["key_patterns"]]
    assert frozen_patterns == sorted(protocol.V2_KEY_PATTERNS)


def test_filesystem_domains_name_real_storage_settings(authority_map):
    fields = StorageConfig.model_fields
    for entry in authority_map["filesystem_domains"]:
        section, _, setting = entry["root_configuration"].partition(".")
        assert section == "storage"
        assert setting in fields, entry["root_configuration"]


def test_owner_names_stay_generation_neutral(authority_map):
    for entry in authority_map["sqlite_tables"]:
        assert not VERSION_TOKEN.search(entry["authority"])
        for field in entry["fields"]:
            assert not VERSION_TOKEN.search(field["owner"])
