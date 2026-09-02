"""The portable ``bmas-transform`` profile, implemented exactly.

The profile pins every rule a second implementation needs for equal
digests: strict UTF-8 with duplicate keys rejected, Unicode NFC for
transformation keys and generated strings, source order preserved
unless an operation declares a new order, the stable default order
over normalized case identifiers and source ordinals, missing values
distinct from JSON null, safe-integer and binary64 number rules with
negative zero normalized, RFC 8785 rendering with no locale
dependence, SHA-256 counter ranking for sampling and split
assignment, and RFC 8785 canonicalization of recipe inputs before
digest calculation. The profile and engine versions travel in recipe
metadata, never inside an identifier.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from typing import Any

PROFILE_NAME = "bmas-transform"
# The profile version is one unsigned 32-bit integer in the ranking
# input and one metadata value in every recipe.
PROFILE_VERSION = 1
ENGINE_VERSION = "1"

SAFE_INTEGER_LIMIT = 9007199254740991
SEED_LIMIT = 2**64 - 1

OPERATIONS = (
    "select",
    "rename",
    "filter",
    "map_template",
    "normalize",
    "deduplicate",
    "sample",
    "split",
    "attach_rubric",
)

_FILTER_OPERATORS = ("eq", "ne", "contains", "exists", "absent")
_NORMALIZE_FORMS = ("nfc", "trim", "collapse_whitespace", "lower")

_TEMPLATE_PATTERN = re.compile(r"\$\$\{|\$\{([A-Za-z0-9_-]+)\}")


class TransformProfileError(ValueError):
    """The value, recipe, or operation violates the profile."""


class MissingValue:
    """The one sentinel that keeps a missing value distinct from null."""

    _instance: MissingValue | None = None

    def __new__(cls) -> MissingValue:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"


MISSING = MissingValue()


def nfc(text: str) -> str:
    """Normalize one transformation string to Unicode NFC."""
    return unicodedata.normalize("NFC", text)


def strict_parse(payload: bytes) -> Any:
    """Parse strict UTF-8 JSON and reject duplicate object keys."""
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TransformProfileError(
            f"The input is not strict UTF-8: {error}"
        ) from error

    def guard_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise TransformProfileError(
                    f"Duplicate object key before construction: {key!r}"
                )
            result[key] = item
        return result

    try:
        return json.loads(text, object_pairs_hook=guard_pairs)
    except json.JSONDecodeError as error:
        raise TransformProfileError(
            f"The input is not valid JSON: {error}"
        ) from error


def _validated_number(value: float | int) -> float | int:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > SAFE_INTEGER_LIMIT:
            raise TransformProfileError(
                "Integers stay inside the safe range; represent "
                "higher-precision numeric content as strings"
            )
        return value
    if value != value or value in (float("inf"), float("-inf")):
        raise TransformProfileError(
            "Only finite binary64 numbers are representable"
        )
    # Negative zero normalizes to zero before hashing or comparison.
    if value == 0.0:
        return 0.0
    return value


def render_number(value: float | int) -> str:
    """Render one number with RFC 8785 rules, locale independent."""
    value = _validated_number(value)
    if isinstance(value, bool):
        raise TransformProfileError("A boolean is not a number")
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    digits_tuple = Decimal(repr(value)).normalize().as_tuple()
    digits = "".join(str(d) for d in digits_tuple.digits)
    exponent = int(digits_tuple.exponent) + len(digits)
    sign = "-" if digits_tuple.sign else ""
    length = len(digits)
    if length <= exponent <= 21:
        return sign + digits + "0" * (exponent - length)
    if 0 < exponent <= 21:
        return sign + digits[:exponent] + "." + digits[exponent:]
    if -6 < exponent <= 0:
        return sign + "0." + "0" * (-exponent) + digits
    mantissa = digits[0] + ("." + digits[1:] if length > 1 else "")
    exponent_sign = "+" if exponent - 1 >= 0 else "-"
    return f"{sign}{mantissa}e{exponent_sign}{abs(exponent - 1)}"


def _canonical_string(text: str) -> str:
    encoded = ['"']
    for character in text:
        code = ord(character)
        if character == '"':
            encoded.append('\\"')
        elif character == "\\":
            encoded.append("\\\\")
        elif code < 0x20:
            named = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}
            encoded.append(named.get(code, f"\\u{code:04x}"))
        else:
            encoded.append(character)
    encoded.append('"')
    return "".join(encoded)


def canonical_json(value: Any) -> str:
    """Serialize one value with the profile's RFC 8785 rules."""
    if value is MISSING:
        raise TransformProfileError(
            "A missing value never serializes; it is not JSON null"
        )
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return render_number(value)
    if isinstance(value, str):
        return _canonical_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = sorted(
            ((str(key), item) for key, item in value.items()),
            key=lambda entry: entry[0].encode("utf-16-be"),
        )
        return "{" + ",".join(
            f"{_canonical_string(key)}:{canonical_json(item)}"
            for key, item in entries
        ) + "}"
    raise TransformProfileError(
        f"The profile does not represent {type(value).__name__}"
    )


