"""Foundation canonical Ed25519 signing for grants and receipts.

Every signed contract signs the RFC 8785 canonical object without its
signature field. The canonical bytes carry one signature-domain
prefix, so two contract types can never verify against each other.
Signatures use Ed25519 and encode as unpadded base64url under the
registered ``ed25519-jcs`` algorithm identifier.

The key registry holds daemon and agent verification keys. Rotation
registers a new key while the old key stays valid for its overlap
window. Revocation denies every new authority immediately and keeps
historical verification of already stored bytes unchanged.
"""
from __future__ import annotations

import base64
import dataclasses
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .digest_profile import canonicalize

SIGNATURE_ALGORITHM = "ed25519-jcs"

ACTIVATION_GRANT_DOMAIN = "bmas.activation-grant"
ACTIVATION_ACKNOWLEDGEMENT_DOMAIN = "bmas.activation-acknowledgement"
EFFECT_GRANT_DOMAIN = "bmas.effect-grant"
ATTEMPT_RECEIPT_DOMAIN = "bmas.attempt-receipt"

SIGNATURE_DOMAINS = (
    ACTIVATION_GRANT_DOMAIN,
    ACTIVATION_ACKNOWLEDGEMENT_DOMAIN,
    EFFECT_GRANT_DOMAIN,
    ATTEMPT_RECEIPT_DOMAIN,
)

KEY_PURPOSES = ("daemon-grant", "agent-receipt")


class SigningError(ValueError):
    """A signing or verification rule failed closed."""


class UnknownKeyError(SigningError):
    """The key identifier is not registered."""


class KeyNotValidError(SigningError):
    """The key cannot authorize a new signature at this time."""


class SignatureMismatchError(SigningError):
    """The signature does not verify against the canonical bytes."""


def signing_input(domain: str, payload: dict[str, Any]) -> bytes:
    """Return the domain-framed canonical bytes of one payload.

    The payload must not contain its own signature field.
    """
    if domain not in SIGNATURE_DOMAINS:
        raise SigningError(f"Unknown signature domain: {domain!r}")
    if "signature" in payload:
        raise SigningError(
            "The canonical signing object excludes its signature field"
        )
    canonical = canonicalize(payload).encode("utf-8")
    return domain.encode("ascii") + b"\x00" + canonical


def encode_signature(raw: bytes) -> str:
    """Encode one Ed25519 signature as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_signature(text: str) -> bytes:
    """Decode one unpadded base64url signature."""
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise SignatureMismatchError("Invalid signature encoding") from exc


@dataclass(frozen=True)
class SigningKeyRecord:
    """One registered verification key with its validity window.

    Each key scopes to one environment, tenant boundary, principal,
    and purpose. Rotation links a predecessor to its successor.
    """

    key_id: str
    owner_id: str
    purpose: str
    public_bytes: bytes
    not_before: str
    not_after: str | None = None
    revoked_at: str | None = None
    environment: str = "default"
    tenant_id: str = "tenant-default"
    predecessor_key_id: str | None = None
    successor_key_id: str | None = None

    def valid_for_new_authority(self, at: str) -> bool:
        """Report whether the key can authorize new work at one time."""
        if self.revoked_at is not None and at >= self.revoked_at:
            return False
        if at < self.not_before:
            return False
        return not (self.not_after is not None and at >= self.not_after)


class KeyRegistry:
    """Registered daemon and agent Ed25519 verification keys."""

    def __init__(self) -> None:
        self._keys: dict[str, SigningKeyRecord] = {}

    def register(self, record: SigningKeyRecord) -> None:
        if record.purpose not in KEY_PURPOSES:
            raise SigningError(f"Unknown key purpose: {record.purpose!r}")
        if record.key_id in self._keys:
            raise SigningError(f"Duplicate key identifier: {record.key_id!r}")
        self._keys[record.key_id] = record

    def revoke(self, key_id: str, at: str) -> None:
        """Revoke one key. Historical verification stays unchanged."""
        record = self.require(key_id)
        self._keys[key_id] = SigningKeyRecord(
            key_id=record.key_id,
            owner_id=record.owner_id,
            purpose=record.purpose,
            public_bytes=record.public_bytes,
            not_before=record.not_before,
            not_after=record.not_after,
            revoked_at=at,
        )

    def require(self, key_id: str) -> SigningKeyRecord:
        record = self._keys.get(key_id)
        if record is None:
            raise UnknownKeyError(f"Unknown key identifier: {key_id!r}")
        return record

    def require_new_authority(
        self, key_id: str, *, owner_id: str, purpose: str, at: str,
    ) -> SigningKeyRecord:
        """Validate one key for a new signature or fail closed."""
        record = self.require(key_id)
        if record.owner_id != owner_id:
            raise KeyNotValidError(
                f"The key {key_id!r} does not belong to {owner_id!r}"
            )
        if record.purpose != purpose:
            raise KeyNotValidError(
                f"The key {key_id!r} has the wrong purpose"
            )
        if not record.valid_for_new_authority(at):
            raise KeyNotValidError(
                f"The key {key_id!r} is expired, revoked, or not yet valid"
            )
        return record

    def active_key_ids(self, *, owner_id: str, purpose: str, at: str) -> list[str]:
        """List every key that can authorize new work for one owner."""
        return [
            record.key_id
            for record in self._keys.values()
            if record.owner_id == owner_id
            and record.purpose == purpose
            and record.valid_for_new_authority(at)
        ]


def sign_payload(
    private_key: Ed25519PrivateKey, domain: str, payload: dict[str, Any],
) -> str:
    """Sign the canonical bytes of one payload under one domain."""
    return encode_signature(private_key.sign(signing_input(domain, payload)))


def verify_payload(
    public_bytes: bytes,
    domain: str,
    payload: dict[str, Any],
    signature: str,
) -> None:
    """Verify one signature over the canonical bytes or fail closed."""
    public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
    try:
        public_key.verify(
            decode_signature(signature), signing_input(domain, payload),
        )
    except InvalidSignature as exc:
        raise SignatureMismatchError(
            f"The signature does not verify under {domain}"
        ) from exc


def public_bytes_of(private_key: Ed25519PrivateKey) -> bytes:
    """Return the raw public bytes of one private key."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    return private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw,
    )


def backup_registry(registry: KeyRegistry, backup_key: bytes) -> bytes:
    """Serialize and encrypt the key registry under the backup key.

    The backup key is separate from every signing key. The encrypted
    payload holds public verification material and metadata only.
    """
    import json

    from cryptography.fernet import Fernet

    records = []
    for record in registry._keys.values():  # noqa: SLF001
        entry = dataclasses.asdict(record)
        entry["public_bytes"] = base64.urlsafe_b64encode(
            record.public_bytes,
        ).decode("ascii")
        records.append(entry)
    payload = json.dumps(records, sort_keys=True).encode("utf-8")
    return Fernet(backup_key).encrypt(payload)


def restore_registry(encrypted: bytes, backup_key: bytes) -> KeyRegistry:
    """Decrypt and rebuild the key registry from one backup."""
    import json

    from cryptography.fernet import Fernet, InvalidToken

    try:
        payload = Fernet(backup_key).decrypt(encrypted)
    except InvalidToken as exc:
        raise SigningError(
            "The backup does not decrypt under this backup key"
        ) from exc
    registry = KeyRegistry()
    for entry in json.loads(payload):
        entry["public_bytes"] = base64.urlsafe_b64decode(
            entry["public_bytes"],
        )
        registry._keys[entry["key_id"]] = SigningKeyRecord(**entry)  # noqa: SLF001
    return registry
