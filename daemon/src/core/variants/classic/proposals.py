"""The discriminated, untrusted Classic proposal contract.

JSONSchemaBench (https://arxiv.org/abs/2501.10868) motivates testing
constraint coverage independently of provider structured generation.
The daemon validates the stored bytes even when a provider promises JSON.
The registry generates the agent response model and parser fixtures.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from jsonschema import Draft202012Validator

from core.digest_profile import plain_json
from core.protocol import ENTRY_TYPES
from execution_envelope import ModelProposal, ModelProposalError, parse_model_proposal

PROPOSAL_SCHEMA_VERSION = "classic-proposal/1"
ROLE_ACTIONS = {
    "expert": ("contribute", "skip"),
    "critic": ("critique", "skip"),
    "planner": ("plan", "skip"),
    "conflict_resolver": ("resolve_conflict", "skip"),
    "cleaner": ("condense", "skip"),
    "decider": ("decide", "skip"),
    "verifier": ("critique", "skip"),
    "judge": ("vote", "skip"),
}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


ENTRY_SCHEMA = _object({
    "type": {"enum": sorted(ENTRY_TYPES)},
    "title": {"type": "string"}, "body": {"type": "string", "minLength": 1},
    "refs": {"type": "array", "items": {"type": "string"}},
    "sources": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
}, ["type", "body"])

# The later evidence package activates its semantic writes.
# Empty typed collections retain the contract without accepting ignored writes.
EMPTY_COLLECTION = {"type": "array", "maxItems": 0}
ACTION_FIELDS: dict[str, dict[str, Any]] = {
    action: {"entries": {"type": "array", "items": ENTRY_SCHEMA},
             "claims": EMPTY_COLLECTION, "goal_updates": EMPTY_COLLECTION,
             "evidence": EMPTY_COLLECTION, "tool_intents": EMPTY_COLLECTION}
    for action in ("contribute", "critique", "plan", "resolve_conflict", "decide")
}
SUMMARY_SCHEMA = copy.deepcopy(ENTRY_SCHEMA)
SUMMARY_SCHEMA["properties"]["type"] = {"const": "condensed_finding"}
SUMMARY_SCHEMA["required"] = ["type", "body", "refs", "sources"]
ACTION_FIELDS["condense"] = {
    "entries": {"type": "array", "minItems": 1, "maxItems": 1, "items": SUMMARY_SCHEMA},
    "removals": {"type": "array", "minItems": 1, "items": _object({
        "entry_id": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    }, ["entry_id", "reason"])},
}
ACTION_FIELDS["vote"] = {"candidate_ref": {"type": "string", "minLength": 1},
                         "reason": {"type": "string"}}
ACTION_FIELDS["skip"] = {"reason": {"type": "string"}}


def proposal_schema(role: str | None = None) -> dict[str, Any]:
    """Return the JSON Schema for one role, or for the complete registry."""
    if role is not None and role not in ROLE_ACTIONS:
        raise ModelProposalError(f"Unknown Classic proposal role: {role}")
    variants = []
    for role_name, actions in ROLE_ACTIONS.items():
        if role is not None and role_name != role:
            continue
        for action in actions:
            fields = {
                "schema_version": {"const": PROPOSAL_SCHEMA_VERSION},
                "role": {"const": role_name}, "action": {"const": action},
                **ACTION_FIELDS[action],
            }
            required = ["schema_version", "role", "action"]
            required += ["entries"] if "entries" in fields else []
            required += ["candidate_ref"] if action == "vote" else []
            required += ["removals"] if action == "condense" else []
            variants.append(_object(fields, required))
    return copy.deepcopy({"$schema": "https://json-schema.org/draft/2020-12/schema",
                          "oneOf": variants})


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ModelProposalError(f"Duplicate proposal field: {name}")
        result[name] = value
    return result


def proposal_request(request: dict[str, Any], *, activation_id: str, attempt: int) -> dict[str, Any]:
    """Bind the role schema and fresh context to one native attempt."""
    role = str(request.get("role") or "expert")
    from models.personas import NATIVE_CLEANER_INSTRUCTIONS

    session_id = f"{activation_id}:{attempt}"
    cleaner_instructions = NATIVE_CLEANER_INSTRUCTIONS if role == "cleaner" else ""
    return {**request, "activation_id": activation_id, "session_id": session_id,
            "role_prompt": str(request.get("role_prompt") or "") + cleaner_instructions
            + "\nReturn exactly one JSON proposal that matches this schema:\n" + json.dumps(proposal_schema(role)),
            "context": {**(request.get("context") or {}), "classic_proposal_role": role,
                        "previous_response_id": None, "session_id": session_id}}


def parse_proposal(raw: bytes, *, role: str) -> ModelProposal:
    """Parse one protected raw response into one role-bound ModelProposal."""
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_members)
        errors = list(Draft202012Validator(proposal_schema(role)).iter_errors(payload))
        if errors:
            raise ModelProposalError(errors[0].message)
        return parse_model_proposal(plain_json(payload), schema_version=PROPOSAL_SCHEMA_VERSION)
    except (ValueError, UnicodeError) as exc:
        raise ModelProposalError(f"Invalid Classic proposal: {exc}") from exc


def parser_fixtures() -> list[dict[str, Any]]:
    """Generate one valid case and forbidden-field cases per discriminator."""
    from execution_envelope import FORBIDDEN_PROPOSAL_FIELDS

    fixtures = []
    for variant in proposal_schema()["oneOf"]:
        properties = variant["properties"]
        payload = {name: properties[name]["const"] for name in ("schema_version", "role", "action")}
        if "entries" in properties:
            payload["entries"] = []
        if payload["action"] == "condense":
            payload["entries"] = [{"type": "condensed_finding", "body": "Summary",
                                   "refs": ["e-source"], "sources": []}]
            payload["removals"] = [{"entry_id": "e-source", "reason": "Summarized"}]
        if "candidate_ref" in properties:
            payload["candidate_ref"] = "candidate-example"
        fixtures.append({"valid": True, "payload": payload})
        for field in ("schema_version", "role", "action"):
            fixtures.append({"valid": False, "payload": {**payload, field: "unknown"}})
        if "entries" in properties and payload["action"] != "condense":
            fixtures.append({"valid": True, "payload": {**payload,
                "entries": [{"type": "finding", "body": "One observation", "confidence": 1}]}})
            fixtures.append({"valid": False, "payload": {**payload,
                "entries": [{"type": "finding", "body": "One observation", "status": "completed"}]}})
            fixtures.append({"valid": False, "payload": {**payload,
                "entries": [{"type": "finding", "body": ""}]}})
        for field in (*FORBIDDEN_PROPOSAL_FIELDS, "unexpected"):
            fixtures.append({"valid": False, "payload": {**payload, field: "untrusted"}})
    return fixtures
