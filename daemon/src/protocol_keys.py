"""Durable signing keys for the agent protocol.

The daemon signs activation and effect grants with one Ed25519 key
that lives beside the database, so a restart keeps the same key and
every grant it issued stays verifiable. Agent public keys register
over the node-authenticated route and persist in the ``signing_keys``
table, so an acknowledgement or a receipt still verifies after a
daemon restart. A registered key identifier never changes its public
bytes. A new key identifier for the same agent records a rotation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

import database as db
from core.asset_store import ArtifactStore
from core.signing import KeyRegistry, SigningKeyRecord, public_bytes_of

DAEMON_KEY_ID = "daemon-grant-key"
DAEMON_KEY_PURPOSE = "daemon-grant"
AGENT_KEY_PURPOSE = "agent-receipt"
AUDIENCE = "bmas-agent"
KEY_NOT_BEFORE = "2000-01-01T00:00:00.000Z"
_daemon_key: Ed25519PrivateKey | None = None


class KeyRegistrationError(ValueError):
    """A key registration conflicts with a pinned key."""


def daemon_key_path() -> Path:
    configured = os.getenv("BMAS_DAEMON_SIGNING_KEY_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path(db.DB_PATH).parent / "daemon-signing-key"


def daemon_private_key() -> Ed25519PrivateKey:
    """Load the daemon grant key from disk or create it once."""
    global _daemon_key
    if _daemon_key is not None:
        return _daemon_key
    path = daemon_key_path()
    if path.is_file():
        seed = path.read_bytes()
        if len(seed) != 32:
            raise RuntimeError(f"The daemon signing key file {path} is not a 32-byte seed")
        _daemon_key = Ed25519PrivateKey.from_private_bytes(seed)
        return _daemon_key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(seed)
    _daemon_key = key
    return key


def reset_for_tests() -> None:
    global _daemon_key
    _daemon_key = None


def daemon_public_records() -> list[dict[str, Any]]:
    key = daemon_private_key()
    return [{
        "key_id": DAEMON_KEY_ID,
        "purpose": DAEMON_KEY_PURPOSE,
        "public_key_hex": public_bytes_of(key).hex(),
        "not_before": KEY_NOT_BEFORE,
    }]


async def register_agent_key(agent_id: str, key_id: str, public_key_hex: str) -> dict[str, Any]:
    """Pin one agent public key. A changed key under one identifier fails."""
    try:
        public = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise KeyRegistrationError("The public key is lowercase hex") from exc
    if len(public) != 32:
        raise KeyRegistrationError("An Ed25519 public key is 32 bytes")
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT agent_id, public_key_hex, revoked_at FROM signing_keys WHERE key_id = ?", (key_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            if str(row["public_key_hex"]) != public.hex() or str(row["agent_id"]) != agent_id:
                raise KeyRegistrationError(f"The key {key_id} is pinned to other bytes or another agent")
            return {"key_id": key_id, "agent_id": agent_id, "state": "revoked" if row["revoked_at"] else "registered", "new": False}
        now = await db._control_now(connection, None)  # noqa: SLF001
        await connection.execute(
            "INSERT INTO signing_keys (key_id, agent_id, purpose, public_key_hex, registered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key_id, agent_id, AGENT_KEY_PURPOSE, public.hex(), now),
        )
        await connection.commit()
    return {"key_id": key_id, "agent_id": agent_id, "state": "registered", "new": True}


async def revoke_agent_key(key_id: str) -> bool:
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, None)  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE signing_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL", (now, key_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def registry() -> KeyRegistry:
    """Build the live key registry: the daemon key and every agent key."""
    keys = KeyRegistry()
    keys.register(SigningKeyRecord(
        key_id=DAEMON_KEY_ID, owner_id="daemon", purpose=DAEMON_KEY_PURPOSE,
        public_bytes=public_bytes_of(daemon_private_key()), not_before=KEY_NOT_BEFORE,
    ))
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute("SELECT * FROM signing_keys ORDER BY registered_at, key_id")
        rows = await cursor.fetchall()
    for row in rows:
        keys.register(SigningKeyRecord(
            key_id=str(row["key_id"]), owner_id=str(row["agent_id"]), purpose=str(row["purpose"]),
            public_bytes=bytes.fromhex(str(row["public_key_hex"])), not_before=KEY_NOT_BEFORE,
            revoked_at=str(row["revoked_at"]) if row["revoked_at"] else None,
        ))
    return keys


async def registered_agent_keys(agent_id: str | None = None) -> list[dict[str, Any]]:
    async with db._connect() as connection:  # noqa: SLF001
        if agent_id is None:
            cursor = await connection.execute("SELECT * FROM signing_keys ORDER BY registered_at, key_id")
        else:
            cursor = await connection.execute(
                "SELECT * FROM signing_keys WHERE agent_id = ? ORDER BY registered_at, key_id", (agent_id,),
            )
        return [dict(row) for row in await cursor.fetchall()]


def artifact_store() -> ArtifactStore:
    """The protected artifact store for grants and observations."""
    root = Path(db.DB_PATH).parent / "agent-protocol-artifacts"
    return ArtifactStore(root, "tenant-default")