def case_digest_bytes(case: dict[str, Any]) -> bytes:
    """Digest one case's canonical bytes for ranking and manifests."""
    return hashlib.sha256(canonical_json(case).encode("utf-8")).digest()


def case_digest(case: dict[str, Any]) -> str:
    return case_digest_bytes(case).hex()


def recipe_digest(recipe: dict[str, Any]) -> str:
    """Digest one recipe after RFC 8785 canonicalization."""
    return hashlib.sha256(
        canonical_json(recipe).encode("utf-8"),
    ).hexdigest()


def dataset_digest(cases: list[dict[str, Any]]) -> str:
    """Digest the ordered case digests of one dataset."""
    joined = b"".join(case_digest_bytes(case) for case in cases)
    return hashlib.sha256(joined).hexdigest()


def rank_bytes(
    *,
    seed: int,
    operation_index: int,
    case_digest_value: bytes,
    counter: int,
    profile_version: int = PROFILE_VERSION,
) -> bytes:
    """Compute one SHA-256 counter rank with the exact pinned input."""
    if not 0 <= seed <= SEED_LIMIT:
        raise TransformProfileError(
            "The seed fits one unsigned 64-bit integer"
        )
    if len(case_digest_value) != 32:
        raise TransformProfileError("A case digest holds 32 bytes")
    payload = (
        PROFILE_NAME.encode("utf-8") + b"\x00"
        + profile_version.to_bytes(4, "big")
        + seed.to_bytes(8, "big")
        + operation_index.to_bytes(4, "big")
        + case_digest_value
        + counter.to_bytes(4, "big")
    )
    # Ranks compare as unsigned 256-bit big-endian integers, which
    # equals lexicographic comparison of the digest bytes.
    return hashlib.sha256(payload).digest()


def resolve_pointer(value: Any, pointer: str) -> Any:
    """Resolve one JSON Pointer, returning MISSING for absent paths."""
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise TransformProfileError(
            f"A binding pointer starts with '/': {pointer!r}"
        )
    current = value
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return MISSING
            current = current[int(token)]
        else:
            return MISSING
    return current


def render_template(
    template: str,
    bindings: dict[str, dict[str, Any]],
    case: dict[str, Any],
) -> str:
    """Render one template with named bindings and literal escapes.

    A scalar uses its locale-independent text form; an object or an
    array uses RFC 8785 JSON. A missing value fails the template
    unless the binding supplies a default.
    """
    output: list[str] = []
    position = 0
    for match in _TEMPLATE_PATTERN.finditer(template):
        output.append(template[position:match.start()])
        position = match.end()
        if match.group(0) == "$${":
            output.append("${")
            continue
        name = match.group(1)
        binding = bindings.get(name)
        if binding is None:
            raise TransformProfileError(
                f"The template references an unbound name: {name!r}"
            )
        value = resolve_pointer(case, str(binding.get("pointer", "")))
        if value is MISSING:
            if "default" not in binding:
                raise TransformProfileError(
                    f"The binding {name!r} resolved no value and "
                    "supplies no default"
                )
            value = binding["default"]
        if isinstance(value, str):
            output.append(nfc(value))
        elif value is None:
            output.append("null")
        elif isinstance(value, bool):
            output.append("true" if value else "false")
        elif isinstance(value, (int, float)):
            output.append(render_number(value))
        else:
            output.append(canonical_json(value))
    output.append(template[position:])
    return nfc("".join(output))


def _stable_key(entry: tuple[int, dict[str, Any]]) -> tuple[bytes, int]:
    ordinal, case = entry
    identity = nfc(str(case.get("case_id") or case.get("item_key") or ""))
    return (identity.encode("utf-8"), ordinal)


def _field_value(case: dict[str, Any], field: str) -> Any:
    if field.startswith("/"):
        return resolve_pointer(case, field)
    return case.get(nfc(field), MISSING)


