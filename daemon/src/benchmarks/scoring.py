"""Run deterministic, versioned scorers for benchmark attempts."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


def _numeric_answer(text: str) -> str | None:
    if not text.strip():
        return None
    if "####" in text:
        marked = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text.split("####")[-1])
        if marked:
            return marked[0].replace(",", "")
    patterns = (
        r"(?:the\s+)?answer\s+is\s*[:\s]*(-?\d+(?:,\d{3})*(?:\.\d+)?)",
        r"answer\s*:\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).replace(",", "")
    values = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text)
    return values[-1].replace(",", "") if values else None


def _normalized_number(value: str) -> str:
    try:
        return str(Decimal(value.strip().replace(",", "")).normalize())
    except InvalidOperation:
        return value.strip().replace(",", "")


def _letter_answer(text: str) -> str | None:
    cleaned = text.strip()
    patterns = (
        r"(?:the\s+)?answer\s+is\s*[:\s]*\(?([A-Da-d])\)?",
        r"answer\s*:\s*\(?([A-Da-d])\)?",
        r"correct\s+answer\s+is\s*[:\s]*\(?([A-Da-d])\)?",
        r"\*\*([A-Da-d])\*\*",
        r"^\s*\(?([A-Da-d])\)?\s*[.:\-]",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).upper()
    if re.fullmatch(r"\s*[A-Da-d]\s*", cleaned):
        return cleaned.upper().strip()
    return None


def score_output(
    *,
    scorer: dict[str, Any],
    expected_output: str,
    actual_output: str,
) -> dict[str, Any]:
    """Return one score result without changing source records."""
    kind = str(scorer.get("kind") or "")
    extracted: str | None
    passed: bool
    method: str
    if kind == "numeric_match":
        extracted = _numeric_answer(actual_output)
        passed = extracted is not None and (
            _normalized_number(extracted) == _normalized_number(expected_output)
        )
        method = "numeric_match" if extracted is not None else "no_answer"
    elif kind == "letter_match":
        extracted = _letter_answer(actual_output)
        passed = extracted is not None and extracted == expected_output.strip().upper()
        method = "letter_match" if extracted is not None else "no_answer"
    elif kind == "exact_match":
        extracted = actual_output.strip()
        passed = extracted == expected_output.strip()
        method = "exact_match"
    else:
        raise ValueError(f"Unsupported scorer kind: {kind or 'missing'}")

    return {
        "status": "scored",
        "score": 1.0 if passed else 0.0,
        "passed": passed,
        "extracted_output": extracted,
        "explanation": method,
        "evidence": {
            "expected_output": expected_output,
            "actual_output": actual_output,
            "method": method,
            "scorer_version": scorer.get("version"),
        },
    }
