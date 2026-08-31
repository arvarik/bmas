"""Foundation model proposals and trusted execution envelopes.

A model proposal is untrusted content. It never carries trusted
execution status, usage, provider identity, or activation identity.
The daemon builds every trusted ``AgentExecutionEnvelope`` from
verified agent receipts and protected artifacts; an agent-created
object that resembles the envelope stays untrusted content.

The daemon saves the protected raw response before parsing. It parses
the response and then seals the envelope with the trusted result
reference. The envelope holds exactly one result field: a proposal
reference, a parse-failure reference, or a no-proposal reason. It
never holds an inline model proposal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.digest_profile import digest_hex

ENVELOPE_DIGEST_DOMAIN = "agent-execution-envelope"
PROPOSAL_DIGEST_DOMAIN = "model-proposal"

TRUSTED_STATUSES = ("completed", "failed", "cancelled", "unknown")

# Trusted fields that model content can never supply.
FORBIDDEN_PROPOSAL_FIELDS = (
    "status",
    "usage",
    "provider",
    "provider_run_id",
    "activation_id",
    "activation_attempt",
    "task_id",
    "run_id",
    "effect_id",
    "proposal_ref",
    "parse_failure_ref",
    "execution_status",
)

PROPOSAL_STATUS_POLICIES = ("reject", "ignore")


class ModelProposalError(ValueError):
    """The model proposal failed validation."""


class EnvelopeError(ValueError):
    """The execution envelope failed validation."""


@dataclass(frozen=True)
class ModelProposal:
    """One parsed untrusted model proposal."""

    schema_version: str
    content: dict[str, Any]

    def digest(self) -> str:
        return digest_hex(
            PROPOSAL_DIGEST_DOMAIN,
            {"schema_version": self.schema_version, "content": self.content},
        )


def parse_model_proposal(
    payload: dict[str, Any],
    *,
    schema_version: str = "1",
    status_policy: str = "reject",
) -> ModelProposal:
    """Parse one untrusted model proposal.

    The parser rejects or strips trusted fields inside model content
    under the registered policy. It never trusts a model-supplied
    execution status.
    """
    if status_policy not in PROPOSAL_STATUS_POLICIES:
        raise ModelProposalError(
            f"Unknown status policy: {status_policy!r}"
        )
    if not isinstance(payload, dict):
        raise ModelProposalError("A model proposal is one JSON object")
    present = [name for name in FORBIDDEN_PROPOSAL_FIELDS if name in payload]
    if present:
        if status_policy == "reject":
            raise ModelProposalError(
                f"Model content cannot carry trusted fields: {present}"
            )
        payload = {
            name: value
            for name, value in payload.items()
            if name not in FORBIDDEN_PROPOSAL_FIELDS
        }
    return ModelProposal(schema_version=schema_version, content=dict(payload))


@dataclass(frozen=True)
class VerifiedReceiptChain:
    """The daemon-verified receipt digests of one dispatch.

    Only the daemon constructs this record, after it verified each
    receipt signature and binding. The envelope builder accepts no
    other receipt source.
    """

    dispatch_ref: str
    receipt_digests: tuple[str, ...]
    provider_run_id: str | None = None
    provider_receipt: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class AgentExecutionEnvelope:
    """The host-created trusted execution record of one activation.

    The daemon supplies the trusted status and the database times.
    The envelope references the protected raw artifact and exactly one
    result: a proposal reference, a parse-failure reference, or a
    no-proposal reason.
    """

    schema_version: str
    trusted_status: str
    task_id: str
    run_id: str
    activation_id: str
    activation_attempt: int
    receipt_digests: tuple[str, ...]
    raw_response_artifact_digest: str
    started_at: str
    observed_at: str
    provider_run_id: str | None = None
    provider_receipt: str | None = None
    usage: dict[str, int] | None = None
    proposal_ref: str | None = None
    parse_failure_ref: str | None = None
    no_proposal_reason: str | None = None
    host_created: bool = field(default=True)

    def result_fields(self) -> list[str]:
        return [
            name
            for name in (
                "proposal_ref",
                "parse_failure_ref",
                "no_proposal_reason",
            )
            if getattr(self, name) is not None
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trusted_status": self.trusted_status,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "activation_id": self.activation_id,
            "activation_attempt": self.activation_attempt,
            "receipt_digests": list(self.receipt_digests),
            "raw_response_artifact_digest": (
                self.raw_response_artifact_digest
            ),
            "started_at": self.started_at,
            "observed_at": self.observed_at,
            "provider_run_id": self.provider_run_id,
            "provider_receipt": self.provider_receipt,
            "usage": self.usage,
            "proposal_ref": self.proposal_ref,
            "parse_failure_ref": self.parse_failure_ref,
            "no_proposal_reason": self.no_proposal_reason,
        }

    def digest(self) -> str:
        return digest_hex(ENVELOPE_DIGEST_DOMAIN, self.to_dict())


def validate_envelope(envelope: AgentExecutionEnvelope) -> None:
    """Validate one envelope or fail closed.

    The envelope requires the trusted activation identity, the raw
    result digest, and the execution status. It holds exactly one
    result field and never an inline proposal.
    """
    if envelope.trusted_status not in TRUSTED_STATUSES:
        raise EnvelopeError(
            f"Unknown trusted status: {envelope.trusted_status!r}"
        )
    for name in (
        "task_id",
        "run_id",
        "activation_id",
        "raw_response_artifact_digest",
    ):
        if not getattr(envelope, name):
            raise EnvelopeError(f"The envelope requires {name}")
    results = envelope.result_fields()
    if len(results) != 1:
        raise EnvelopeError(
            "The envelope holds exactly one result field; it holds "
            f"{results or 'none'}"
        )
    if envelope.proposal_ref is not None and not isinstance(
        envelope.proposal_ref, str,
    ):
        raise EnvelopeError(
            "The proposal reference is one digest string, never an "
            "inline proposal"
        )
    if not envelope.host_created:
        raise EnvelopeError("Only the daemon creates the trusted envelope")


def build_envelope(
    *,
    trusted_status: str,
    task_id: str,
    run_id: str,
    activation_id: str,
    activation_attempt: int,
    receipt_chain: VerifiedReceiptChain,
    raw_response_artifact_digest: str,
    started_at: str,
    observed_at: str,
    proposal: ModelProposal | None = None,
    parse_failure_ref: str | None = None,
    no_proposal_reason: str | None = None,
    schema_version: str = "1",
) -> AgentExecutionEnvelope:
    """Build and seal one trusted envelope from verified inputs.

    The caller parses the raw response only after the protected raw
    artifact persisted; this builder then seals the envelope with the
    one trusted result reference. The receipt chain must come from the
    daemon verifier.
    """
    if not isinstance(receipt_chain, VerifiedReceiptChain):
        raise EnvelopeError(
            "The envelope builds only from daemon-verified receipts"
        )
    supplied = [
        value
        for value in (proposal, parse_failure_ref, no_proposal_reason)
        if value is not None
    ]
    if len(supplied) != 1:
        raise EnvelopeError(
            "The builder needs exactly one result: a parsed proposal, a "
            "parse-failure reference, or a no-proposal reason"
        )
    envelope = AgentExecutionEnvelope(
        schema_version=schema_version,
        trusted_status=trusted_status,
        task_id=task_id,
        run_id=run_id,
        activation_id=activation_id,
        activation_attempt=activation_attempt,
        receipt_digests=receipt_chain.receipt_digests,
        raw_response_artifact_digest=raw_response_artifact_digest,
        started_at=started_at,
        observed_at=observed_at,
        provider_run_id=receipt_chain.provider_run_id,
        provider_receipt=receipt_chain.provider_receipt,
        usage=receipt_chain.usage,
        proposal_ref=proposal.digest() if proposal is not None else None,
        parse_failure_ref=parse_failure_ref,
        no_proposal_reason=no_proposal_reason,
    )
    validate_envelope(envelope)
    return envelope


def classify_agent_payload(payload: dict[str, Any]) -> str:
    """Classify one agent-returned object.

    An object that resembles the trusted envelope stays untrusted
    content; only the daemon builds the envelope from verified
    receipts.
    """
    envelope_markers = {
        "trusted_status",
        "receipt_digests",
        "proposal_ref",
        "parse_failure_ref",
        "no_proposal_reason",
        "host_created",
    }
    if envelope_markers & set(payload):
        return "untrusted_content"
    return "model_output"
