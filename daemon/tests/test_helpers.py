# /opt/bmas/daemon/tests/test_helpers.py
"""Shared test helper functions for Phase 2 tests.

These are plain functions (not fixtures) that build common test data.
Import from any test module:
    from test_helpers import make_proposed_entry, make_critique_entry, make_solution_entry
"""
from __future__ import annotations


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
