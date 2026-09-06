"""Foundation agent protocol contracts and endpoint negotiation.

This module holds the pure protocol contracts: the bMAS agent
capability document, the signed activation grant, the signed
activation acknowledgement, the signed effect grant, the signed
attempt receipt, and the endpoint directory that partitions agents by
exact protocol and qualification state.

The daemon signs grants. The agent signs acknowledgements and
receipts. Every signature covers the RFC 8785 canonical object without
its signature field, under one signature domain, with the registered
``ed25519-jcs`` algorithm identifier.

An activation grant permits the agent to accept work and request
child effects. It does not permit a provider call, a tool call, or
another external effect; only an effect grant does.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Literal

from core.digest_profile import (
    DIGEST_PROFILE,
    DigestInputError,
    canonicalize,
    digest_hex,
    parse_digest_input,
)
from core.signing import (
    ACTIVATION_ACKNOWLEDGEMENT_DOMAIN,
    ACTIVATION_GRANT_DOMAIN,
    ATTEMPT_RECEIPT_DOMAIN,
    EFFECT_GRANT_DOMAIN,
    SIGNATURE_ALGORITHM,
    KeyRegistry,
    sign_payload,
    verify_payload,
)
from core.variants import RuntimeKey

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

# A digest value in these contracts is one lowercase hexadecimal
# SHA-256 string under the bmas-digest profile.
Digest = str

CURRENT_AGENT_PROTOCOL_VERSION = "2"
LEGACY_AGENT_PROTOCOL_VERSION = "1"
RECEIPT_CONTRACT_VERSION = "1"
GRANT_SCHEMA_VERSION = "1"
ACKNOWLEDGEMENT_SCHEMA_VERSION = "1"
CAPABILITY_DOCUMENT_DIGEST_DOMAIN = "agent-capability-document"

ACKNOWLEDGEMENT_DECISIONS = ("accepted", "rejected")

RECEIPT_STAGES = (
    "grant_acknowledged",
    "transport_starting",
    "provider_acknowledged",
    "response_observed",
    "failed_before_transport",
    "cancellation_observed",
)


class AgentProtocolError(ValueError):
    """One agent protocol rule failed closed."""


class GrantBindingError(AgentProtocolError):
    """A signed contract does not bind the expected values."""


class AcknowledgementError(AgentProtocolError):
    """The acknowledgement failed validation."""


class ReceiptError(AgentProtocolError):
    """The attempt receipt failed validation."""


class QualificationError(AgentProtocolError):
    """The agent endpoint cannot qualify for the current protocol."""


class NoQualifiedEndpointError(AgentProtocolError):
    """No qualified endpoint supports every required capability."""


def _dataclass_payload(record: Any) -> dict[str, Any]:
    """Return the canonical signing payload without the signature."""
    payload: dict[str, Any] = {}
    for spec in fields(record):
        if spec.name == "signature":
            continue
        value = getattr(record, spec.name)
        if isinstance(value, RuntimeKey):
            value = value.to_dict()
        elif isinstance(value, tuple):
            value = list(value)
        payload[spec.name] = value
    return payload


# ── Agent capability document ────────────────────────────────────────


@dataclass(frozen=True)
class AgentCapabilityDocument:
    """The bMAS agent capability document.

    The document declares the agent's own capabilities. It never
    proxies an upstream provider capability document.
    """

    schema_version: str
    agent_id: str
    supported_protocol_versions: tuple[str, ...]
    supported_receipt_versions: tuple[str, ...]
    supported_activation_schemas: tuple[str, ...]
    supported_dispatch_schemas: tuple[str, ...]
    supported_acknowledgement_schemas: tuple[str, ...]
    supported_proposal_schemas: tuple[str, ...]
    supported_envelope_schemas: tuple[str, ...]
    nested_model_receipts: bool
    nested_tool_receipts: bool
    structured_output: bool
    usage_reporting: bool
    streaming: bool
    cancellation: bool
    resume: bool
    durable_grant_deduplication: bool
    acknowledgement_status_lookup: bool
    receipt_key_ids: tuple[str, ...]
    max_request_bytes: int
    max_response_bytes: int
    max_artifact_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_payload(self)

    def digest(self) -> Digest:
        return digest_hex(CAPABILITY_DOCUMENT_DIGEST_DOMAIN, self.to_dict())


def qualification_failures(document: AgentCapabilityDocument) -> list[str]:
    """List every reason the document fails current-protocol qualification.

    An endpoint without durable grant deduplication and durable
    acknowledgement status lookup cannot qualify.
    """
    failures: list[str] = []
    if (
        CURRENT_AGENT_PROTOCOL_VERSION
        not in document.supported_protocol_versions
    ):
        failures.append("protocol_version")
    if RECEIPT_CONTRACT_VERSION not in document.supported_receipt_versions:
        failures.append("receipt_version")
    if not document.durable_grant_deduplication:
        failures.append("durable_grant_deduplication")
    if not document.acknowledgement_status_lookup:
        failures.append("acknowledgement_status_lookup")
    if not document.nested_model_receipts:
        failures.append("nested_model_receipts")
    if not document.nested_tool_receipts:
        failures.append("nested_tool_receipts")
    if not document.receipt_key_ids:
        failures.append("receipt_key_ids")
    return failures


def is_qualified(document: AgentCapabilityDocument) -> bool:
    """Report current-protocol qualification for one capability document."""
    return not qualification_failures(document)


# ── Activation grant ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ActivationGrant:
    """The signed permission for one agent to accept one activation.

    The grant binds the task, run, runtime pair, activation, attempt,
    request, context view, both fences, agent, protocol, audience,
    expiry, and nonce. It permits the agent to accept work and request
    child effects; it authorizes no external effect, so delivery can
    repeat with the same signed bytes.
    """

    schema_version: str
    activation_grant_id: str
    task_id: str
    run_id: str
    runtime_key: RuntimeKey
    activation_id: str
    attempt: int
    request_digest: Digest
    context_view_digest: Digest
    task_fence: str
    activation_fence: str
    agent_id: str
    agent_protocol_version: str
    audience: str
    not_before: str
    expires_at: str
    grant_nonce: str
    key_id: str
    signature_algorithm: Literal["ed25519-jcs"]
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        return _dataclass_payload(self)

    def to_bytes(self) -> bytes:
        """Return the exact canonical bytes of the complete signed grant."""
        payload = self.signing_payload()
        payload["signature"] = self.signature
        return canonicalize(payload).encode("utf-8")


def sign_activation_grant(
    grant_fields: dict[str, Any], private_key: Ed25519PrivateKey,
) -> ActivationGrant:
    """Sign one activation grant with the daemon key."""
    unsigned = ActivationGrant(
        signature="", signature_algorithm=SIGNATURE_ALGORITHM, **grant_fields,
    )
    signature = sign_payload(
        private_key, ACTIVATION_GRANT_DOMAIN, unsigned.signing_payload(),
    )
    return ActivationGrant(
        signature=signature,
        signature_algorithm=SIGNATURE_ALGORITHM,
        **grant_fields,
    )


def verify_activation_grant(
    grant: ActivationGrant, registry: KeyRegistry,
) -> None:
    """Verify the daemon signature on one activation grant."""
    record = registry.require(grant.key_id)
    verify_payload(
        record.public_bytes,
        ACTIVATION_GRANT_DOMAIN,
        grant.signing_payload(),
        grant.signature,
    )


# ── Activation acknowledgement ───────────────────────────────────────


@dataclass(frozen=True)
class ActivationAcknowledgement:
    """The exact signed agent response to one activation grant."""

    schema_version: str
    acknowledgement_id: str
    activation_grant_id: str
    activation_grant_digest: Digest
    task_id: str
    run_id: str
    runtime_key: RuntimeKey
    activation_id: str
    attempt: int
    task_fence: str
    activation_fence: str
    agent_id: str
    audience: str
    agent_protocol_version: str
    capability_digest: Digest
    decision: Literal["accepted", "rejected"]
    decision_reason_code: str
    agent_execution_id: str | None
    grant_nonce: str
    agent_observed_at: str
    key_id: str
    signature_algorithm: Literal["ed25519-jcs"]
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        return _dataclass_payload(self)

    def canonical_digest(self) -> Digest:
        payload = self.signing_payload()
        payload["signature"] = self.signature
        return digest_hex("activation-acknowledgement", payload)

    def to_bytes(self) -> bytes:
        payload = self.signing_payload()
        payload["signature"] = self.signature
        return canonicalize(payload).encode("utf-8")


def sign_acknowledgement(
    acknowledgement_fields: dict[str, Any],
    private_key: Ed25519PrivateKey,
) -> ActivationAcknowledgement:
    """Sign one acknowledgement with the registered agent key."""
    unsigned = ActivationAcknowledgement(
        signature="",
        signature_algorithm=SIGNATURE_ALGORITHM,
        **acknowledgement_fields,
    )
    signature = sign_payload(
        private_key,
        ACTIVATION_ACKNOWLEDGEMENT_DOMAIN,
        unsigned.signing_payload(),
    )
    return ActivationAcknowledgement(
        signature=signature,
        signature_algorithm=SIGNATURE_ALGORITHM,
        **acknowledgement_fields,
    )


_ACKNOWLEDGEMENT_FIELDS = frozenset(
    spec.name for spec in fields(ActivationAcknowledgement)
)


def parse_acknowledgement(text: str) -> ActivationAcknowledgement:
    """Parse one acknowledgement strictly.

    The parser rejects duplicate JSON keys, unknown members, missing
    members, and non-canonical number forms.
    """
    try:
        data = parse_digest_input(text)
    except DigestInputError as exc:
        raise AcknowledgementError(str(exc)) from exc
    if not isinstance(data, dict):
        raise AcknowledgementError("The acknowledgement is one JSON object")
    unknown = set(data) - _ACKNOWLEDGEMENT_FIELDS
    if unknown:
        raise AcknowledgementError(
            f"The acknowledgement rejects unknown members: {sorted(unknown)}"
        )
    missing = _ACKNOWLEDGEMENT_FIELDS - set(data)
    if missing:
        raise AcknowledgementError(
            f"The acknowledgement misses members: {sorted(missing)}"
        )
    runtime_value = data["runtime_key"]
    if not isinstance(runtime_value, dict):
        raise AcknowledgementError("runtime_key is one runtime pair object")
    data = dict(data)
    data["runtime_key"] = RuntimeKey(**runtime_value)
    if data["decision"] not in ACKNOWLEDGEMENT_DECISIONS:
        raise AcknowledgementError(
            f"Unknown decision: {data['decision']!r}"
        )
    if data["signature_algorithm"] != SIGNATURE_ALGORITHM:
        raise AcknowledgementError(
            f"Unknown signature algorithm: {data['signature_algorithm']!r}"
        )
    return ActivationAcknowledgement(**data)


def verify_acknowledgement_signature(
    acknowledgement: ActivationAcknowledgement, registry: KeyRegistry,
) -> None:
    """Verify the agent signature on one acknowledgement."""
    record = registry.require(acknowledgement.key_id)
    verify_payload(
        record.public_bytes,
        ACTIVATION_ACKNOWLEDGEMENT_DOMAIN,
        acknowledgement.signing_payload(),
        acknowledgement.signature,
    )


# ── Effect grant ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EffectGrant:
    """The signed daemon token that authorizes one external attempt.

    The token binds the complete identity chain: token, task, run,
    activation, activation attempt, effect operation, effect, effect
    attempt, and dispatch. It binds the task fence, the activation
    lease reference, the request digest, the budget reservation with
    its maximum authorized amount, the provider or tool target, the
    agent identity, the audience, the times, and the nonce. The agent
    cannot widen the provider, tool, or budget scope.
    """

    schema_version: str
    token_id: str
    task_id: str
    run_id: str
    activation_id: str
    activation_attempt: int
    effect_operation_id: str
    effect_id: str
    effect_attempt_number: int
    dispatch_ref: str
    task_fence: str
    lease_ref: str
    request_digest: Digest
    reservation_id: str
    max_authorized_amount_nanos: int
    provider: str | None
    model: str | None
    tool: str | None
    operation: str
    capability_digest: Digest
    agent_id: str
    audience: str
    issued_at: str
    expires_at: str
    grant_nonce: str
    protocol_version: str
    digest_profile: str
    key_id: str
    signature_algorithm: Literal["ed25519-jcs"]
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        return _dataclass_payload(self)

    def to_bytes(self) -> bytes:
        payload = self.signing_payload()
        payload["signature"] = self.signature
        return canonicalize(payload).encode("utf-8")


def sign_effect_grant(
    grant_fields: dict[str, Any], private_key: Ed25519PrivateKey,
) -> EffectGrant:
    """Sign one effect grant with the daemon key."""
    unsigned = EffectGrant(
        signature="",
        signature_algorithm=SIGNATURE_ALGORITHM,
        digest_profile=DIGEST_PROFILE,
        **grant_fields,
    )
    signature = sign_payload(
        private_key, EFFECT_GRANT_DOMAIN, unsigned.signing_payload(),
    )
    return EffectGrant(
        signature=signature,
        signature_algorithm=SIGNATURE_ALGORITHM,
        digest_profile=DIGEST_PROFILE,
        **grant_fields,
    )


def verify_effect_grant(
    grant: EffectGrant,
    registry: KeyRegistry,
    *,
    expected: dict[str, Any] | None = None,
    at: str | None = None,
) -> None:
    """Verify the signature and every expected binding of one grant.

    ``expected`` maps field names to required values. The agent uses
    this validation before transport: the signature, the audience, the
    expiry, the fence binding, and the request digest.
    """
    record = registry.require(grant.key_id)
    verify_payload(
        record.public_bytes,
        EFFECT_GRANT_DOMAIN,
        grant.signing_payload(),
        grant.signature,
    )
    if at is not None and at >= grant.expires_at:
        raise GrantBindingError("The effect grant expired")
    for name, value in (expected or {}).items():
        if getattr(grant, name) != value:
            raise GrantBindingError(
                f"The effect grant binds a different {name}"
            )


# ── Attempt receipt ──────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentAttemptReceipt:
    """One signed authenticated observation of one attempt stage.

    The agent returns authenticated observations, not a trusted
    execution envelope. An agent timestamp supports diagnosis only; it
    cannot establish lease, deadline, or ordering authority.
    """

    schema_version: str
    receipt_id: str
    effect_operation_id: str
    effect_id: str
    effect_attempt_number: int
    dispatch_ref: str
    token_id: str
    activation_id: str
    activation_attempt: int
    receipt_sequence: int
    request_digest: Digest
    provider: str | None
    model: str | None
    tool: str | None
    operation: str
    stage: str
    transport_observation: str | None
    provider_run_id: str | None
    provider_receipt: str | None
    raw_response_digest: Digest | None
    usage: dict[str, int] | None
    agent_id: str
    protocol_version: str
    agent_observed_at: str
    key_id: str
    signature_algorithm: Literal["ed25519-jcs"]
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        return _dataclass_payload(self)


def sign_attempt_receipt(
    receipt_fields: dict[str, Any], private_key: Ed25519PrivateKey,
) -> AgentAttemptReceipt:
    """Sign one attempt receipt with the registered agent key."""
    unsigned = AgentAttemptReceipt(
        signature="",
        signature_algorithm=SIGNATURE_ALGORITHM,
        **receipt_fields,
    )
    signature = sign_payload(
        private_key, ATTEMPT_RECEIPT_DOMAIN, unsigned.signing_payload(),
    )
    return AgentAttemptReceipt(
        signature=signature,
        signature_algorithm=SIGNATURE_ALGORITHM,
        **receipt_fields,
    )


_RECEIPT_FIELDS = frozenset(spec.name for spec in fields(AgentAttemptReceipt))


def parse_attempt_receipt(text: str) -> AgentAttemptReceipt:
    """Parse one attempt receipt strictly.

    The parser rejects duplicate JSON keys, unknown members, missing
    members, and non-canonical number forms, the same way the
    acknowledgement parser does.
    """
    try:
        data = parse_digest_input(text)
    except DigestInputError as exc:
        raise ReceiptError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ReceiptError("The receipt is one JSON object")
    unknown = set(data) - _RECEIPT_FIELDS
    if unknown:
        raise ReceiptError(f"The receipt rejects unknown members: {sorted(unknown)}")
    missing = _RECEIPT_FIELDS - set(data)
    if missing:
        raise ReceiptError(f"The receipt misses members: {sorted(missing)}")
    if data["stage"] not in RECEIPT_STAGES:
        raise ReceiptError(f"Unknown receipt stage: {data['stage']!r}")
    if data["signature_algorithm"] != SIGNATURE_ALGORITHM:
        raise ReceiptError(f"Unknown signature algorithm: {data['signature_algorithm']!r}")
    usage = data.get("usage")
    if usage is not None and (
        not isinstance(usage, dict)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in usage.values())
    ):
        raise ReceiptError("usage carries integer counts only")
    return AgentAttemptReceipt(**data)


def verify_attempt_receipt_signature(
    receipt: AgentAttemptReceipt, registry: KeyRegistry,
) -> None:
    """Verify the agent signature on one attempt receipt."""
    if receipt.stage not in RECEIPT_STAGES:
        raise ReceiptError(f"Unknown receipt stage: {receipt.stage!r}")
    record = registry.require(receipt.key_id)
    verify_payload(
        record.public_bytes,
        ATTEMPT_RECEIPT_DOMAIN,
        receipt.signing_payload(),
        receipt.signature,
    )


# ── Endpoint negotiation ─────────────────────────────────────────────


@dataclass(frozen=True)
class AgentEndpoint:
    """One published agent endpoint in one protocol partition."""

    agent_id: str
    protocol_version: str
    qualification_state: str
    capability_document: AgentCapabilityDocument | None = None


class EndpointDirectory:
    """Endpoint pools partitioned by exact protocol and qualification.

    A current-protocol activation cannot route to a legacy agent. The
    directory never downgrades after every current-protocol endpoint
    becomes unavailable; selection fails closed instead.
    """

    def __init__(self) -> None:
        self._endpoints: dict[str, AgentEndpoint] = {}

    def publish(self, endpoint: AgentEndpoint) -> None:
        if endpoint.protocol_version == CURRENT_AGENT_PROTOCOL_VERSION:
            document = endpoint.capability_document
            if document is None or not is_qualified(document):
                raise QualificationError(
                    f"The endpoint {endpoint.agent_id!r} cannot qualify "
                    "for the current protocol"
                )
        self._endpoints[endpoint.agent_id] = endpoint

    def remove(self, agent_id: str) -> None:
        self._endpoints.pop(agent_id, None)

    def health_partitions(self) -> dict[str, list[str]]:
        """Publish both protocol partitions during rollout."""
        partitions: dict[str, list[str]] = {}
        for endpoint in self._endpoints.values():
            partitions.setdefault(endpoint.protocol_version, []).append(
                endpoint.agent_id,
            )
        for agents in partitions.values():
            agents.sort()
        return partitions

    def select(
        self,
        *,
        protocol_version: str,
        required_capability_names: tuple[str, ...] = (),
    ) -> AgentEndpoint:
        """Select one qualified endpoint in one exact protocol partition."""
        for endpoint in sorted(
            self._endpoints.values(), key=lambda entry: entry.agent_id,
        ):
            if endpoint.protocol_version != protocol_version:
                continue
            if endpoint.qualification_state != "qualified":
                continue
            document = endpoint.capability_document
            if protocol_version == CURRENT_AGENT_PROTOCOL_VERSION:
                if document is None or not is_qualified(document):
                    continue
                if any(
                    not getattr(document, name, False)
                    for name in required_capability_names
                ):
                    continue
            return endpoint
        raise NoQualifiedEndpointError(
            "No qualified endpoint supports every required capability "
            f"for protocol {protocol_version}"
        )


def legacy_effect_projection(effect_record: dict[str, Any]) -> dict[str, Any]:
    """Mark one legacy nested effect in compatibility projections.

    A legacy bearer-authenticated execution path has no receipt chain.
    The projection never claims complete effect conformance for it.
    """
    projection = dict(effect_record)
    projection["observability"] = "legacy_unobservable"
    projection["effect_conformance"] = "incomplete"
    return projection
