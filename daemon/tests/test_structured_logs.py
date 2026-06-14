# /opt/bmas/daemon/tests/test_structured_logs.py
"""Tests for the structured, per-agent logging pipeline.

Covers:
- normalize_level: abbreviation/spelling canonicalization (INF→info,
  WRN→warning, ERR→error, DBG→debug) so the UI never renders abbreviations.
- log_entries structured columns: fields/node/turn_id round-trip through
  insert_log_entry → get_task_logs with the JSON `fields` blob decoded.
- _decode_log_row: graceful handling of malformed JSON.
- log message/payload completeness: long messages and payloads are stored
  and returned verbatim (never truncated).
"""
import os
import sys

import aiosqlite
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import database as db  # noqa: E402
from core.log_levels import normalize_level  # noqa: E402
from database import SCHEMA_DDL, _decode_log_row  # noqa: E402

# ── normalize_level ──────────────────────────────────────────────────

class TestNormalizeLevel:
    @pytest.mark.parametrize("raw,expected", [
        ("INF", "info"),
        ("inf", "info"),
        ("info", "info"),
        ("INFO", "info"),
        ("WRN", "warning"),
        ("warn", "warning"),
        ("WARNING", "warning"),
        ("ERR", "error"),
        ("error", "error"),
        ("fatal", "error"),
        ("critical", "error"),
        ("DBG", "debug"),
        ("debug", "debug"),
        ("trace", "debug"),
    ])
    def test_canonicalization(self, raw, expected):
        assert normalize_level(raw) == expected

    def test_none_defaults_to_info(self):
        assert normalize_level(None) == "info"

    def test_unknown_passthrough_lowercased(self):
        assert normalize_level("Notice") == "notice"


# ── _decode_log_row ──────────────────────────────────────────────────

class TestDecodeLogRow:
    def test_decodes_json_fields(self):
        row = {"id": 1, "fields": '{"event": "turn_response", "n": 3}'}
        out = _decode_log_row(row)
        assert out["fields"] == {"event": "turn_response", "n": 3}

    def test_malformed_json_kept_as_raw(self):
        row = {"id": 1, "fields": "{not valid json"}
        out = _decode_log_row(row)
        assert out["fields"] == {"_raw": "{not valid json"}

    def test_null_fields_untouched(self):
        row = {"id": 1, "fields": None}
        out = _decode_log_row(row)
        assert out["fields"] is None


# ── DB round-trip ────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def log_db(tmp_path, monkeypatch):
    """A schema-initialized DB with one parent task for FK integrity."""
    db_path = str(tmp_path / "logs.db")
    monkeypatch.setattr("database.DB_PATH", db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA_DDL)
        await conn.execute(
            "INSERT INTO tasks (id, label, full_input) VALUES ('t-log', 'l', 'i')"
        )
        await conn.commit()
    return db_path


class TestLogRoundTrip:
    @pytest.mark.asyncio
    async def test_structured_fields_round_trip(self, log_db):
        fields = {
            "event": "turn_response",
            "actor": "expert.valuation",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "refs": ["e-1", "e-2"],
        }
        await db.insert_log_entry(
            "t-log", "expert.valuation", "info", "Responded",
            fields=fields, node="agent-node1", turn_id="turn-abc",
        )
        rows = await db.get_task_logs("t-log")
        assert len(rows) == 1
        r = rows[0]
        assert r["agent_role"] == "expert.valuation"
        assert r["level"] == "info"
        assert r["node"] == "agent-node1"
        assert r["turn_id"] == "turn-abc"
        assert r["fields"] == fields  # decoded back to a dict

    @pytest.mark.asyncio
    async def test_long_message_and_payload_not_truncated(self, log_db):
        big_msg = "X" * 12000
        big_output = "Y" * 50000
        await db.insert_log_entry(
            "t-log", "decider", "info", big_msg,
            fields={"output": big_output},
        )
        rows = await db.get_task_logs("t-log")
        assert len(rows[0]["message"]) == 12000
        assert len(rows[0]["fields"]["output"]) == 50000

    @pytest.mark.asyncio
    async def test_backward_compatible_no_fields(self, log_db):
        await db.insert_log_entry("t-log", "daemon", "info", "plain")
        rows = await db.get_task_logs("t-log")
        assert rows[0]["message"] == "plain"
        assert rows[0]["fields"] is None
