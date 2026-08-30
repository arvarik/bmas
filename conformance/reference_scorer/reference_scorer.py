#!/usr/bin/env python3
"""The isolated deterministic reference scorer for Foundation conformance.

The scorer is one pure function from input bytes to output bytes. It
supports exactly two scoring modes:

1. ``exact_match`` compares an actual string with an expected string.
2. ``bounded_numeric`` maps an absolute numeric difference into the
   closed range zero to one under a positive tolerance.

Pinned contract:

- Executable format: one Python 3.13 module. It uses only the Python
  standard library. It runs through ``score_bytes`` in memory or as a
  command that reads one input and writes the result to stdout.
- Dependencies: none outside the standard library.
- Input schema: one UTF-8 JSON document, defined in the package README
  and enforced by ``validate_input_document``.
- Output schema: ``result.schema.json`` in this package, enforced by
  ``validate_result_document`` before any byte leaves the scorer.
- Resource limits: the module constants below.
- Locale: the output never depends on the process locale.
- Random source: none. The scorer uses no randomness.
- Clock source: none. The scorer never reads a clock.
- Failure codes: the module exit-code constants below.

All arithmetic uses decimal numbers under one pinned context, so the
same normalized input produces byte-identical output on every
supported host. The scorer rejects non-finite numbers and rejects its
own output when the output fails the result contract.

The scorer is not an evaluation authority. It performs no database,
network, artifact, runtime, or benchmark interface access, and it
writes no file.
"""

from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException
from hashlib import sha256

INPUT_SCHEMA_ID = "bmas.reference_scorer_input"
RESULT_SCHEMA_ID = "bmas.reference_scorer_result"
CONTRACT_VERSION = "1.0.0"
SCORER_KINDS = ("exact_match", "bounded_numeric")

# Resource limits. Every limit is part of the pinned contract.
MAX_INPUT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 8_388_608
MAX_CASES = 10_000
MAX_TEXT_CHARS = 65_536
MAX_CASE_ID_CHARS = 120
MAX_NUMBER_DIGITS = 34
MAX_NUMBER_EXPONENT = 50

# Failure codes. The command exits with these values.
EXIT_SUCCESS = 0
EXIT_INVALID_INPUT = 2
EXIT_INVALID_OUTPUT = 3
EXIT_RESOURCE_LIMIT = 4

# One pinned decimal context: 34 significant digits with banker's
# rounding, wide enough for every admitted input number.
DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
SCORE_QUANTUM = Decimal("0.000001")

_ZERO = Decimal(0)
_ONE = Decimal(1)


class ScorerError(Exception):
    """Base error with one pinned failure code."""

    exit_code = EXIT_INVALID_INPUT


class InvalidInputError(ScorerError):
    exit_code = EXIT_INVALID_INPUT


class InvalidOutputError(ScorerError):
    exit_code = EXIT_INVALID_OUTPUT


class ResourceLimitError(ScorerError):
    exit_code = EXIT_RESOURCE_LIMIT


# ── Parsing ────────────────────────────────────────────────────────


