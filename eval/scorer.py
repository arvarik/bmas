"""Benchmark scorers — extract and compare model answers against ground truth.

GSM8K: numeric answer extraction (last number convention).
MMLU: letter answer extraction (A/B/C/D).

See docs/proposals/10-migration-and-rollout.md Phase E bullet 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field


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

    def to_dict(self) -> dict:
        return asdict(self)


def score_gsm8k(expected: str, response: str) -> tuple[str | None, bool, str]:
    """Score a GSM8K response against the expected numeric answer.

    Returns (extracted_answer, correct, score_method).

    Convention: the final numeric value in the response is the model's answer
    (matching GSM8K evaluation standard). The expected answer has commas
    already stripped by the dataset loader.
    """
    if not response or not response.strip():
        return None, False, "no_answer"

    extracted = _extract_gsm8k_answer(response)
    if extracted is None:
        return None, False, "no_answer"

    # Normalize: strip commas, whitespace, trailing zeros for decimals
    norm_expected = _normalize_number(expected)
    norm_extracted = _normalize_number(extracted)

    correct = norm_expected == norm_extracted
    return extracted, correct, "numeric_match"


def score_mmlu(expected: str, response: str) -> tuple[str | None, bool, str]:
    """Score an MMLU response against the expected letter (A/B/C/D).

    Returns (extracted_answer, correct, score_method).

    Extraction priority:
      1. Explicit letter pattern (e.g., "The answer is B", "(C)", "Answer: D")
      2. Standalone letter at start of response
      3. No match → no_answer
    """
    if not response or not response.strip():
        return None, False, "no_answer"

    expected_upper = expected.strip().upper()

    extracted = _extract_mmlu_letter(response)
    if extracted is not None:
        correct = extracted == expected_upper
        return extracted, correct, "letter_match"

    return None, False, "no_answer"


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


# ── Internal extraction helpers ──────────────────────────────────────


def _extract_gsm8k_answer(text: str) -> str | None:
    """Extract the final numeric answer from a GSM8K-style response.

    Priority:
      1. After "####" marker (GSM8K chain-of-thought format)
      2. After "the answer is" / "answer:" phrases
      3. Last number in the response (GSM8K convention)
    """
    # 1. Look for #### marker
    if "####" in text:
        after = text.split("####")[-1].strip()
        numbers = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", after)
        if numbers:
            return numbers[0].replace(",", "")

    # 2. Look for "the answer is <number>" or "answer: <number>"
    answer_patterns = [
        r"(?:the\s+)?answer\s+is\s*[:\s]*(-?\d+(?:,\d{3})*(?:\.\d+)?)",
        r"answer\s*:\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)",
        r"=\s*\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*$",
    ]
    for pattern in answer_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).replace(",", "")

    # 3. Last number in the response
    all_numbers = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text)
    if all_numbers:
        return all_numbers[-1].replace(",", "")

    return None


def _extract_mmlu_letter(text: str) -> str | None:
    """Extract a letter answer (A/B/C/D) from an MMLU-style response.

    Priority:
      1. Explicit answer patterns: "The answer is X", "Answer: X", "(X)"
      2. Standalone letter at the start
      3. Any isolated A/B/C/D
    """
    text_clean = text.strip()

    # 1. Explicit patterns
    explicit_patterns = [
        r"(?:the\s+)?answer\s+is\s*[:\s]*\(?([A-Da-d])\)?",
        r"answer\s*:\s*\(?([A-Da-d])\)?",
        r"correct\s+answer\s+is\s*[:\s]*\(?([A-Da-d])\)?",
        r"\*\*([A-Da-d])\*\*",  # Markdown bold
        r"^\s*\(?([A-Da-d])\)?\s*[.:\-]",  # Letter at start with punctuation
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, text_clean, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).upper()

    # 2. Standalone letter at start of response
    start_match = re.match(r"^\s*([A-Da-d])\s*$", text_clean.split("\n")[0])
    if start_match:
        return start_match.group(1).upper()

    # 3. Single isolated letter in short responses (< 20 chars)
    if len(text_clean) < 20:
        letters = re.findall(r"\b([A-Da-d])\b", text_clean)
        if len(letters) == 1:
            return letters[0].upper()

    return None


def _normalize_number(s: str) -> str:
    """Normalize a numeric string for comparison.

    Strips commas, leading/trailing whitespace, and trailing .0 for integers.
    """
    s = s.strip().replace(",", "")
    # Try to normalize as float then back
    try:
        val = float(s)
        # If it's an integer value, represent as int
        if val == int(val):
            return str(int(val))
        return str(val)
    except (ValueError, OverflowError):
        return s
