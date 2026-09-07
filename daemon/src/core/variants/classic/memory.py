"""The context session policy of the Classic runtime.

The context session policy builds the provider session fields of one
activation and keeps the response identities that chain one actor's
turns. The engine checkpoints the identities. The memory policy that
selects durable goal, event, and episodic memory arrives with the actor
memory work package; this module holds no such behavior today.
"""
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
