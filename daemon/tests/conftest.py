# /opt/bmas/daemon/tests/conftest.py
"""Shared test fixtures — mock config before any route imports.

config.py calls sys.exit(1) when it can't find bmas.yaml or required
env vars at import time.  We intercept this by inserting a fake config
module into sys.modules BEFORE any route module is imported.

Also provides shared fixtures for gateway tests (board_store,
event_emitter, gateway, gateway_no_hooks) to support the Phase 2
test_gateway.py suite.
"""

import os
import sys
import types

import pytest

# ── Path Setup ──────────────────────────────────────────────────────
# Add both src/ (for app modules) and tests/ (for test_helpers) to path.

_TESTS_DIR = os.path.dirname(__file__)
_SRC_DIR = os.path.join(_TESTS_DIR, "..", "src")

# Insert at start so these take precedence
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


# ── Fake config module ──────────────────────────────────────────────
# Insert BEFORE any test or route tries `import config`

_fake_config = types.ModuleType("config")
_fake_config.STORAGE_ENABLED = False
_fake_config.STORAGE_USER_MEDIA_DIR = "/tmp/bmas-test-uploads"
_fake_config.STORAGE_ARTIFACTS_DIR = "/tmp/bmas-test-output"
_fake_config.STORAGE_MAX_UPLOAD_MB = 50
_fake_config.STORAGE_MAX_TASK_OUTPUT_MB = 500
_fake_config.STORAGE_ALLOWED_TYPES = {"pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"}
_fake_config.STORAGE_PDF_EXTRACTION = "pymupdf"
_fake_config.STORAGE_EXTRACTION_MAX_CHARS = 60000
_fake_config.BMAS_NODE_KEY = ""
_fake_config.STORAGE_CONFIG = {
    "enabled": False,
    "user_media_dir": "/tmp/bmas-test-uploads",
    "artifacts_dir": "/tmp/bmas-test-output",
    "max_upload_mb": 50,
    "max_task_output_mb": 500,
    "allowed_upload_types": ["pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"],
    "pdf_extraction": "pymupdf",
    "extraction_max_chars": 60000,
}

# Inject BEFORE any real import can trigger sys.exit
sys.modules["config"] = _fake_config


# ── Gateway Test Fixtures (Phase 2) ─────────────────────────────────
# These fixtures were previously inline or in the old conftest.  They
# support test_gateway.py (~45 tests covering the core gateway pipeline).

@pytest.fixture
def board_store():
    """Fresh in-memory board store for each test."""
    from core.board_store import InMemoryBoardStore
    return InMemoryBoardStore()


@pytest.fixture
def event_emitter():
    """Fresh in-memory event emitter for each test."""
    from core.event_emitter import InMemoryEventEmitter
    return InMemoryEventEmitter()


@pytest.fixture
def gateway(board_store, event_emitter):
    """Standard BoardGateway with default hooks."""
    from core.gateway import BoardGateway, salience_recompute_hook
    return BoardGateway(
        board_store, event_emitter,
        recompute_hooks=[salience_recompute_hook],
    )


@pytest.fixture
def gateway_no_hooks(board_store, event_emitter):
    """BoardGateway with no recompute hooks (for replay tests)."""
    from core.gateway import BoardGateway
    return BoardGateway(board_store, event_emitter)
