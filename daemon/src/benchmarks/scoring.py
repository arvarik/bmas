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


def _within_tolerance(extracted: str, expected: str, tolerance: str) -> bool:
    """Compare two numbers exactly or inside one decimal tolerance."""
    try:
        left = Decimal(extracted.strip().replace(",", ""))
        right = Decimal(expected.strip().replace(",", ""))
        bound = Decimal(tolerance)
    except InvalidOperation:
        return False
    return abs(left - right) <= bound


def score_output(
    *,
    scorer: dict[str, Any],
    expected_output: str,
    actual_output: str,
) -> dict[str, Any]:
    """Score one output under the complete effective configuration.

    The effective configuration is the validated per-revision scorer
    configuration. It changes scorer behavior, and its checksum
    travels with every stored score.
    """
    kind = str(scorer.get("kind") or "")
    configuration = scorer.get("configuration") or {}
    if not isinstance(configuration, dict):
        raise ValueError("The scorer configuration must be one object")
    extracted: str | None
    passed: bool
    method: str
    if kind == "numeric_match":
        tolerance = str(configuration.get("tolerance") or "0")
        extracted = _numeric_answer(actual_output)
        if extracted is None:
            passed = False
            method = "no_answer"
        elif tolerance != "0":
            passed = _within_tolerance(extracted, expected_output, tolerance)
            method = "numeric_match_within_tolerance"
        else:
            passed = _normalized_number(extracted) == _normalized_number(
                expected_output,
            )
            method = "numeric_match"
    elif kind == "letter_match":
        choices = str(configuration.get("choices") or "ABCD").upper()
        extracted = _letter_answer(actual_output)
        if extracted is not None and extracted not in choices:
            extracted = None
        passed = extracted is not None and (
            extracted == expected_output.strip().upper()
        )
        method = "letter_match" if extracted is not None else "no_answer"
    elif kind == "exact_match":
        case_sensitive = bool(configuration.get("case_sensitive", True))
        normalize_whitespace = bool(
            configuration.get("normalize_whitespace", False),
        )
        extracted = actual_output.strip()
        expected = expected_output.strip()
        compared_extracted = extracted
        if normalize_whitespace:
            compared_extracted = " ".join(compared_extracted.split())
            expected = " ".join(expected.split())
        if not case_sensitive:
            compared_extracted = compared_extracted.lower()
            expected = expected.lower()
        passed = compared_extracted == expected
        method = "exact_match"
    else:
        raise ValueError(f"Unsupported scorer kind: {kind or 'missing'}")

    return {
        "status": "scored",
        "score": 1.0 if passed else 0.0,
        "passed": passed,
        "extracted_output": extracted,
        "explanation": method,
        "configuration_checksum": scorer.get("configuration_checksum"),
        "evidence": {
            "expected_output": expected_output,
            "actual_output": actual_output,
            "method": method,
            "scorer_version": scorer.get("version"),
            "effective_configuration": configuration,
        },
    }
