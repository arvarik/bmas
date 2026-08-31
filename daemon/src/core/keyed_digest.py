"""Foundation keyed lookup digests and the semantic text transform.

The ``bmas-digest`` profile from the digest utility stays the one
shared content digest. This module adds two separate values:

1. One semantic canonical text value for text comparison. It applies
   NFC and LF line endings and preserves all other whitespace. It
   never replaces the exact content digest.
2. One keyed HMAC-SHA-256 digest for equality checks on sensitive
   values. A plain hash of a sensitive value can still disclose
   information, so equality lookups use one per-tenant key and record
   its key identifier. A keyed digest never appears in a public
   export.

Key rotation keeps the previous epoch queryable, so an equality
lookup checks the active and the previous key epochs.
"""
from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass

from core.digest_profile import (
    DIGEST_PROFILE,
    DIGEST_PROFILE_VERSION,
    DigestInputError,
)

KEYED_DIGEST_ALGORITHM = "hmac-sha256"
KEYED_DIGEST_DOMAIN_PREFIX = "bmas:"


class KeyedDigestError(ValueError):
    """One keyed digest rule failed closed."""


def semantic_text(value: str) -> str:
    """Return the separate canonical text value for semantic comparison.

    The transform applies NFC normalization and LF line endings. It
    preserves every other whitespace character. The result supports
    semantic comparison only; the exact content digest still uses the
    exact validated UTF-8 bytes.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DigestInputError(
            "The semantic text transform rejects invalid Unicode"
        ) from exc
    normalized = unicodedata.normalize("NFC", value)
    return normalized.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class TenantKeyEpoch:
    """One tenant HMAC key epoch."""

    tenant_id: str
    key_id: str
    key_bytes: bytes
    epoch: int


@dataclass(frozen=True)
class KeyedDigestRecord:
    """One keyed equality digest with its complete metadata."""

    profile: str
    profile_version: str
    domain: str
    algorithm: str
    value: str
    key_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "profile_version": self.profile_version,
            "domain": self.domain,
            "algorithm": self.algorithm,
            "value": self.value,
            "key_id": self.key_id,
        }


class TenantKeyRing:
    """Per-tenant HMAC keys with active and previous epochs."""

    def __init__(self) -> None:
        self._epochs: dict[str, list[TenantKeyEpoch]] = {}

    def install_key(
        self, tenant_id: str, key_id: str, key_bytes: bytes,
    ) -> TenantKeyEpoch:
        """Install one new active key epoch for a tenant."""
        if len(key_bytes) < 32:
            raise KeyedDigestError(
                "A tenant HMAC key uses at least 32 bytes"
            )
        history = self._epochs.setdefault(tenant_id, [])
        epoch = TenantKeyEpoch(
            tenant_id=tenant_id,
            key_id=key_id,
            key_bytes=key_bytes,
            epoch=len(history) + 1,
        )
        history.append(epoch)
        return epoch

    def active_epoch(self, tenant_id: str) -> TenantKeyEpoch:
        history = self._epochs.get(tenant_id)
        if not history:
            raise KeyedDigestError(
                f"No HMAC key exists for tenant {tenant_id!r}"
            )
        return history[-1]

    def lookup_epochs(self, tenant_id: str) -> list[TenantKeyEpoch]:
        """Return the active and previous epochs for rotation lookup."""
        history = self._epochs.get(tenant_id)
        if not history:
            raise KeyedDigestError(
                f"No HMAC key exists for tenant {tenant_id!r}"
            )
        return list(history[-2:])


def _framed(domain: str, payload: bytes) -> bytes:
    if not domain:
        raise KeyedDigestError("A keyed digest names its domain")
    return (
        KEYED_DIGEST_DOMAIN_PREFIX.encode("ascii")
        + domain.encode("ascii")
        + b"\x00"
        + payload
    )


def keyed_digest(
    ring: TenantKeyRing,
    tenant_id: str,
    domain: str,
    value: str,
) -> KeyedDigestRecord:
    """Digest one sensitive value under the tenant's active key."""
    epoch = ring.active_epoch(tenant_id)
    mac = hmac.new(
        epoch.key_bytes,
        _framed(domain, value.encode("utf-8")),
        hashlib.sha256,
    )
    return KeyedDigestRecord(
        profile=DIGEST_PROFILE,
        profile_version=DIGEST_PROFILE_VERSION,
        domain=domain,
        algorithm=KEYED_DIGEST_ALGORITHM,
        value=mac.hexdigest(),
        key_id=epoch.key_id,
    )


def keyed_lookup_values(
    ring: TenantKeyRing,
    tenant_id: str,
    domain: str,
    value: str,
) -> list[KeyedDigestRecord]:
    """Digest one value under the active and previous key epochs.

    During rotation, an equality lookup queries both epochs, so a
    record written under the previous key still matches.
    """
    records = []
    for epoch in ring.lookup_epochs(tenant_id):
        mac = hmac.new(
            epoch.key_bytes,
            _framed(domain, value.encode("utf-8")),
            hashlib.sha256,
        )
        records.append(
            KeyedDigestRecord(
                profile=DIGEST_PROFILE,
                profile_version=DIGEST_PROFILE_VERSION,
                domain=domain,
                algorithm=KEYED_DIGEST_ALGORITHM,
                value=mac.hexdigest(),
                key_id=epoch.key_id,
            ),
        )
    return records


def export_digest_for_public_view(record: dict[str, str]) -> dict[str, str]:
    """Strip keyed digests from one public export record.

    A keyed digest supports internal equality only. A public export
    keeps unkeyed content digests and drops every keyed value.
    """
    if record.get("algorithm") == KEYED_DIGEST_ALGORITHM or (
        record.get("key_id")
    ):
        return {
            "profile": record.get("profile", DIGEST_PROFILE),
            "domain": record.get("domain", ""),
            "redacted": "keyed_digest_withheld",
        }
    return dict(record)


_REQUIRED_AUDIT_FIELDS = ("profile", "profile_version", "domain",
                          "algorithm", "value")


def audit_digest_record(record: dict[str, str]) -> None:
    """Audit one persisted digest record or fail closed.

    Every shared digest declares its profile, domain, algorithm,
    value, and optional key identifier. A keyed algorithm requires the
    key identifier; an unkeyed algorithm forbids it.
    """
    missing = [name for name in _REQUIRED_AUDIT_FIELDS if not record.get(name)]
    if missing:
        raise KeyedDigestError(f"The digest record misses {missing}")
    if record["profile"] != DIGEST_PROFILE:
        raise KeyedDigestError(
            f"Unknown digest profile: {record['profile']!r}"
        )
    if record["algorithm"] not in ("sha256", KEYED_DIGEST_ALGORITHM):
        raise KeyedDigestError(
            f"Unknown digest algorithm: {record['algorithm']!r}"
        )
    if record["algorithm"] == KEYED_DIGEST_ALGORITHM:
        if not record.get("key_id"):
            raise KeyedDigestError(
                "A keyed digest records its key identifier"
            )
    elif record.get("key_id"):
        raise KeyedDigestError(
            "An unkeyed digest carries no key identifier"
        )
