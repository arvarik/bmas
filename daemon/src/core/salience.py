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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.entry import BoardEntry


@dataclass(frozen=True)
class SalienceWeights:
    """Configurable weights for the salience formula (doc 04 §7)."""
    w_c: float = 0.4   # confidence weight
    w_r: float = 0.2   # recency weight
    w_x: float = 0.3   # refs-in weight (citations)
    w_p: float = 0.3   # penalty weight (open critiques)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> SalienceWeights:
        """Build the weights from the ``coordination.board.salience_weights`` shape.

        The mapping names the components ``confidence``, ``recency``,
        ``refs_in``, and ``penalty``. A missing component keeps its
        default weight.
        """
        values = dict(mapping or {})
        defaults = cls()
        return cls(
            w_c=float(values.get("confidence", defaults.w_c)),
            w_r=float(values.get("recency", defaults.w_r)),
            w_x=float(values.get("refs_in", defaults.w_x)),
            w_p=float(values.get("penalty", defaults.w_p)),
        )

    def to_mapping(self) -> dict[str, float]:
        """Return the ``coordination.board.salience_weights`` shape."""
        return {
            "confidence": self.w_c,
            "recency": self.w_r,
            "refs_in": self.w_x,
            "penalty": self.w_p,
        }


DEFAULT_WEIGHTS = SalienceWeights()

# The multiplier one operator boost applies. A boost is a separate
# salience component: the recompute hook derives the base score from
# the board and then applies every recorded boost factor.
OPERATOR_BOOST_FACTOR = 2.0


def _clamp01(value: float) -> float:
    """Clamp a float to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))


def apply_salience_boosts(
    scores: dict[str, float],
    boosts: Mapping[str, Any] | None,
) -> dict[str, float]:
    """Apply the recorded operator boost factors to computed scores.

    ``boosts`` maps an entry identifier to its accumulated factor. An
    entry without a score, or a factor that is not a positive number,
    leaves the score unchanged.
    """
    if not boosts:
        return dict(scores)
    boosted = dict(scores)
    for entry_id, factor in boosts.items():
        if entry_id not in boosted:
            continue
        try:
            multiplier = float(factor)
        except (TypeError, ValueError):
            continue
        if multiplier <= 0.0:
            continue
        boosted[entry_id] = max(0.0, min(1.0, boosted[entry_id] * multiplier))
    return boosted


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
