"""Benchmark scorers — compatibility shims over the daemon scorer plugins.

The daemon deterministic scorer owns numeric (last number convention)
and letter (A/B/C/D) scoring. These functions stay for one deprecation
cycle: with a configured client they delegate to the daemon preview
endpoint, and without one they fall back to the local logic, warn, and
record the fallback through ``FALLBACK_RECORDER`` when one is set.
"""

from __future__ import annotations

import re
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


# One optional evaluation client and one optional fallback recorder,
# set by the CLI. Tests inject both.
SCORER_CLIENT = None
FALLBACK_RECORDER = None


def _delegate(plugin_configuration: dict, expected: str, response: str):
    """Score through the daemon boundary when a client is configured."""
    if SCORER_CLIENT is None:
        return None
    preview = SCORER_CLIENT.preview_score(
        "deterministic",
        {"final_output": response, "reference_answer": expected},
        plugin_configuration,
    )
    result = preview.get("result") or {}
    if result.get("status") != "scored":
        return None
    dimension = (result.get("dimensions") or [{}])[0]
    return dimension.get("category"), bool(result.get("passed")), str(
        result.get("explanation") or "daemon",
    )


def _local_fallback(entry_point: str) -> None:
    import warnings

    warnings.warn(
        f"{entry_point} scored locally; the daemon scorer plugin is the "
        "authority and this shim stays for one deprecation cycle",
        DeprecationWarning,
        stacklevel=3,
    )
    if FALLBACK_RECORDER is not None:
        FALLBACK_RECORDER(entry_point)


def score_gsm8k(expected: str, response: str) -> tuple[str | None, bool, str]:
    """Score a GSM8K response against the expected numeric answer.

    Returns (extracted_answer, correct, score_method).

    Convention: the final numeric value in the response is the model's answer
    (matching GSM8K evaluation standard). The expected answer has commas
    already stripped by the dataset loader.
    """
    delegated = _delegate({"comparison": "last_number"}, expected, response)
    if delegated is not None:
        return delegated
    _local_fallback("eval.scorer.score_gsm8k")
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
    delegated = _delegate(
        {"comparison": "multiple_choice", "choices": ["A", "B", "C", "D"]},
        expected, response,
    )
    if delegated is not None:
        return delegated
    _local_fallback("eval.scorer.score_mmlu")
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

    NOTE: The float() round-trip loses precision for integers > 2^53
    (e.g. 9007199254740993 becomes 9007199254740992).  GSM8K answers are
    small enough that this is safe, but if this scorer is extended to
    datasets with very large numeric answers, switch to decimal.Decimal.
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
