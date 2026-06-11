"""Tests for the blackboard protocol registry (doc 04 §1, §8, §9).

Verifies that Redis v2 key patterns, SSE event names, entry types,
and entry statuses are correctly registered and match the spec.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.protocol import (
    # Entry types
    ENTRY_TYPES,
    ENTRY_TYPE_OBJECTIVE,
    ENTRY_TYPE_ATTACHMENT,
    ENTRY_TYPE_PLAN,
    ENTRY_TYPE_FINDING,
    ENTRY_TYPE_CRITIQUE,
    ENTRY_TYPE_REBUTTAL,
    ENTRY_TYPE_CONFLICT,
    ENTRY_TYPE_DIRECTIVE,
    ENTRY_TYPE_SOLUTION,
    ENTRY_TYPE_ARTIFACT,
    # Entry statuses
    ENTRY_STATUSES,
    ENTRY_STATUS_OPEN,
    ENTRY_STATUS_SUPERSEDED,
    ENTRY_STATUS_REMOVED,
    # Redis keys
    BOARD_ENTRIES_KEY,
    BOARD_EVENTS_KEY,
    BOARD_META_KEY,
    BOARD_PRIVATE_KEY,
    BOARD_SALIENCE_KEY,
    TRACES_KEY,
    FILES_KEY,
    EVENTS_CHANNEL,
    EVENTS_SYSTEM_CHANNEL,
    V2_KEY_PATTERNS,
    resolve_key,
    task_key_patterns,
    # SSE events
    EVENT_BOARD_ENTRY,
    EVENT_ENTRY_REMOVED,
    EVENT_ENTRY_STATUS_CHANGED,
    EVENT_ENTRY_REJECTED,
    EVENT_CONSENSUS,
    EVENT_COORDINATOR_NARRATION,
    EVENT_TRACE,
    EVENT_TURN_START,
    EVENT_TURN_END,
    EVENT_FILE_ADDED,
    EVENT_ARTIFACT_CREATED,
    V2_EVENT_NAMES,
    LEGACY_EVENT_NAMES,
    all_v2_event_names,
    is_v2_event,
    is_legacy_event,
)


# ── Tests: Entry Types (doc 04 §1) ──────────────────────────────────

class TestEntryTypes:

    SPEC_TYPES = {
        "objective", "attachment", "plan", "finding", "critique",
        "rebuttal", "conflict", "directive", "solution", "artifact",
    }

    def test_all_spec_types_registered(self):
        """All 10 entry types from doc 04 §1 are registered."""
        assert ENTRY_TYPES == self.SPEC_TYPES

    def test_entry_count(self):
        """Exactly 10 entry types (matches the spec table)."""
        assert len(ENTRY_TYPES) == 10

    def test_constants_match_set(self):
        """Each constant matches its string in the frozenset."""
        assert ENTRY_TYPE_OBJECTIVE == "objective"
        assert ENTRY_TYPE_ATTACHMENT == "attachment"
        assert ENTRY_TYPE_PLAN == "plan"
        assert ENTRY_TYPE_FINDING == "finding"
        assert ENTRY_TYPE_CRITIQUE == "critique"
        assert ENTRY_TYPE_REBUTTAL == "rebuttal"
        assert ENTRY_TYPE_CONFLICT == "conflict"
        assert ENTRY_TYPE_DIRECTIVE == "directive"
        assert ENTRY_TYPE_SOLUTION == "solution"
        assert ENTRY_TYPE_ARTIFACT == "artifact"


class TestEntryStatuses:

    def test_all_statuses_registered(self):
        """All 3 entry statuses are registered."""
        assert ENTRY_STATUSES == {"open", "superseded", "removed"}

    def test_constants_match(self):
        assert ENTRY_STATUS_OPEN == "open"
        assert ENTRY_STATUS_SUPERSEDED == "superseded"
        assert ENTRY_STATUS_REMOVED == "removed"


# ── Tests: Redis Key Patterns (doc 04 §8) ────────────────────────────

class TestRedisKeyPatterns:

    SPEC_KEY_PATTERNS = {
        "bmas:board:{task}:entries",
        "bmas:board:{task}:events",
        "bmas:board:{task}:meta",
        "bmas:board:{task}:private:{topic}",
        "bmas:board:{task}:salience",
        "bmas:traces:{task}:{turn}",
        "bmas:files:{task}",
        "bmas:events:{task}",
    }

    def test_all_spec_keys_registered(self):
        """All 8 key patterns from doc 04 §8 are in the registry."""
        registered = set(V2_KEY_PATTERNS.keys())
        assert registered == self.SPEC_KEY_PATTERNS

    def test_key_count(self):
        """Exactly 8 key patterns (matches the spec table)."""
        assert len(V2_KEY_PATTERNS) == 8

    def test_each_entry_has_type_and_purpose(self):
        """Every registry entry has a 'type' and 'purpose' field."""
        for pattern, meta in V2_KEY_PATTERNS.items():
            assert "type" in meta, f"Missing 'type' for {pattern}"
            assert "purpose" in meta, f"Missing 'purpose' for {pattern}"

    def test_redis_types_are_valid(self):
        """Redis types are one of: Hash, Stream, ZSet, Channel."""
        valid = {"Hash", "Stream", "ZSet", "Channel"}
        for pattern, meta in V2_KEY_PATTERNS.items():
            assert meta["type"] in valid, f"Invalid type for {pattern}: {meta['type']}"

    def test_constants_match_patterns(self):
        """Individual key constants match their expected patterns."""
        assert BOARD_ENTRIES_KEY == "bmas:board:{task}:entries"
        assert BOARD_EVENTS_KEY == "bmas:board:{task}:events"
        assert BOARD_META_KEY == "bmas:board:{task}:meta"
        assert BOARD_PRIVATE_KEY == "bmas:board:{task}:private:{topic}"
        assert BOARD_SALIENCE_KEY == "bmas:board:{task}:salience"
        assert TRACES_KEY == "bmas:traces:{task}:{turn}"
        assert FILES_KEY == "bmas:files:{task}"
        assert EVENTS_CHANNEL == "bmas:events:{task}"
        assert EVENTS_SYSTEM_CHANNEL == "bmas:events:system"

    def test_resolve_key_board_entries(self):
        """resolve_key produces the correct concrete key."""
        key = resolve_key(BOARD_ENTRIES_KEY, task="task-abc")
        assert key == "bmas:board:task-abc:entries"

    def test_resolve_key_private(self):
        """resolve_key handles two placeholders."""
        key = resolve_key(BOARD_PRIVATE_KEY, task="t1", topic="conflict-a")
        assert key == "bmas:board:t1:private:conflict-a"

    def test_resolve_key_traces(self):
        key = resolve_key(TRACES_KEY, task="t1", turn="turn-3")
        assert key == "bmas:traces:t1:turn-3"

    def test_resolve_key_missing_kwarg_raises(self):
        """resolve_key raises KeyError for missing placeholder."""
        with pytest.raises(KeyError):
            resolve_key(BOARD_ENTRIES_KEY)

    def test_task_key_patterns_returns_per_task(self):
        """task_key_patterns returns all patterns containing {task}."""
        patterns = task_key_patterns()
        for p in patterns:
            assert "{task}" in p
        # All 8 patterns contain {task}
        assert len(patterns) == 8


# ── Tests: SSE Event Names (doc 04 §9) ───────────────────────────────

class TestSSEEventNames:

    SPEC_V2_EVENTS = {
        "board_entry",
        "entry_removed",
        "entry_status_changed",
        "entry_rejected",
        "consensus",
        "coordinator_narration",
        "trace",
        "turn_start",
        "turn_end",
        "file_added",
        "artifact_created",
    }

    SPEC_LEGACY_EVENTS = {
        "debate", "subtask", "phase", "log", "cost", "complete",
    }

    def test_all_spec_v2_events_registered(self):
        """All 11 v2 event names from doc 04 §9 + doc 05 §1.2 are registered."""
        registered = set(V2_EVENT_NAMES.keys())
        assert registered == self.SPEC_V2_EVENTS

    def test_v2_event_count(self):
        """Exactly 11 v2 events (doc 04 §9 + coordinator_narration from doc 05 §1.2)."""
        assert len(V2_EVENT_NAMES) == 11

    def test_legacy_events_complete(self):
        """All 6 legacy event names are listed."""
        assert LEGACY_EVENT_NAMES == self.SPEC_LEGACY_EVENTS

    def test_constants_match_names(self):
        """Individual event constants match their string values."""
        assert EVENT_BOARD_ENTRY == "board_entry"
        assert EVENT_ENTRY_REMOVED == "entry_removed"
        assert EVENT_ENTRY_STATUS_CHANGED == "entry_status_changed"
        assert EVENT_ENTRY_REJECTED == "entry_rejected"
        assert EVENT_CONSENSUS == "consensus"
        assert EVENT_COORDINATOR_NARRATION == "coordinator_narration"
        assert EVENT_TRACE == "trace"
        assert EVENT_TURN_START == "turn_start"
        assert EVENT_TURN_END == "turn_end"
        assert EVENT_FILE_ADDED == "file_added"
        assert EVENT_ARTIFACT_CREATED == "artifact_created"

    def test_no_v2_legacy_overlap(self):
        """V2 and legacy event names must not overlap."""
        overlap = set(V2_EVENT_NAMES.keys()) & LEGACY_EVENT_NAMES
        assert not overlap, f"Overlapping event names: {overlap}"

    def test_all_v2_event_names_sorted(self):
        """all_v2_event_names() returns sorted list."""
        names = all_v2_event_names()
        assert names == sorted(names)
        assert len(names) == 11

    def test_is_v2_event(self):
        """is_v2_event() correctly identifies v2 events."""
        assert is_v2_event("board_entry") is True
        assert is_v2_event("consensus") is True
        assert is_v2_event("debate") is False
        assert is_v2_event("unknown") is False

    def test_is_legacy_event(self):
        """is_legacy_event() correctly identifies legacy events."""
        assert is_legacy_event("debate") is True
        assert is_legacy_event("complete") is True
        assert is_legacy_event("board_entry") is False
        assert is_legacy_event("unknown") is False

    def test_each_event_has_description(self):
        """Every registered v2 event has a non-empty description."""
        for name, desc in V2_EVENT_NAMES.items():
            assert isinstance(desc, str), f"Description for {name} is not a string"
            assert len(desc) > 5, f"Description for {name} is too short"
