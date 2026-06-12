# /opt/bmas/daemon/tests/test_auth.py
"""
Tests for the shared auth module (auth.py).

Covers:
- check_bearer_or_pass: dashboard-bypass, valid bearer, invalid bearer
- require_node_key: missing, wrong, valid, dev-mode (no key)
"""

import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from auth import check_bearer_or_pass, require_node_key  # noqa: E402


def _make_request(auth_header: str | None = None) -> MagicMock:
    """Build a mock FastAPI Request with the given Authorization header."""
    req = MagicMock()
    if auth_header is not None:
        req.headers = {"Authorization": auth_header}
    else:
        req.headers = {}
    return req


# ── check_bearer_or_pass ────────────────────────────────────────────────

class TestCheckBearerOrPass:
    """Tests for the dashboard-bypass auth helper."""

    def test_no_key_configured_always_passes(self):
        """When node_key is empty, any request passes (dev mode)."""
        req = _make_request()
        check_bearer_or_pass(req, "")  # should not raise

    def test_no_key_configured_with_bad_header_still_passes(self):
        """Even with a wrong header, if no key is configured, allow (dev mode)."""
        req = _make_request("Bearer wrong-key")
        check_bearer_or_pass(req, "")  # should not raise

    def test_valid_bearer_token(self):
        """Correct bearer token passes."""
        req = _make_request("Bearer my-secret-key")
        check_bearer_or_pass(req, "my-secret-key")  # should not raise

    def test_valid_bearer_with_extra_whitespace(self):
        """Token with trailing whitespace is accepted (stripped)."""
        req = _make_request("Bearer my-secret-key ")
        check_bearer_or_pass(req, "my-secret-key")  # should not raise

    def test_wrong_bearer_token_raises_401(self):
        """Wrong bearer token raises HTTPException 401."""
        from fastapi import HTTPException
        req = _make_request("Bearer wrong-key")
        with pytest.raises(HTTPException) as exc_info:
            check_bearer_or_pass(req, "correct-key")
        assert exc_info.value.status_code == 401

    def test_no_auth_header_passes_when_key_configured(self):
        """No auth header (dashboard session) passes even when key is configured."""
        req = _make_request()
        check_bearer_or_pass(req, "some-key")  # should not raise (dashboard session)

    def test_non_bearer_scheme_raises_401(self):
        """Basic auth or other schemes are rejected when token is present."""
        from fastapi import HTTPException
        req = _make_request("Basic dXNlcjpwYXNz")
        with pytest.raises(HTTPException) as exc_info:
            check_bearer_or_pass(req, "some-key")
        assert exc_info.value.status_code == 401

    def test_bearer_without_space_raises_401(self):
        """'Bearer' without trailing space is treated as wrong auth format."""
        from fastapi import HTTPException
        req = _make_request("Bearermy-secret-key")
        with pytest.raises(HTTPException) as exc_info:
            check_bearer_or_pass(req, "my-secret-key")
        assert exc_info.value.status_code == 401


# ── require_node_key ────────────────────────────────────────────────────

class TestRequireNodeKey:
    """Tests for the strict node-key auth helper (ingest/artifacts)."""

    def test_dev_mode_no_key_always_passes(self):
        """When node_key is empty, any request passes (dev mode)."""
        req = _make_request()
        require_node_key(req, "")  # should not raise

    def test_valid_bearer_token(self):
        """Correct bearer token passes without exception."""
        req = _make_request("Bearer test-node-key-xyz")
        require_node_key(req, "test-node-key-xyz")  # should not raise

    def test_missing_auth_header_raises_value_error(self):
        """Missing Authorization header raises ValueError."""
        req = _make_request()
        with pytest.raises(ValueError, match="[Mm]issing"):
            require_node_key(req, "some-key")

    def test_wrong_token_raises_value_error(self):
        """Wrong token raises ValueError."""
        req = _make_request("Bearer wrong-token")
        with pytest.raises(ValueError, match="[Ii]nvalid"):
            require_node_key(req, "correct-token")

    def test_non_bearer_scheme_raises_value_error(self):
        """Non-Bearer auth scheme raises ValueError (missing bearer token)."""
        req = _make_request("Basic dXNlcjpwYXNz")
        with pytest.raises(ValueError, match="[Mm]issing"):
            require_node_key(req, "some-key")

    def test_bearer_with_correct_token_stripped(self):
        """Token with trailing whitespace is accepted."""
        req = _make_request("Bearer my-key ")
        require_node_key(req, "my-key")  # should not raise

    def test_correct_token_no_exception(self):
        """Verify no side effects on successful auth."""
        req = _make_request("Bearer abc-123-def")
        result = require_node_key(req, "abc-123-def")
        assert result is None  # returns None on success