def _reject_duplicate_pairs(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise InvalidInputError(f"duplicate object key: {key!r}")
        document[key] = value
    return document


def _reject_constant(value: str):
    raise InvalidInputError(f"non-finite number: {value}")


def parse_input_bytes(input_bytes: bytes) -> dict:
    """Parse input bytes into a document with decimal numbers."""
    if not isinstance(input_bytes, bytes):
        raise InvalidInputError("the input must be bytes")
    if len(input_bytes) > MAX_INPUT_BYTES:
        raise ResourceLimitError(
            f"the input holds {len(input_bytes)} bytes; the limit is {MAX_INPUT_BYTES}"
        )
    try:
        text = input_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise InvalidInputError(f"the input is not valid UTF-8: {error}") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_constant,
        )
    except ScorerError:
        raise
    except ValueError as error:
        raise InvalidInputError(f"the input is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise InvalidInputError("the input document must be one JSON object")
    return document


def _admit_number(value, location: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise InvalidInputError(f"{location} must be a JSON number")
    if not value.is_finite():
        raise InvalidInputError(f"{location} is a non-finite number")
    digits = len(value.as_tuple().digits)
    if digits > MAX_NUMBER_DIGITS:
        raise ResourceLimitError(
            f"{location} holds {digits} digits; the limit is {MAX_NUMBER_DIGITS}"
        )
    if value != 0 and abs(value.adjusted()) > MAX_NUMBER_EXPONENT:
        raise ResourceLimitError(
            f"{location} exponent {value.adjusted()} exceeds the limit "
            f"{MAX_NUMBER_EXPONENT}"
        )
    return value


def _admit_text(value, location: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidInputError(f"{location} must be a JSON string")
    if len(value) > limit:
        raise ResourceLimitError(
            f"{location} holds {len(value)} characters; the limit is {limit}"
        )
    return value


def _require_keys(document: dict, location: str, keys: tuple[str, ...]) -> None:
    for key in keys:
        if key not in document:
            raise InvalidInputError(f"{location} lacks the required field {key!r}")
    for key in document:
        if key not in keys:
            raise InvalidInputError(f"{location} holds the unknown field {key!r}")


def validate_input_document(document: dict) -> None:
    """Enforce the pinned input schema on a parsed document."""
    _require_keys(document, "the input", ("schema_id", "metadata", "scorer", "cases"))
    if document["schema_id"] != INPUT_SCHEMA_ID:
        raise InvalidInputError(
            f"the input schema_id must be {INPUT_SCHEMA_ID!r}, "
            f"not {document['schema_id']!r}"
        )
    metadata = document["metadata"]
    if not isinstance(metadata, dict):
        raise InvalidInputError("the input metadata must be one JSON object")
    _require_keys(metadata, "the input metadata", ("contract_version",))
    if metadata["contract_version"] != CONTRACT_VERSION:
        raise InvalidInputError(
            f"the input contract_version must be {CONTRACT_VERSION!r}, "
            f"not {metadata['contract_version']!r}"
        )
    scorer = document["scorer"]
    if scorer not in SCORER_KINDS:
        raise InvalidInputError(f"unknown scorer: {scorer!r}")

    cases = document["cases"]
    if not isinstance(cases, list) or not cases:
        raise InvalidInputError("the input cases must be one nonempty JSON array")
    if len(cases) > MAX_CASES:
        raise ResourceLimitError(
            f"the input holds {len(cases)} cases; the limit is {MAX_CASES}"
        )

    seen_ids = set()
    for index, case in enumerate(cases):
        location = f"case {index}"
        if not isinstance(case, dict):
            raise InvalidInputError(f"{location} must be one JSON object")
        if scorer == "exact_match":
            _require_keys(case, location, ("case_id", "expected", "actual"))
            _admit_text(case["expected"], f"{location} expected", MAX_TEXT_CHARS)
            _admit_text(case["actual"], f"{location} actual", MAX_TEXT_CHARS)
        else:
            _require_keys(case, location, ("case_id", "expected", "actual", "tolerance"))
            _admit_number(case["expected"], f"{location} expected")
            _admit_number(case["actual"], f"{location} actual")
            tolerance = _admit_number(case["tolerance"], f"{location} tolerance")
            if tolerance <= 0:
                raise InvalidInputError(f"{location} tolerance must be greater than zero")
        case_id = _admit_text(case["case_id"], f"{location} case_id", MAX_CASE_ID_CHARS)
        if not case_id:
            raise InvalidInputError(f"{location} case_id must not be empty")
        if case_id in seen_ids:
            raise InvalidInputError(f"duplicate case_id: {case_id!r}")
        seen_ids.add(case_id)


# ── Normalization and encoding ─────────────────────────────────────


def canonical_number_form(value: Decimal) -> str:
    """Return one canonical text form for a numeric value."""
    if value == 0:
        return "0"
    return format(value.normalize(DECIMAL_CONTEXT), "f")


def _normalize_value(value):
    if isinstance(value, Decimal):
        return canonical_number_form(value)
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def normalized_input_bytes(document: dict) -> bytes:
    """Encode a validated document into its one normalized byte form.

    Numbers become canonical decimal text, keys sort, and the encoding
    is compact ASCII JSON. Two inputs with one meaning share one
    normalized form, so they share one output.
    """
    return canonical_encode(_normalize_value(document))


def canonical_encode(value) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def quantize_score(value: Decimal) -> str:
    return str(value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT))


# ── Scoring ────────────────────────────────────────────────────────


def score_exact_match(case: dict) -> Decimal:
    return _ONE if case["expected"] == case["actual"] else _ZERO


def score_bounded_numeric(case: dict) -> Decimal:
    difference = DECIMAL_CONTEXT.abs(
        DECIMAL_CONTEXT.subtract(case["actual"], case["expected"])
    )
    ratio = DECIMAL_CONTEXT.divide(difference, case["tolerance"])
    raw = DECIMAL_CONTEXT.subtract(_ONE, ratio)
    return min(_ONE, max(_ZERO, raw))


def build_result_document(document: dict) -> dict:
    scorer = document["scorer"]
    score_case = score_exact_match if scorer == "exact_match" else score_bounded_numeric

    case_results = []
    score_sum = _ZERO
    for case in document["cases"]:
        score = score_case(case)
        score_sum = DECIMAL_CONTEXT.add(score_sum, score)
        case_results.append({"case_id": case["case_id"], "score": quantize_score(score)})

    case_count = len(case_results)
    mean_score = DECIMAL_CONTEXT.divide(score_sum, Decimal(case_count))
    return {
        "schema_id": RESULT_SCHEMA_ID,
        "metadata": {"contract_version": CONTRACT_VERSION},
        "scorer": scorer,
        "input_sha256": sha256(normalized_input_bytes(document)).hexdigest(),
        "case_results": case_results,
        "aggregate": {
            "case_count": case_count,
            "score_sum": quantize_score(score_sum),
            "mean_score": quantize_score(mean_score),
        },
    }


# ── Output validation ──────────────────────────────────────────────

_SCORE_FORM = "one decimal string with exactly six fraction digits"


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidOutputError(f"invalid scorer output: {message}")


def _is_score_text(value) -> bool:
    if not isinstance(value, str) or "." not in value:
        return False
    whole, _, fraction = value.partition(".")
    return whole.isdigit() and len(fraction) == 6 and fraction.isdigit()


def validate_result_document(document: dict) -> None:
    """Enforce the result contract that result.schema.json declares."""
    _check(isinstance(document, dict), "the result must be one JSON object")
    _check(
        sorted(document) == sorted(
            ("schema_id", "metadata", "scorer", "input_sha256", "case_results", "aggregate")
        ),
        "the result holds a missing or unknown field",
    )
    _check(document["schema_id"] == RESULT_SCHEMA_ID, "wrong result schema_id")
    metadata = document["metadata"]
    _check(
        isinstance(metadata, dict) and list(metadata) == ["contract_version"],
        "the result metadata must hold only contract_version",
    )
    _check(metadata["contract_version"] == CONTRACT_VERSION, "wrong contract_version")
    _check(document["scorer"] in SCORER_KINDS, "unknown scorer in the result")
    digest = document["input_sha256"]
    _check(
        isinstance(digest, str)
        and len(digest) == 64
        and all(c in "0123456789abcdef" for c in digest),
        "input_sha256 must be one lowercase hexadecimal digest",
    )

    case_results = document["case_results"]
    _check(
        isinstance(case_results, list) and 0 < len(case_results) <= MAX_CASES,
        "case_results must be one bounded nonempty array",
    )
    seen_ids = set()
    for case in case_results:
        _check(isinstance(case, dict), "each case result must be one JSON object")
        _check(sorted(case) == ["case_id", "score"], "each case result holds case_id and score")
        _check(
            isinstance(case["case_id"], str) and 0 < len(case["case_id"]) <= MAX_CASE_ID_CHARS,
            "each case_id must be one bounded nonempty string",
        )
        _check(case["case_id"] not in seen_ids, "duplicate case_id in the result")
        seen_ids.add(case["case_id"])
        _check(_is_score_text(case["score"]), f"each score must be {_SCORE_FORM}")
        _check(_ZERO <= Decimal(case["score"]) <= _ONE, "each score must stay in [0, 1]")

    aggregate = document["aggregate"]
    _check(isinstance(aggregate, dict), "the aggregate must be one JSON object")
    _check(
        sorted(aggregate) == ["case_count", "mean_score", "score_sum"],
        "the aggregate holds case_count, mean_score, and score_sum",
    )
    _check(
        isinstance(aggregate["case_count"], int)
        and not isinstance(aggregate["case_count"], bool)
        and aggregate["case_count"] == len(case_results),
        "case_count must equal the number of case results",
    )
    _check(_is_score_text(aggregate["score_sum"]), f"score_sum must be {_SCORE_FORM}")
    _check(_is_score_text(aggregate["mean_score"]), f"mean_score must be {_SCORE_FORM}")
    _check(
        _ZERO <= Decimal(aggregate["mean_score"]) <= _ONE,
        "mean_score must stay in [0, 1]",
    )


# ── Public entry points ────────────────────────────────────────────


def score_bytes(input_bytes: bytes) -> bytes:
    """Score one input document and return the exact result bytes.

    This is the whole scorer: bytes in, bytes out, nothing else. The
    result bytes carry one trailing line feed.
    """
    document = parse_input_bytes(input_bytes)
    validate_input_document(document)
    try:
        result = build_result_document(document)
    except DecimalException as error:
        raise InvalidInputError(f"the input defeats decimal arithmetic: {error}") from error
    validate_result_document(result)
    output = canonical_encode(result) + b"\n"
    if len(output) > MAX_OUTPUT_BYTES:
        raise ResourceLimitError(
            f"the result holds {len(output)} bytes; the limit is {MAX_OUTPUT_BYTES}"
        )
    return output


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        sys.stderr.write("usage: reference_scorer.py [input-path]\n")
        return EXIT_INVALID_INPUT
    try:
        if argv:
            with open(argv[0], "rb") as handle:
                input_bytes = handle.read(MAX_INPUT_BYTES + 1)
        else:
            input_bytes = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except OSError as error:
        sys.stderr.write(f"error {EXIT_INVALID_INPUT}: cannot read the input: {error}\n")
        return EXIT_INVALID_INPUT
    try:
        output = score_bytes(input_bytes)
    except ScorerError as error:
        sys.stderr.write(f"error {error.exit_code}: {error}\n")
        return error.exit_code
    sys.stdout.buffer.write(output)
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
