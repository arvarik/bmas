# /opt/bmas/daemon/src/core/salience.py
"""Deterministic salience scoring (doc 04 §7).

salience(e) = clamp01(
    w_c · confidence(e)
  + w_r · recency(e)          # 1.0 now → decays over rounds
  + w_x · min(1, refs_in(e)/3)  # how many entries cite/respond to e
  - w_p · penalty(e)          # open critiques against e, unrebutted
)

Registered as a recompute_derived hook (seam rule 5).
Pure function, no I/O, fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.entry import BoardEntry


@dataclass(frozen=True)
class SalienceWeights:
    """Configurable weights for the salience formula (doc 04 §7)."""
    w_c: float = 0.4   # confidence weight
    w_r: float = 0.2   # recency weight
    w_x: float = 0.3   # refs-in weight (citations)
    w_p: float = 0.3   # penalty weight (open critiques)


DEFAULT_WEIGHTS = SalienceWeights()


def _clamp01(value: float) -> float:
    """Clamp a float to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))


def _recency(entry_round: int, current_round: int) -> float:
    """Recency score: 1.0 for current round, decays exponentially.

    decay = 0.7^(current_round - entry_round)
    """
    if current_round <= 0 or entry_round <= 0:
        return 1.0
    gap = max(0, current_round - entry_round)
    return 0.7 ** gap


def _refs_in_count(
    entry_id: str, entries: dict[str, BoardEntry]
) -> int:
    """Count how many other open entries cite this entry."""
    count = 0
    for other in entries.values():
        if other.status != "open":
            continue
        if entry_id in other.refs:
            count += 1
    return count


def _penalty(
    entry_id: str, entries: dict[str, BoardEntry]
) -> float:
    """Penalty: number of open, unrebutted critiques against this entry.

    A critique is unrebutted if no rebuttal in the board refs the critique.
    Returns 0..1 (capped at 1.0 for ≥3 unrebutted critiques).
    """
    unrebutted = 0
    # Find all critiques that reference this entry
    critiques_of_entry: list[str] = []
    for other in entries.values():
        if (
            other.status == "open"
            and other.type == "critique"
            and entry_id in other.refs
        ):
            critiques_of_entry.append(other.id)

    # Check if each critique has a rebuttal
    for crit_id in critiques_of_entry:
        has_rebuttal = False
        for other in entries.values():
            if (
                other.status == "open"
                and other.type == "rebuttal"
                and crit_id in other.refs
            ):
                has_rebuttal = True
                break
        if not has_rebuttal:
            unrebutted += 1

    # Cap at 1.0 for 3+ unrebutted critiques
    return min(1.0, unrebutted / 3.0) if unrebutted > 0 else 0.0


def compute_salience(
    entries: dict[str, BoardEntry],
    current_round: int,
    weights: SalienceWeights | None = None,
) -> dict[str, float]:
    """Compute salience scores for all open entries.

    Returns a dict of entry_id → salience score.
    Only computes for open entries (removed/superseded keep their last score).
    """
    w = weights or DEFAULT_WEIGHTS
    scores: dict[str, float] = {}

    for entry_id, entry in entries.items():
        if entry.status != "open":
            # Preserve existing salience for non-open entries
            scores[entry_id] = entry.salience
            continue

        confidence_term = w.w_c * entry.confidence
        recency_term = w.w_r * _recency(entry.round, current_round)
        refs_in = _refs_in_count(entry_id, entries)
        refs_in_term = w.w_x * min(1.0, refs_in / 3.0)
        penalty_term = w.w_p * _penalty(entry_id, entries)

        score = _clamp01(
            confidence_term + recency_term + refs_in_term - penalty_term
        )
        scores[entry_id] = score

    return scores
