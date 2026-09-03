"""The scored evaluation item and its aggregate accuracy helpers.

Scoring itself lives in the daemon scorer plugins; the evaluation
harness only carries the scored result and totals it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class ScoredResult:
    """An evaluation item with scoring results."""

    id: str
    question: str
    expected_answer: str
    actual_response: str
    dataset: str
    subject: str | None
    extracted_answer: str | None
    correct: bool
    score_method: str  # "numeric_match" | "letter_match" | "text_fallback" | "no_answer"
    task_id: str | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    tokens: int | None = None
    model_used: str | None = None
    terminated_by: str | None = None
    status: str | None = None
    error_message: str | None = None
    rounds: int | None = None
    effective_actions: int | None = None
    variant: str | None = None
    phase: str | None = None
    state_revision: int | None = None
    recovery_count: int | None = None
    variant_metrics: dict[str, object] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_accuracy(results: list[ScoredResult]) -> float:
    """Compute overall accuracy from scored results.

    Returns 0.0 for empty input (no division by zero).
    """
    if not results:
        return 0.0
    correct = sum(1 for r in results if r.correct)
    return correct / len(results)


def compute_accuracy_by_subject(results: list[ScoredResult]) -> dict[str, float]:
    """Compute per-subject accuracy (MMLU breakdown).

    Returns {subject: accuracy} for all subjects with at least one result.
    """
    by_subject: dict[str, list[bool]] = {}
    for r in results:
        if r.subject:
            by_subject.setdefault(r.subject, []).append(r.correct)

    return {
        subj: sum(correct_list) / len(correct_list) if correct_list else 0.0
        for subj, correct_list in sorted(by_subject.items())
    }
