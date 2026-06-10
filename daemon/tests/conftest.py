# /opt/bmas/daemon/tests/conftest.py
"""Shared test fixtures for Phase 2 gateway tests.

All tests use in-memory fakes — no LLM, no Redis, no network.
"""
from __future__ import annotations

import sys
import os

# Add daemon/src to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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


# ── Helpers ──────────────────────────────────────────────────────────

def make_proposed_entry(**overrides):
    """Create a minimal valid proposed entry dict."""
    base = {
        "type": "finding",
        "title": "Test finding",
        "body": "This is a test finding with evidence.",
        "refs": [],
        "confidence": 0.75,
    }
    base.update(overrides)
    return base


def make_critique_entry(refs=None, **overrides):
    """Create a critique entry dict."""
    base = {
        "type": "critique",
        "title": "Test critique",
        "body": "This finding has issues because...",
        "refs": refs or [],
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


def make_solution_entry(**overrides):
    """Create a solution entry dict."""
    base = {
        "type": "solution",
        "title": "Final answer",
        "body": "Based on the debate, the conclusion is...",
        "refs": [],
        "confidence": 0.9,
    }
    base.update(overrides)
    return base
