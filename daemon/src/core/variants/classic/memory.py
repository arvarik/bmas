"""Host-owned actor memory and provider session isolation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextSessionPolicy:
    """Build the activation-scoped provider session (doc 12 §5.2)."""

    # The last response identity per actor: cross-round memory of one
    # chained model conversation.
    response_ids: dict[str, str] = field(default_factory=dict)

    def get_response_id(self, actor: str) -> str | None:
        """Get the last response_id for an actor (cross-round memory)."""
        return self.response_ids.get(actor)

    def set_response_id(self, actor: str, response_id: str) -> None:
        """Store the response_id from an actor's latest turn."""
        self.response_ids[actor] = response_id

    def clear_response_id(self, actor: str) -> None:
        """Drop stateful response context after a safe endpoint failover."""
        self.response_ids.pop(actor, None)

    def session_fields(self, task_id: str, actor: str, *, actor_context: str) -> dict[str, Any]:
        """The session fields of one turn payload.

        In fresh context mode the bounded board view is the whole memory:
        the model conversation does not chain, so per-round cost stays
        flat over long runs.
        """
        return {
            "session_id": f"{task_id}:{actor}",
            "previous_response_id": (
                self.get_response_id(actor)
                if actor_context == "chained"
                else None
            ),
        }


MEMORY_TOKEN_LIMIT = 1024


def provider_session_id(activation_id: str, attempt: int) -> str:
    """Return one opaque session identifier for one activation attempt."""
    from core.digest_profile import digest_hex

    return digest_hex("classic-provider-session", {"activation_id": activation_id, "attempt": attempt})


def isolated_request(request: dict[str, Any], activation_id: str, attempt: int) -> dict[str, Any]:
    """Remove continuation keys and bind the attempt session."""
    forbidden = {"session_id", "previous_response_id", "memory_scope", "actor_session_id", "task_session_id"}
    context = {key: value for key, value in (request.get("context") or {}).items() if key not in forbidden}
    result = {key: value for key, value in request.items() if key not in forbidden}
    return {**result, "session_id": provider_session_id(activation_id, attempt), "context": context}


def validate_memory(value: Any) -> dict[str, Any]:
    """Validate bounded notes, questions, and entry references."""
    import json

    names = {"working_notes", "open_questions", "entry_ids"}
    if not isinstance(value, dict) or set(value) - names:
        raise ValueError("Actor memory accepts only notes, questions, and entry identifiers")
    record = {name: value.get(name, []) for name in sorted(names)}
    if any(not isinstance(items, list) or len(items) > 32
           or any(not isinstance(item, str) or len(item) > 1024 for item in items)
           for items in record.values()):
        raise ValueError("Actor memory requires bounded lists of text")
    rendered = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if (len(rendered) + 3) // 4 > MEMORY_TOKEN_LIMIT:
        raise ValueError("Actor memory exceeds the pinned character tokenizer limit")
    return record


async def current_memory_digest(run_id: str, actor: str) -> str | None:
    """Read the latest committed memory reference from the verified journal."""
    import runtime_journal as journal

    records = await journal.read_journal(run_id=run_id)
    journal.verify_chain(records)
    for record in reversed(records):
        memory = record.payload.get("actor_memory")
        if record.operation_type == "proposal_decision" and record.payload["decision"] == "accepted" and memory and memory["actor"] == actor:
            return str(memory["artifact_digest"])
    return None


def read_memory(digest: str) -> dict[str, Any]:
    """Read the immutable actor memory bytes by digest."""
    import json

    import protocol_keys

    record = protocol_keys.artifact_store().read_object(digest)
    if record.get("redacted"):
        raise ValueError("The actor memory artifact is unavailable")
    return dict(json.loads(record["payload"]))


def store_memory(run_id: str, actor: str, value: dict[str, Any], *, previous_digest: str | None = None) -> str:
    """Promote one bounded memory revision with its run and actor binding."""
    import activation_service
    import protocol_keys
    from core.digest_profile import canonicalize

    record = {"schema_version": "actor-memory/1", "run_id": run_id, "actor": actor,
              "previous_digest": previous_digest, "memory": validate_memory(value)}
    return activation_service.persist_protected_artifact(protocol_keys.artifact_store(),
        canonicalize(record).encode(), media_type="application/json", access_policy="classic-actor-memory",
        referenced_by=f"{run_id}:{actor}")
