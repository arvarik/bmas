# /opt/bmas/daemon/tests/conftest.py
"""Shared test fixtures for Phase 2 gateway tests.

All tests use in-memory fakes — no LLM, no Redis, no network.

NOTE: Test helper functions (make_proposed_entry, make_critique_entry,
make_solution_entry) live in test_helpers.py so they can be imported
by any test module.  conftest.py is for pytest fixtures only.
"""
from __future__ import annotations

import sys
import os

# Add daemon/src to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# Add daemon/tests to path so test_helpers.py is importable
sys.path.insert(0, os.path.dirname(__file__))

import pytest

from core.board_store import InMemoryBoardStore
from core.event_emitter import InMemoryEventEmitter
from core.gateway import BoardGateway, salience_recompute_hook


@pytest.fixture
def board_store():
    """Fresh in-memory board store."""
    return InMemoryBoardStore()


@pytest.fixture
def event_emitter():
    """Fresh in-memory event emitter."""
    return InMemoryEventEmitter()


@pytest.fixture
def gateway(board_store, event_emitter):
    """Pre-configured BoardGateway with salience hook."""
    return BoardGateway(
        board_store=board_store,
        event_emitter=event_emitter,
        recompute_hooks=[salience_recompute_hook],
    )


@pytest.fixture
def gateway_no_hooks(board_store, event_emitter):
    """BoardGateway without recompute hooks (for isolated tests)."""
    return BoardGateway(
        board_store=board_store,
        event_emitter=event_emitter,
        recompute_hooks=[],
    )
