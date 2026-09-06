"""The native bMAS agent protocol inside the agent process.

The daemon delivers one signed activation grant with the work request.
The agent verifies the grant under the daemon key, signs one exact
acknowledgement under its own key, stores the grant and the
acknowledgement durably before it posts the acknowledgement to the
daemon, and only then executes. A repeated delivery of the same grant
returns the stored acknowledgement and the stored result, across a
restart, without a second execution. Every nested model call runs
under one daemon-issued effect grant and returns signed attempt
receipts, so the daemon observes each provider call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .digest_profile import canonicalize, digest_hex
from .signing import (
    ACTIVATION_ACKNOWLEDGEMENT_DOMAIN,
    ACTIVATION_GRANT_DOMAIN,
    ATTEMPT_RECEIPT_DOMAIN,
    EFFECT_GRANT_DOMAIN,
    SIGNATURE_ALGORITHM,
    SigningError,
    public_bytes_of,
    sign_payload,
    verify_payload,
)

logger = logging.getLogger("bmas.agent.native")

PROTOCOL_VERSION = "2"
RECEIPT_VERSION = "1"
SCHEMA_VERSION = "1"
AUDIENCE = "bmas-agent"
CAPABILITY_DOCUMENT_DIGEST_DOMAIN = "agent-capability-document"
REQUEST_DIGEST_DOMAIN = "agent-request"
STAGE_TRANSPORT_STARTING = "transport_starting"
STAGE_RESPONSE_OBSERVED = "response_observed"
STAGE_FAILED_BEFORE_TRANSPORT = "failed_before_transport"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class NativeProtocolError(RuntimeError):
    """The native protocol cannot continue."""


class GrantRejectedError(NativeProtocolError):
    """The activation grant fails verification."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def utc_now() -> str:
    """Return the current time in the daemon's millisecond ISO form."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def canonical_bytes(record: dict[str, Any]) -> bytes:
    return canonicalize(record).encode("utf-8")


@dataclass(frozen=True)
class AgentKeys:
    key_id: str
    private_key: Ed25519PrivateKey

    @property
    def public_hex(self) -> str:
        return public_bytes_of(self.private_key).hex()


def load_or_create_agent_key(path: Path, key_id: str) -> AgentKeys:
    """Load the agent seed from disk or create one with owner-only access."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_file():
        seed = path.read_bytes()
        if len(seed) != 32:
            raise NativeProtocolError(f"The agent key file {path} is not a 32-byte seed")
        return AgentKeys(key_id, Ed25519PrivateKey.from_private_bytes(seed))
    private_key = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    seed = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(seed)
    return AgentKeys(key_id, private_key)


def capability_document(agent_id: str, key_id: str) -> dict[str, Any]:
    """The agent's own capability document. It proxies no provider document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "agent_id": agent_id,
        "supported_protocol_versions": [PROTOCOL_VERSION],
        "supported_receipt_versions": [RECEIPT_VERSION],
        "supported_activation_schemas": ["1"],
        "supported_dispatch_schemas": ["1"],
        "supported_acknowledgement_schemas": ["1"],
        "supported_proposal_schemas": ["1"],
        "supported_envelope_schemas": ["1"],
        "nested_model_receipts": True,
        # The starter execution path calls no tool. A tool call, when a
        # backend adds one, runs under the same effect-grant flow.
        "nested_tool_receipts": True,
        "structured_output": True,
        "usage_reporting": True,
        "streaming": False,
        "cancellation": True,
        "resume": True,
        "durable_grant_deduplication": True,
        "acknowledgement_status_lookup": True,
        "receipt_key_ids": [key_id],
        "max_request_bytes": 4 * 1024 * 1024,
        "max_response_bytes": 4 * 1024 * 1024,
        "max_artifact_bytes": 16 * 1024 * 1024,
    }


class GrantStore:
    """Durable grant records under the activation cache directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, grant_id: str) -> Path:
        name = hashlib.sha256(grant_id.encode("utf-8")).hexdigest()
        return self.root / f"{name}.json"

    def load(self, grant_id: str) -> dict[str, Any] | None:
        path = self.path(grant_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, grant_id: str, record: dict[str, Any]) -> None:
        path = self.path(grant_id)
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=".grant-", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


