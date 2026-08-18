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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def _relationship_indexes(
    entries: dict[str, BoardEntry],
) -> tuple[dict[str, int], dict[str, int]]:
    """Build citation and unrebutted-critique counts in linear time."""
    refs_in: dict[str, int] = {}
    critiques_by_target: dict[str, list[str]] = {}
    rebutted_critiques: set[str] = set()

    for entry in entries.values():
        if entry.status != "open":
            continue
        for target_id in set(entry.refs):
            refs_in[target_id] = refs_in.get(target_id, 0) + 1
            if entry.type == "critique":
                critiques_by_target.setdefault(target_id, []).append(entry.id)
            elif entry.type == "rebuttal":
                rebutted_critiques.add(target_id)

    penalties: dict[str, int] = {}
    for target_id, critique_ids in critiques_by_target.items():
        penalties[target_id] = sum(
            critique_id not in rebutted_critiques
            for critique_id in critique_ids
        )
    return refs_in, penalties


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
    refs_in, unrebutted_critiques = _relationship_indexes(entries)

    for entry_id, entry in entries.items():
        if entry.status != "open":
            # Preserve existing salience for non-open entries
            scores[entry_id] = entry.salience
            continue

        confidence_term = w.w_c * entry.confidence
        recency_term = w.w_r * _recency(entry.round, current_round)
        refs_in_term = w.w_x * min(1.0, refs_in.get(entry_id, 0) / 3.0)
        penalty = min(1.0, unrebutted_critiques.get(entry_id, 0) / 3.0)
        penalty_term = w.w_p * penalty

        score = _clamp01(
            confidence_term + recency_term + refs_in_term - penalty_term
        )
        scores[entry_id] = score

    return scores
