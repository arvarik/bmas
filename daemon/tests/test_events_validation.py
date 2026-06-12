# /opt/bmas/daemon/tests/test_events_validation.py
"""
Tests for input validation in the events SSE endpoint (routes/events.py).

Covers the _validate_task_id helper which guards against malformed task IDs
before they reach the Redis pub/sub layer.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from routes.events import _validate_task_id  # noqa: E402


class TestValidateTaskId:
    """Tests for the _validate_task_id helper."""

    # ── Valid IDs ───────────────────────────────────────────────────────

    def test_standard_task_id(self):
        """Standard bMAS task IDs (task-{8 hex chars}) pass."""
        assert _validate_task_id("task-a1b2c3d4") is True

    def test_alphanumeric_only(self):
        """Pure alphanumeric IDs pass."""
        assert _validate_task_id("task12345678") is True

    def test_with_underscores(self):
        """Underscores are allowed."""
        assert _validate_task_id("task_abc_123") is True

    def test_with_hyphens(self):
        """Hyphens are allowed."""
        assert _validate_task_id("task-abc-123") is True

    def test_minimum_length(self):
        """Single character ID is valid (minimum 1)."""
        assert _validate_task_id("a") is True

    def test_maximum_length(self):
        """64-character ID is valid (maximum 64)."""
        assert _validate_task_id("a" * 64) is True

    def test_mixed_case(self):
        """Mixed case letters are allowed."""
        assert _validate_task_id("Task-ABC-123-def") is True

    # ── Invalid IDs ─────────────────────────────────────────────────────

    def test_empty_string_rejected(self):
        """Empty string fails (minimum length 1)."""
        assert _validate_task_id("") is False

    def test_too_long_rejected(self):
        """65-character ID exceeds the 64-char maximum."""
        assert _validate_task_id("a" * 65) is False

    def test_path_traversal_rejected(self):
        """Path traversal sequences are rejected."""
        assert _validate_task_id("../etc/passwd") is False

    def test_null_byte_rejected(self):
        """Null byte injection is rejected."""
        assert _validate_task_id("task\x00evil") is False

    def test_spaces_rejected(self):
        """Spaces are not allowed."""
        assert _validate_task_id("task id") is False

    def test_colon_rejected(self):
        """Redis key separators (colons) are not allowed."""
        assert _validate_task_id("task:id") is False

    def test_slash_rejected(self):
        """Slashes are not allowed."""
        assert _validate_task_id("task/id") is False

    def test_dot_rejected(self):
        """Dots are not allowed."""
        assert _validate_task_id("task.id") is False

    def test_redis_wildcard_rejected(self):
        """Redis glob wildcards are not allowed."""
        assert _validate_task_id("task*") is False
        assert _validate_task_id("task?") is False

    def test_newline_rejected(self):
        """Newline characters are not allowed."""
        assert _validate_task_id("task\nid") is False

    def test_sql_injection_pattern_rejected(self):
        """SQL injection patterns are rejected."""
        assert _validate_task_id("'; DROP TABLE tasks; --") is False

    def test_unicode_rejected(self):
        """Unicode characters outside ASCII alphanumeric range are rejected."""
        assert _validate_task_id("tâsk-id") is False