class DaemonClient:
    """The agent's authenticated client for the daemon protocol routes."""

    def __init__(self, base_url: str, *, node_key: str, node_id: str, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.node_key = node_key
        self.node_id = node_id
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    def headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"X-Node-Id": self.node_id}
        if self.node_key:
            headers["Authorization"] = f"Bearer {self.node_key}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(f"{self.base_url}{path}", json=payload, headers=self.headers())
        return self._result(response, path)

    async def _post_bytes(self, path: str, payload: bytes) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}{path}", content=payload, headers=self.headers("application/json"),
        )
        return self._result(response, path)

    @staticmethod
    def _result(response: httpx.Response, path: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise NativeProtocolError(f"The daemon rejected {path}: HTTP {response.status_code} {response.text[:300]}")
        body = response.json()
        if not isinstance(body, dict):
            raise NativeProtocolError(f"The daemon returned a non-object body for {path}")
        return body

    async def register_key(self, agent_id: str, key_id: str, public_hex: str) -> dict[str, Any]:
        return await self._post_json("/agent-protocol/agent-keys", {
            "agent_id": agent_id, "key_id": key_id, "public_key_hex": public_hex,
        })

    async def daemon_keys(self) -> dict[str, bytes]:
        response = await self.http.get(f"{self.base_url}/agent-protocol/keys", headers=self.headers())
        body = self._result(response, "/agent-protocol/keys")
        keys: dict[str, bytes] = {}
        for record in body.get("daemon_keys", []):
            keys[str(record["key_id"])] = bytes.fromhex(str(record["public_key_hex"]))
        return keys

    async def post_acknowledgement(self, payload: bytes) -> dict[str, Any]:
        return await self._post_bytes("/agent-protocol/acknowledgements", payload)

    async def request_effect_grant(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/agent-protocol/effect-grants", payload)

    async def post_receipt(self, payload: bytes) -> dict[str, Any]:
        return await self._post_bytes("/agent-protocol/receipts", payload)


@dataclass
class EffectHandle:
    """One nested effect under one daemon-issued effect grant."""

    grant: dict[str, Any]
    effect_id: str
    effect_operation_id: str
    effect_attempt_number: int
    dispatch_ref: str
    sequence: int = 0
    sent: bool = False


@dataclass
class EffectContext:
    """The activation grant an execution runs under."""

    protocol: NativeProtocol
    grant: dict[str, Any]
    calls: int = 0
    handles: list[EffectHandle] = field(default_factory=list)

    def next_call(self) -> int:
        self.calls += 1
        return self.calls


EFFECT_CONTEXT: ContextVar[EffectContext | None] = ContextVar("bmas_effect_context", default=None)


def current_effect_context() -> EffectContext | None:
    return EFFECT_CONTEXT.get()


def _payload_without_signature(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "signature"}


def _usage_ints(usage: dict[str, Any] | None) -> dict[str, int] | None:
    if not usage:
        return None
    result: dict[str, int] = {}
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            result[str(key)] = value
        elif isinstance(value, float) and value.is_integer():
            result[str(key)] = int(value)
    return result or None


class NativeProtocol:
    """The agent's native protocol state: keys, document, store, and client."""

    def __init__(
        self,
        *,
        agent_id: str,
        cache_dir: Path,
        daemon_url: str,
        node_key: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.cache_dir = Path(cache_dir)
        self.keys = load_or_create_agent_key(self.cache_dir / "agent-signing-key", f"agent-key-{agent_id}")
        self.document = capability_document(agent_id, self.keys.key_id)
        self.capability_digest = digest_hex(CAPABILITY_DOCUMENT_DIGEST_DOMAIN, self.document)
        self.store = GrantStore(self.cache_dir / "grants")
        self.client = DaemonClient(daemon_url, node_key=node_key, node_id=agent_id, http=http)
        self._daemon_keys: dict[str, bytes] = self._load_daemon_key_cache()
        self._registered = False
        self._lock = asyncio.Lock()

    # ── Keys ─────────────────────────────────────────────────────────

    def _daemon_key_cache_path(self) -> Path:
        return self.cache_dir / "daemon-keys.json"

    def _load_daemon_key_cache(self) -> dict[str, bytes]:
        path = self._daemon_key_cache_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
            return {str(key): bytes.fromhex(str(value)) for key, value in data.items()}
        except (OSError, ValueError):
            return {}

    def _save_daemon_key_cache(self) -> None:
        path = self._daemon_key_cache_path()
        path.write_text(json.dumps({key: value.hex() for key, value in self._daemon_keys.items()}, sort_keys=True))
        os.chmod(path, 0o600)

    async def ensure_registered(self) -> bool:
        """Register the agent public key with the daemon once per process."""
        if self._registered:
            return True
        try:
            await self.client.register_key(self.agent_id, self.keys.key_id, self.keys.public_hex)
        except (NativeProtocolError, httpx.HTTPError) as exc:
            logger.warning("Agent key registration deferred: %s", exc)
            return False
        self._registered = True
        return True

    async def daemon_public_key(self, key_id: str) -> bytes:
        if key_id not in self._daemon_keys:
            self._daemon_keys.update(await self.client.daemon_keys())
            self._save_daemon_key_cache()
        try:
            return self._daemon_keys[key_id]
        except KeyError as exc:
            raise GrantRejectedError("unknown_daemon_key", f"Unknown daemon key {key_id}") from exc

    # ── Grants ───────────────────────────────────────────────────────

    async def verify_grant(self, grant: dict[str, Any]) -> None:
        """Verify one activation grant or raise ``GrantRejectedError``."""
        for member in ("activation_grant_id", "task_id", "run_id", "activation_id", "attempt",
                       "task_fence", "activation_fence", "agent_id", "agent_protocol_version",
                       "audience", "not_before", "expires_at", "grant_nonce", "key_id",
                       "signature_algorithm", "signature"):
            if member not in grant:
                raise GrantRejectedError("malformed_grant", f"The grant misses {member}")
        if grant["signature_algorithm"] != SIGNATURE_ALGORITHM:
            raise GrantRejectedError("signature_algorithm", "Unknown signature algorithm")
        if grant["audience"] != AUDIENCE:
            raise GrantRejectedError("audience", "The grant names another audience")
        if grant["agent_id"] != self.agent_id:
            raise GrantRejectedError("agent_id", "The grant names another agent")
        if grant["agent_protocol_version"] != PROTOCOL_VERSION:
            raise GrantRejectedError("protocol_version", "The grant names another protocol")
        now = utc_now()
        if str(grant["expires_at"]) <= now:
            raise GrantRejectedError("expired", "The grant expired")
        if str(grant["not_before"]) > now:
            raise GrantRejectedError("not_yet_valid", "The grant is not valid yet")
        public = await self.daemon_public_key(str(grant["key_id"]))
        try:
            verify_payload(public, ACTIVATION_GRANT_DOMAIN, _payload_without_signature(grant), str(grant["signature"]))
        except SigningError as exc:
            raise GrantRejectedError("signature", str(exc)) from exc

    def sign(self, fields: dict[str, Any], domain: str) -> dict[str, Any]:
        unsigned = {**fields, "signature_algorithm": SIGNATURE_ALGORITHM}
        signature = sign_payload(self.keys.private_key, domain, unsigned)
        return {**unsigned, "signature": signature}

    def acknowledgement_fields(
        self, grant: dict[str, Any], grant_digest: str, decision: str, reason_code: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "acknowledgement_id": f"acknowledgement-{grant['activation_grant_id']}",
            "activation_grant_id": grant["activation_grant_id"],
            "activation_grant_digest": grant_digest,
            "task_id": grant["task_id"],
            "run_id": grant["run_id"],
            "runtime_key": dict(grant["runtime_key"]),
            "activation_id": grant["activation_id"],
            "attempt": int(grant["attempt"]),
            "task_fence": grant["task_fence"],
            "activation_fence": grant["activation_fence"],
            "agent_id": self.agent_id,
            "audience": AUDIENCE,
            "agent_protocol_version": PROTOCOL_VERSION,
            "capability_digest": self.capability_digest,
            "decision": decision,
            "decision_reason_code": reason_code,
            "agent_execution_id": None,
            "grant_nonce": grant["grant_nonce"],
            "agent_observed_at": utc_now(),
            "key_id": self.keys.key_id,
        }

    async def activate(
        self,
        grant: dict[str, Any],
        grant_digest: str,
        execute: Callable[[EffectContext], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Acknowledge one grant durably, then execute once."""
        grant_id = str(grant.get("activation_grant_id", ""))
        if not grant_id:
            raise GrantRejectedError("malformed_grant", "The grant carries no identifier")
        await self.ensure_registered()
        async with self._lock:
            record = self.store.load(grant_id)
            if record is None:
                try:
                    await self.verify_grant(grant)
                    decision, reason = "accepted", "accepted"
                except GrantRejectedError as exc:
                    decision, reason = "rejected", exc.reason_code
                acknowledgement = self.sign(
                    self.acknowledgement_fields(grant, grant_digest, decision, reason),
                    ACTIVATION_ACKNOWLEDGEMENT_DOMAIN,
                )
                record = {
                    "grant": grant, "grant_digest": grant_digest,
                    "acknowledgement": acknowledgement, "decision": decision,
                    "result": None, "stored_at": utc_now(),
                }
                self.store.save(grant_id, record)
        acknowledgement = record["acknowledgement"]
        if record.get("result") is not None:
            return {"acknowledgement": acknowledgement, "result": record["result"], "replayed": True}
        outcome = await self.client.post_acknowledgement(canonical_bytes(acknowledgement))
        status = str(outcome.get("status", ""))
        if record["decision"] != "accepted" or status not in ("accepted", "duplicate"):
            return {"acknowledgement": acknowledgement, "result": None, "replayed": False, "daemon_outcome": outcome}
        context = EffectContext(protocol=self, grant=grant)
        result = await execute(context)
        record["result"] = result
        record["completed_at"] = utc_now()
        self.store.save(grant_id, record)
        return {"acknowledgement": acknowledgement, "result": result, "replayed": False, "daemon_outcome": outcome}

    def acknowledgement_for(self, grant_id: str) -> dict[str, Any] | None:
        record = self.store.load(grant_id)
        if record is None:
            return None
        return {
            "acknowledgement": record["acknowledgement"],
            "decision": record["decision"],
            "completed": record.get("result") is not None,
            "stored_at": record.get("stored_at"),
        }

    # ── Nested effects ───────────────────────────────────────────────

    def verify_effect_grant(self, grant: dict[str, Any], *, request_digest: str, task_fence: str) -> None:
        for member in ("token_id", "effect_id", "effect_operation_id", "effect_attempt_number", "dispatch_ref",
                       "activation_id", "activation_attempt", "task_fence", "request_digest", "audience",
                       "agent_id", "expires_at", "provider", "model", "tool", "operation", "key_id", "signature"):
            if member not in grant:
                raise NativeProtocolError(f"The effect grant misses {member}")
        if grant["audience"] != AUDIENCE or grant["agent_id"] != self.agent_id:
            raise NativeProtocolError("The effect grant names another agent or audience")
        if grant["request_digest"] != request_digest:
            raise NativeProtocolError("The effect grant binds another request")
        if grant["task_fence"] != task_fence:
            raise NativeProtocolError("The effect grant binds another task fence")
        if str(grant["expires_at"]) <= utc_now():
            raise NativeProtocolError("The effect grant expired")
        public = self._daemon_keys.get(str(grant["key_id"]))
        if public is None:
            raise NativeProtocolError(f"Unknown daemon key {grant['key_id']} on the effect grant")
        verify_payload(public, EFFECT_GRANT_DOMAIN, _payload_without_signature(grant), str(grant["signature"]))

    async def open_model_effect(self, context: EffectContext, *, model: str, request: dict[str, Any]) -> EffectHandle:
        """Request one effect grant for one model call and record the start."""
        grant = context.grant
        request_digest = digest_hex(REQUEST_DIGEST_DOMAIN, request)
        call = context.next_call()
        response = await self.client.request_effect_grant({
            "run_id": grant["run_id"],
            "parent_grant_id": grant["activation_grant_id"],
            "kind": "provider",
            "request_digest": request_digest,
            "child_idempotency_key": f"{grant['activation_grant_id']}:model:{call}",
            "retry_safety": "conditional",
            "target": "litellm",
            "operation": "chat",
            "provider": "litellm",
            "model": model,
            "capability_digest": self.capability_digest,
            "task_fence": grant["task_fence"],
        })
        effect_grant = response["grant"]
        await self.daemon_public_key(str(effect_grant.get("key_id", "")))
        self.verify_effect_grant(effect_grant, request_digest=request_digest, task_fence=str(grant["task_fence"]))
        handle = EffectHandle(
            grant=effect_grant,
            effect_id=str(response["effect_id"]),
            effect_operation_id=str(response["effect_operation_id"]),
            effect_attempt_number=int(response["effect_attempt_number"]),
            dispatch_ref=str(response["dispatch_ref"]),
        )
        context.handles.append(handle)
        await self.receipt(handle, stage=STAGE_TRANSPORT_STARTING)
        handle.sent = True
        return handle

    async def receipt(
        self,
        handle: EffectHandle,
        *,
        stage: str,
        usage: dict[str, Any] | None = None,
        raw_response: bytes | None = None,
        transport_observation: str | None = None,
    ) -> dict[str, Any]:
        """Sign and post one attempt receipt for one nested effect."""
        handle.sequence += 1
        grant = handle.grant
        fields = {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": f"receipt-{handle.effect_id}-{handle.sequence}",
            "effect_operation_id": handle.effect_operation_id,
            "effect_id": handle.effect_id,
            "effect_attempt_number": handle.effect_attempt_number,
            "dispatch_ref": handle.dispatch_ref,
            "token_id": grant["token_id"],
            "activation_id": grant["activation_id"],
            "activation_attempt": int(grant["activation_attempt"]),
            "receipt_sequence": handle.sequence,
            "request_digest": grant["request_digest"],
            "provider": grant.get("provider"),
            "model": grant.get("model"),
            "tool": grant.get("tool"),
            "operation": grant["operation"],
            "stage": stage,
            "transport_observation": transport_observation,
            "provider_run_id": None,
            "provider_receipt": None,
            "raw_response_digest": hashlib.sha256(raw_response).hexdigest() if raw_response is not None else None,
            "usage": _usage_ints(usage),
            "agent_id": self.agent_id,
            "protocol_version": PROTOCOL_VERSION,
            "agent_observed_at": utc_now(),
            "key_id": self.keys.key_id,
        }
        signed = self.sign(fields, ATTEMPT_RECEIPT_DOMAIN)
        return await self.client.post_receipt(canonical_bytes(signed))