def _normalized_comparable(value: Any) -> str:
    if value is MISSING:
        return "m:missing"
    if isinstance(value, str):
        return "s:" + nfc(value)
    return "j:" + canonical_json(value)


# ── Operations ───────────────────────────────────────────────────────


def _apply_select(cases: list[dict], parameters: dict) -> list[dict]:
    fields = [nfc(str(name)) for name in parameters.get("fields") or []]
    if not fields:
        raise TransformProfileError("select names at least one field")
    return [
        {name: case[name] for name in fields if name in case}
        for case in cases
    ]


def _apply_rename(cases: list[dict], parameters: dict) -> list[dict]:
    mapping = {
        nfc(str(source)): nfc(str(target))
        for source, target in (parameters.get("mapping") or {}).items()
    }
    if not mapping:
        raise TransformProfileError("rename names at least one mapping")
    renamed = []
    for case in cases:
        result = {}
        for key, item in case.items():
            result[mapping.get(nfc(str(key)), nfc(str(key)))] = item
        renamed.append(result)
    return renamed


def _apply_filter(cases: list[dict], parameters: dict) -> list[dict]:
    field = str(parameters.get("field") or "")
    operator = str(parameters.get("operator") or "")
    if operator not in _FILTER_OPERATORS:
        raise TransformProfileError(
            f"Unknown filter operator: {operator!r}"
        )
    expected = parameters.get("value")
    kept = []
    for case in cases:
        value = _field_value(case, field)
        if operator == "exists":
            keep = value is not MISSING
        elif operator == "absent":
            keep = value is MISSING
        elif value is MISSING:
            keep = False
        elif operator == "eq":
            keep = _normalized_comparable(value) == (
                _normalized_comparable(expected)
            )
        elif operator == "ne":
            keep = _normalized_comparable(value) != (
                _normalized_comparable(expected)
            )
        else:
            keep = isinstance(value, str) and isinstance(expected, str) \
                and nfc(expected) in nfc(value)
        if keep:
            kept.append(case)
    return kept


def _apply_map_template(cases: list[dict], parameters: dict) -> list[dict]:
    target = nfc(str(parameters.get("target") or ""))
    template = str(parameters.get("template") or "")
    bindings = dict(parameters.get("bindings") or {})
    if not target:
        raise TransformProfileError("map_template names its target field")
    mapped = []
    for case in cases:
        result = dict(case)
        result[target] = render_template(template, bindings, case)
        mapped.append(result)
    return mapped


def _apply_normalize(cases: list[dict], parameters: dict) -> list[dict]:
    fields = [nfc(str(name)) for name in parameters.get("fields") or []]
    forms = list(parameters.get("forms") or ["nfc"])
    for form in forms:
        if form not in _NORMALIZE_FORMS:
            raise TransformProfileError(f"Unknown normalize form: {form!r}")
    normalized = []
    for case in cases:
        result = dict(case)
        for name in fields:
            value = result.get(name)
            if not isinstance(value, str):
                continue
            for form in forms:
                if form == "nfc":
                    value = nfc(value)
                elif form == "trim":
                    value = value.strip()
                elif form == "collapse_whitespace":
                    value = " ".join(value.split())
                elif form == "lower":
                    value = value.lower()
            result[name] = value
        normalized.append(result)
    return normalized


def _apply_deduplicate(cases: list[dict], parameters: dict) -> list[dict]:
    fields = [nfc(str(name)) for name in parameters.get("fields") or []]
    if not fields:
        raise TransformProfileError(
            "deduplicate names at least one field"
        )
    seen: set[str] = set()
    kept = []
    for case in cases:
        key = canonical_json([
            _normalized_comparable(_field_value(case, name))
            for name in fields
        ])
        if key in seen:
            continue
        seen.add(key)
        kept.append(case)
    return kept


def _ranked(
    cases: list[dict],
    *,
    seed: int,
    operation_index: int,
) -> list[tuple[bytes, tuple[bytes, int], dict]]:
    entries = []
    for ordinal, case in enumerate(cases):
        rank = rank_bytes(
            seed=seed,
            operation_index=operation_index,
            case_digest_value=case_digest_bytes(case),
            counter=0,
        )
        entries.append((rank, _stable_key((ordinal, case)), case))
    return entries


def _apply_sample(
    cases: list[dict], parameters: dict, *, seed: int, operation_index: int,
) -> list[dict]:
    count = parameters.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise TransformProfileError(
            "sample requires one nonnegative integer count"
        )
    ranked = _ranked(cases, seed=seed, operation_index=operation_index)
    # The lowest ranks select without replacement; equal ranks resolve
    # with the stable default order. The selection keeps source order.
    chosen = sorted(ranked, key=lambda entry: (entry[0], entry[1]))
    selected = {id(entry[2]) for entry in chosen[:count]}
    return [case for case in cases if id(case) in selected]


