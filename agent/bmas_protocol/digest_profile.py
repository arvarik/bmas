"""Foundation Stage 0C: the canonical bmas-digest profile.

The profile canonicalizes a JSON value with RFC 8785 and hashes the
result with SHA-256. Every digest input starts with the frame
``bmas:<domain>`` and one NUL byte, so two domains can never collide on
equal content.

Profile rules:

- Object keys sort by UTF-16 code units.
- Strings serialize with the ECMAScript JSON escape rules and stay in
  UTF-8 without Unicode normalization.
- The profile supports integer numbers inside the I-JSON safe range
  only. It rejects every other number, so each supported language
  produces identical bytes.
- Duplicate object keys and invalid Unicode are rejected.

The digest profile version is explicit metadata. Source identifiers
stay free of version tokens.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

DIGEST_PROFILE = "bmas-digest"
DIGEST_PROFILE_VERSION = "1"
DIGEST_ALGORITHM = "sha256"

# The I-JSON safe integer range: the largest range every supported
# language represents exactly.
MAX_SAFE_INTEGER = 2**53 - 1
MIN_SAFE_INTEGER = -(2**53 - 1)

_DOMAIN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789-."
)


class DigestInputError(ValueError):
    """The value cannot enter the canonical digest profile."""


@dataclass(frozen=True)
class DigestRecord:
    """One complete digest with its profile metadata."""

    profile: str
    profile_version: str
    domain: str
    algorithm: str
    value: str
    key_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "profile": self.profile,
            "profile_version": self.profile_version,
            "domain": self.domain,
            "algorithm": self.algorithm,
            "value": self.value,
        }
        if self.key_id is not None:
            record["key_id"] = self.key_id
        return record


def _canonical_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DigestInputError(
            "The digest profile rejects invalid Unicode"
        ) from exc
    # json.dumps applies the ECMAScript escape rules: the two-character
    # escapes, lowercase \u00xx for other control characters, and
    # literal unnormalized UTF-8 for everything else.
    return json.dumps(value, ensure_ascii=False)


def _canonical_number(value: int) -> str:
    if value > MAX_SAFE_INTEGER or value < MIN_SAFE_INTEGER:
        raise DigestInputError(
            "The digest profile rejects numbers outside the I-JSON "
            f"safe integer range: {value}"
        )
    return str(value)


def _sort_key(key: str) -> bytes:
    # RFC 8785 sorts keys by UTF-16 code units.
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise DigestInputError(
            "The digest profile rejects invalid Unicode"
        ) from exc


def canonicalize(value: Any) -> str:
    """Return the RFC 8785 canonical text of one JSON value."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return _canonical_number(value)
    if isinstance(value, float):
        raise DigestInputError(
            "The digest profile supports integer numbers only"
        )
    if isinstance(value, str):
        return _canonical_string(value)
    if isinstance(value, (list, tuple)):
        parts = ",".join(canonicalize(item) for item in value)
        return f"[{parts}]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise DigestInputError("Object keys must be strings")
        entries = ",".join(
            f"{_canonical_string(key)}:{canonicalize(value[key])}"
            for key in sorted(value, key=_sort_key)
        )
        return f"{{{entries}}}"
    raise DigestInputError(
        f"The digest profile rejects values of type {type(value).__name__}"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DigestInputError(f"Duplicate object key: {key!r}")
        result[key] = value
    return result


def parse_digest_input(text: str) -> Any:
    """Parse JSON text for the digest profile and reject duplicates."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_float_text,
            parse_constant=_reject_constant,
        )
    except DigestInputError:
        raise
    except ValueError as exc:
        raise DigestInputError(f"Invalid digest input: {exc}") from exc


def _reject_float_text(text: str) -> Any:
    raise DigestInputError(
        f"The digest profile supports integer numbers only: {text}"
    )


def _reject_constant(text: str) -> Any:
    raise DigestInputError(f"The digest profile rejects {text}")


def _validate_domain(domain: str) -> str:
    if not domain or any(
        character not in _DOMAIN_CHARACTERS for character in domain
    ):
        raise DigestInputError(f"Invalid digest domain: {domain!r}")
    return domain


def digest_input_bytes(domain: str, value: Any) -> bytes:
    """Return the framed digest input for one domain and value."""
    _validate_domain(domain)
    canonical = canonicalize(value).encode("utf-8")
    return b"bmas:" + domain.encode("ascii") + b"\x00" + canonical


def digest_value(
    domain: str, value: Any, *, key_id: str | None = None,
) -> DigestRecord:
    """Digest one JSON value under one domain."""
    framed = digest_input_bytes(domain, value)
    return DigestRecord(
        profile=DIGEST_PROFILE,
        profile_version=DIGEST_PROFILE_VERSION,
        domain=domain,
        algorithm=DIGEST_ALGORITHM,
        value=hashlib.sha256(framed).hexdigest(),
        key_id=key_id,
    )


def digest_hex(domain: str, value: Any) -> str:
    """Return the bare hexadecimal digest of one value."""
    return digest_value(domain, value).value


def digest_bytes(domain: str, payload: bytes) -> str:
    """Digest raw bytes, framed under one domain.

    Artifact content is raw bytes, not JSON, so the frame applies
    directly to the payload.
    """
    _validate_domain(domain)
    framed = b"bmas:" + domain.encode("ascii") + b"\x00" + payload
    return hashlib.sha256(framed).hexdigest()
