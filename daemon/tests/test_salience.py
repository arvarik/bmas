# /opt/bmas/daemon/tests/test_salience.py
"""Salience computation tests (doc 04 §7)."""
from __future__ import annotations

import pytest

from core.entry import BoardEntry
from core.salience import compute_salience, SalienceWeights, DEFAULT_WEIGHTS


def _entry(
    entry_id: str,
    confidence: float = 0.75,
    entry_round: int = 1,
    entry_type: str = "finding",
    refs: list[str] | None = None,
    status: str = "open",
) -> BoardEntry:
    """Build a test BoardEntry."""
    return BoardEntry(
        id=entry_id,
        task_id="test-task",
        type=entry_type,
        author="test-author",
        body="test body content",
        confidence=confidence,
        round=entry_round,
        refs=refs or [],
        status=status,
    )


class TestComputeSalience:
    """Test the compute_salience pure function."""

    def test_single_entry_current_round(self):
        """Single entry in current round → salience = w_c*conf + w_r*1.0."""
        entries = {"e-1": _entry("e-1", confidence=0.8, entry_round=1)}
        scores = compute_salience(entries, current_round=1)
        # Expected: 0.4*0.8 + 0.2*1.0 + 0.3*0 - 0.3*0 = 0.32 + 0.20 = 0.52
        assert abs(scores["e-1"] - 0.52) < 0.01

    def test_recency_decay(self):
        """Entry from 2 rounds ago has lower recency."""
        entries = {"e-1": _entry("e-1", confidence=0.8, entry_round=1)}
        scores = compute_salience(entries, current_round=3)
        # recency = 0.7^(3-1) = 0.49
        # Expected: 0.4*0.8 + 0.2*0.49 + 0.3*0 - 0.3*0 = 0.32 + 0.098 = 0.418
        assert abs(scores["e-1"] - 0.418) < 0.01

    def test_refs_in_boost(self):
        """Entry cited by 3 others → refs_in term maxes at 1.0."""
        e1 = _entry("e-1", confidence=0.5, entry_round=1)
        e2 = _entry("e-2", refs=["e-1"], entry_round=1)
        e3 = _entry("e-3", refs=["e-1"], entry_round=1)
        e4 = _entry("e-4", refs=["e-1"], entry_round=1)
        entries = {"e-1": e1, "e-2": e2, "e-3": e3, "e-4": e4}

        scores = compute_salience(entries, current_round=1)
        # e-1: refs_in = 3 → min(1, 3/3) = 1.0
        # Expected for e-1: 0.4*0.5 + 0.2*1.0 + 0.3*1.0 - 0.3*0 = 0.2 + 0.2 + 0.3 = 0.7
        assert abs(scores["e-1"] - 0.7) < 0.01

    def test_refs_in_partial(self):
        """Entry cited by 1 other → refs_in = 1/3."""
        e1 = _entry("e-1", confidence=0.5, entry_round=1)
        e2 = _entry("e-2", refs=["e-1"], entry_round=1)
        entries = {"e-1": e1, "e-2": e2}

        scores = compute_salience(entries, current_round=1)
        # e-1: refs_in = 1 → min(1, 1/3) ≈ 0.333
        # Expected: 0.4*0.5 + 0.2*1.0 + 0.3*0.333 - 0 = 0.2 + 0.2 + 0.1 = 0.5
        assert abs(scores["e-1"] - 0.5) < 0.02

    def test_penalty_from_critique(self):
        """Entry with unrebutted critique → penalty reduces salience."""
        e1 = _entry("e-1", confidence=0.8, entry_round=1)
        crit = _entry("e-2", entry_type="critique", refs=["e-1"], entry_round=1)
        entries = {"e-1": e1, "e-2": crit}

        scores = compute_salience(entries, current_round=1)
        # e-1: penalty = 1 unrebutted critique → 1/3 ≈ 0.333
        # BUT: the critique also refs e-1, so refs_in = 1 → refs_in_term = 0.3*(1/3) = 0.1
        # Expected: 0.4*0.8 + 0.2*1.0 + 0.3*(1/3) - 0.3*(1/3)
        #         = 0.32 + 0.2 + 0.1 - 0.1 = 0.52
        assert abs(scores["e-1"] - 0.52) < 0.02

    def test_penalty_with_rebuttal(self):
        """Rebutted critique → no penalty."""
        e1 = _entry("e-1", confidence=0.8, entry_round=1)
        crit = _entry("e-2", entry_type="critique", refs=["e-1"], entry_round=1)
        rebuttal = _entry("e-3", entry_type="rebuttal", refs=["e-2"], entry_round=1)
        entries = {"e-1": e1, "e-2": crit, "e-3": rebuttal}

        scores = compute_salience(entries, current_round=1)
        # e-1: rebuttal refs the critique → no penalty
        # Expected: 0.4*0.8 + 0.2*1.0 = 0.52 (no refs_in to e-1 from non-critique open entries)
        # Wait: e-2 (critique) refs e-1 → refs_in=1 for e-1 (critique is open and refs e-1)
        # Also e-3 (rebuttal) refs e-2, not e-1
        # So refs_in for e-1 = 1 (from e-2)
        # Expected: 0.4*0.8 + 0.2*1.0 + 0.3*(1/3) - 0.3*0 = 0.32 + 0.2 + 0.1 - 0 = 0.62
        assert abs(scores["e-1"] - 0.62) < 0.02

    def test_removed_entries_keep_score(self):
        """Removed entries keep their last salience score."""
        e1 = _entry("e-1", confidence=0.8, status="removed")
        e1.salience = 0.42
        entries = {"e-1": e1}

        scores = compute_salience(entries, current_round=2)
        assert scores["e-1"] == 0.42

    def test_empty_entries(self):
        """Zero entries → empty scores."""
        scores = compute_salience({}, current_round=1)
        assert scores == {}

    def test_confidence_zero(self):
        """Confidence exactly 0 → only recency term."""
        entries = {"e-1": _entry("e-1", confidence=0.0, entry_round=1)}
        scores = compute_salience(entries, current_round=1)
        # Expected: 0.4*0 + 0.2*1.0 + 0 - 0 = 0.2
        assert abs(scores["e-1"] - 0.2) < 0.01

    def test_confidence_one(self):
        """Confidence exactly 1 → full confidence term."""
        entries = {"e-1": _entry("e-1", confidence=1.0, entry_round=1)}
        scores = compute_salience(entries, current_round=1)
        # Expected: 0.4*1.0 + 0.2*1.0 = 0.6
        assert abs(scores["e-1"] - 0.6) < 0.01

    def test_custom_weights(self):
        """Custom salience weights are respected."""
        entries = {"e-1": _entry("e-1", confidence=0.5, entry_round=1)}
        weights = SalienceWeights(w_c=1.0, w_r=0.0, w_x=0.0, w_p=0.0)
        scores = compute_salience(entries, current_round=1, weights=weights)
        # Only confidence matters: 1.0 * 0.5 = 0.5
        assert abs(scores["e-1"] - 0.5) < 0.01

    def test_salience_clamped_to_01(self):
        """Salience is clamped to [0, 1]."""
        # High penalty could make score negative → should be 0
        e1 = _entry("e-1", confidence=0.0, entry_round=5)
        c1 = _entry("c-1", entry_type="critique", refs=["e-1"], entry_round=1)
        c2 = _entry("c-2", entry_type="critique", refs=["e-1"], entry_round=1)
        c3 = _entry("c-3", entry_type="critique", refs=["e-1"], entry_round=1)
        entries = {"e-1": e1, "c-1": c1, "c-2": c2, "c-3": c3}

        scores = compute_salience(entries, current_round=1)
        assert scores["e-1"] >= 0.0
        assert scores["e-1"] <= 1.0

    def test_many_refs_in_caps_at_one(self):
        """More than 3 refs_in → refs_in term caps at 1.0."""
        e1 = _entry("e-1", confidence=0.5, entry_round=1)
        others = {
            f"e-{i}": _entry(f"e-{i}", refs=["e-1"], entry_round=1)
            for i in range(2, 12)  # 10 entries citing e-1
        }
        entries = {"e-1": e1, **others}

        scores = compute_salience(entries, current_round=1)
        # refs_in = 10 → min(1, 10/3) = 1.0
        # Expected: 0.4*0.5 + 0.2*1.0 + 0.3*1.0 = 0.7
        assert abs(scores["e-1"] - 0.7) < 0.01