def _apply_split(
    cases: list[dict], parameters: dict, *, seed: int, operation_index: int,
) -> list[dict]:
    weights = parameters.get("weights") or {}
    target = nfc(str(parameters.get("target") or "split"))
    names: list[str] = []
    values: list[int] = []
    for name in sorted(weights, key=lambda key: str(key).encode("utf-16-be")):
        weight = weights[name]
        if not isinstance(weight, int) or isinstance(weight, bool) \
                or weight <= 0:
            raise TransformProfileError(
                "split weights are positive integers"
            )
        names.append(nfc(str(name)))
        values.append(weight)
    if not names:
        raise TransformProfileError("split names at least one weight")
    total = sum(values)
    boundaries: list[tuple[int, str]] = []
    running = 0
    for name, weight in zip(names, values, strict=True):
        running += weight
        boundaries.append((running, name))
    result = []
    for case in cases:
        rank = rank_bytes(
            seed=seed,
            operation_index=operation_index,
            case_digest_value=case_digest_bytes(case),
            counter=0,
        )
        remainder = int.from_bytes(rank, "big") % total
        assigned = next(
            name for boundary, name in boundaries if remainder < boundary
        )
        updated = dict(case)
        updated[target] = assigned
        result.append(updated)
    return result


def _apply_attach_rubric(cases: list[dict], parameters: dict) -> list[dict]:
    rubric_id = str(parameters.get("rubric_id") or "")
    if not rubric_id:
        raise TransformProfileError("attach_rubric names one rubric")
    attached = []
    for case in cases:
        updated = dict(case)
        updated["rubric_id"] = rubric_id
        attached.append(updated)
    return attached


def validate_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    """Validate one recipe envelope and its metadata."""
    if not isinstance(recipe, dict):
        raise TransformProfileError("A recipe is one JSON object")
    if recipe.get("profile") != PROFILE_NAME:
        raise TransformProfileError(
            f"The recipe declares the {PROFILE_NAME} profile"
        )
    if recipe.get("profile_version") != PROFILE_VERSION:
        raise TransformProfileError(
            "The recipe metadata pins the supported profile version"
        )
    seed = recipe.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool) or not (
        0 <= seed <= SEED_LIMIT
    ):
        raise TransformProfileError(
            "The seed fits one unsigned 64-bit integer"
        )
    operations = recipe.get("operations")
    if not isinstance(operations, list) or not operations:
        raise TransformProfileError(
            "A recipe lists at least one operation"
        )
    for operation in operations:
        if not isinstance(operation, dict) or (
            operation.get("operation") not in OPERATIONS
        ):
            raise TransformProfileError(
                f"Unknown operation: {operation!r}"
            )
    return recipe


def apply_recipe(
    cases: list[dict[str, Any]], recipe: dict[str, Any],
) -> dict[str, Any]:
    """Apply one recipe deterministically and report every digest."""
    validate_recipe(recipe)
    seed = int(recipe.get("seed", 0))
    current = [dict(case) for case in cases]
    for index, operation in enumerate(recipe["operations"]):
        name = str(operation["operation"])
        parameters = dict(operation.get("parameters") or {})
        if name == "select":
            current = _apply_select(current, parameters)
        elif name == "rename":
            current = _apply_rename(current, parameters)
        elif name == "filter":
            current = _apply_filter(current, parameters)
        elif name == "map_template":
            current = _apply_map_template(current, parameters)
        elif name == "normalize":
            current = _apply_normalize(current, parameters)
        elif name == "deduplicate":
            current = _apply_deduplicate(current, parameters)
        elif name == "sample":
            current = _apply_sample(
                current, parameters, seed=seed, operation_index=index,
            )
        elif name == "split":
            current = _apply_split(
                current, parameters, seed=seed, operation_index=index,
            )
        elif name == "attach_rubric":
            current = _apply_attach_rubric(current, parameters)
    return {
        "cases": current,
        "case_digests": [case_digest(case) for case in current],
        "dataset_digest": dataset_digest(current),
        "recipe_digest": recipe_digest(recipe),
        "engine": {
            "profile": PROFILE_NAME,
            "profile_version": PROFILE_VERSION,
            "engine_version": ENGINE_VERSION,
        },
    }
