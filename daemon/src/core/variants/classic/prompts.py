"""The prompt policy of the Classic runtime.

The prompt policy renders the activation payload of one turn from the
registered persona template or the generated expert definition, the
board view the activation receives, the session fields, and the status
lines of the run. It reads every input from its arguments and touches
no storage or provider.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from budget_service import BudgetError

if TYPE_CHECKING:
    from core.variants.classic.roster import AgentRoster

RESPONSE_CONTRACT = "entries_v1"
EVIDENCE_STATUS = (
    "Evidence required: ground every new finding in an external "
    "source. List the URLs or tool citations in the entry's "
    "\"sources\" array. A round of unsourced restatement counts "
    "as a stall."
)
BUDGET_STATUS = (
    "Over 80% of the task budget is spent. Converge now: "
    "verify or finalize existing work instead of opening new work."
)
BUDGET_PRESSURE_SHARE = 0.8


class PromptPolicy:
    """Render the activation prompt from template, definition, and view."""

    @staticmethod
    def role_prompt(actor: str, roster: AgentRoster | None, query: str) -> str:
        """The persona text of one actor: a registered template or a generated expert."""
        from models.personas import ROLE_PERSONAS, generate_expert_persona

        base_role = actor.split(".")[0] if "." in actor else actor
        if actor.startswith("expert.") and roster:
            slug = actor.split(".", 1)[1]
            expert = next(
                (e for e in roster.experts if e.slug == slug), None
            )
            if expert:
                return generate_expert_persona(
                    expert.name, expert.ability, query,
                )
            return ROLE_PERSONAS.get(base_role, "")
        return ROLE_PERSONAS.get(actor, "")

    @staticmethod
    def render(
        *,
        task_id: str,
        query: str,
        actor: str,
        role_prompt: str,
        board_data: dict[str, Any],
        round_no: int,
        session: dict[str, Any],
        budget_remaining_usd: float,
        budget_ceiling: float,
        budget_spent: float,
        require_evidence: bool,
    ) -> dict[str, Any]:
        """Build the payload dispatched to a KS for this turn (doc 03 §4)."""
        base_role = actor.split(".")[0] if "." in actor else actor
        payload = {
            "task_id": task_id,
            "turn_id": f"turn-{uuid.uuid4().hex[:8]}",
            "round": round_no,
            "role": actor,
            "role_prompt": role_prompt,
            "objective": query,
            "board": board_data,
            "response_contract": RESPONSE_CONTRACT,
            "budget_remaining_usd": budget_remaining_usd,
            # Phase 5: stateful turns (doc 12 §5.2).
            **session,
        }
        if require_evidence and base_role in ("expert", "planner"):
            payload["evidence_status"] = EVIDENCE_STATUS
        if (
            budget_ceiling > 0
            and budget_spent / budget_ceiling >= BUDGET_PRESSURE_SHARE
        ):
            payload["budget_status"] = BUDGET_STATUS
        return payload


class PromptRenderError(BudgetError, ValueError):
    """The host cannot resolve an immutable prompt input."""


EXPERT_DEFINITION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "slug", "ability"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "slug": {"type": "string", "maxLength": 80, "pattern": "^[a-z][a-z0-9_]*$",
                 "not": {"pattern": "[^a-z0-9_]"}},
        "ability": {"type": "string", "minLength": 1, "maxLength": 2048},
    },
}


def parse_expert_definition(value: Any) -> dict[str, str]:
    """Parse the strict expert-definition schema without authority fields."""
    from jsonschema import Draft202012Validator

    errors = list(Draft202012Validator(EXPERT_DEFINITION_SCHEMA).iter_errors(value))
    if errors:
        raise ValueError(f"Invalid expert definition: {errors[0].message}")
    return dict(value)


def promote_input(value: Any, reference: str) -> str:
    """Store immutable JSON prompt inputs in the protected artifact store."""
    import json

    import activation_service
    import protocol_keys

    return activation_service.persist_protected_artifact(protocol_keys.artifact_store(),
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(), media_type="application/json",
        access_policy="classic-prompt-input", referenced_by=reference)


def read_input(digest: str) -> Any:
    """Read an immutable prompt input or reject an erased input."""
    import json

    import protocol_keys

    record = protocol_keys.artifact_store().read_object(digest)
    if record.get("redacted"):
        raise ValueError("The prompt input is unavailable")
    return json.loads(record["payload"])


async def render_native_request(request: dict[str, Any], *, run_id: str, activation_id: str,
                                attempt: int, task_fence: str, phase: str | None = None) -> dict[str, Any]:
    """Pin every input and store one host render receipt before dispatch."""
    import json

    import activation_service
    import database as db
    import runtime_journal as journal
    from content_boundary import build_agent_view, label_untrusted
    from core.digest_profile import plain_json
    from core.variants.classic.compiler import (
        PROMPT_RENDER_ENGINE_VERSION,
        TOKENIZER_ID,
        TOKENIZER_REVISION,
        load_specification,
        specification_store,
    )
    from core.variants.classic.memory import (
        current_memory_digest,
        isolated_request,
        read_memory,
        store_memory,
    )
    from core.variants.classic.proposals import proposal_schema

    existing = await db.get_classic_render_receipt(activation_id, attempt)
    if existing:
        if existing["run_id"] != run_id:
            raise PromptRenderError("The render receipt belongs to another run")
        return dict(read_input(str(existing["request_artifact_digest"])))
    stored = await db.get_classic_specification(run_id)
    if stored is None:
        raise PromptRenderError("The prompt requires its admitted specification")
    spec = load_specification(specification_store(), str(stored["artifact_digest"]))
    if spec.prompts.render_engine_version != PROMPT_RENDER_ENGINE_VERSION:
        raise PromptRenderError("The pinned prompt renderer is unavailable")
    if (next(iter(spec.models.tier_models.values())).tokenizer_id, next(iter(spec.models.tier_models.values())).tokenizer_revision) != (TOKENIZER_ID, TOKENIZER_REVISION):
        raise PromptRenderError("The pinned memory tokenizer is unavailable")
    result = isolated_request(request, activation_id, attempt)
    result.update(task_id=stored["task_id"], activation_id=activation_id)
    ceiling = result.get("max_completion_tokens", result.get("max_tokens", 4096))
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        raise BudgetError("The output token ceiling must be a positive integer")
    result.pop("max_tokens", None)
    result["max_completion_tokens"] = min(ceiling, spec.limits.max_output_tokens)
    context = dict(result["context"])
    actor = str(result.get("actor") or result.get("role") or phase or "control_unit")
    role = str(context.get("classic_proposal_role") or result.get("role") or "expert")
    if phase is None:
        context["classic_proposal_role"] = role
    template_id = role if phase is None else {
        "control_plane:ag": "expert_generator", "expert_generation": "expert_generator",
        "control_plane:cu": "control_unit", "control_plane:sole": "sole",
        "evaluation_judge": "evaluation_judge", "worker": "expert",
    }.get(phase, phase)
    if template_id in ("verifier", "judge"):
        template_id = "critic" if template_id == "verifier" else "sole"
    if template_id not in spec.prompts.static_template_digests:
        raise PromptRenderError(f"No pinned template exists for {template_id!r}")
    template_digest = spec.prompts.static_template_digests[template_id]
    template_record = specification_store().read_object(template_digest)
    if template_record.get("redacted"):
        raise PromptRenderError("The pinned template is unavailable")
    template = bytes(template_record["payload"]).decode()
    parameters = result.pop("prompt_parameters", {})
    if template_id == "expert_generator":
        template = template.format(n=parameters.get("n", spec.team.experts_by_tier["light"]))
    elif template_id == "control_unit":
        template = template.format(max_concurrent=parameters.get("max_concurrent", spec.coordination.max_parallel_agents))
    definition_digest = context.pop("expert_definition_digest", None)
    definition = context.pop("expert_definition", None)
    if role == "expert" and actor.startswith("expert.") and not definition and not definition_digest:
        metadata = await db.get_board_meta(str(stored["task_id"]))
        roster = metadata.get("roster") or {}
        if isinstance(roster, str):
            roster = json.loads(roster)
        expert = next((item for item in roster.get("experts", []) if f"expert.{item['slug']}" == actor), None)
        if expert is None:
            raise PromptRenderError("The activation requires its generated expert definition")
        definition_digest = expert.get("definition_digest")
        definition = {name: expert[name] for name in ("name", "slug", "ability")}
    segments = []
    if definition_digest:
        definition_record = read_input(str(definition_digest))
        definition = parse_expert_definition(definition_record["definition"])
    elif definition:
        definition = parse_expert_definition(definition)
        definition_digest = promote_input({"schema_version": "expert-definition/1", "definition": definition,
            "generator_receipt": None, "source_class": "agent_profile"}, activation_id)
    if definition:
        segments.append(label_untrusted(json.dumps(definition, ensure_ascii=True), "agent_profile"))
    memory_digest = None
    memory_view: dict[str, Any] = {}
    if spec.memory.actor_memory == "host_owned" and phase is None:
        prior_receipt = await db.get_classic_render_receipt(activation_id, attempt - 1) if attempt > 1 else None
        memory_digest = prior_receipt["memory_artifact_digest"] if prior_receipt else await current_memory_digest(run_id, actor)
        if memory_digest is None:
            memory_digest = store_memory(run_id, actor, {})
        memory_record = read_memory(memory_digest)
        if memory_record["run_id"] != run_id or memory_record["actor"] != actor:
            raise PromptRenderError("The actor memory belongs to another actor or run")
        memory_view = memory_record["memory"]
        segments.append(label_untrusted(json.dumps(memory_view, ensure_ascii=True, sort_keys=True, separators=(",", ":")), "peer_message"))
    memory_view_digest = promote_input(memory_view, activation_id)
    if phase is None:
        task_view = {"description": result.get("description", ""), "context": context}
        schema = proposal_schema(role)
    else:
        task_view = {"messages": [message for message in result.get("messages", []) if message.get("role") != "system"],
                     "parameters": parameters}
        schema = result.get("response_format", {})
        if template_id == "expert_generator":
            schema = {"type": "object", "additionalProperties": False, "required": ["experts"],
                      "properties": {"experts": {"type": "array", "minItems": 1, "maxItems": 12,
                                                   "items": EXPERT_DEFINITION_SCHEMA}}}
            template += "\nReturn only the expert data in this schema:\n" + json.dumps(schema)
    task_digest = promote_input(task_view, activation_id)
    schema_digest = promote_input(schema, activation_id)
    if phase is None:
        template += "\nReturn exactly one JSON proposal that matches this schema:\n" + json.dumps(schema)
        if memory_digest:
            template += "\nOptional memory_delta replaces supplied lists of working_notes, open_questions, and entry_ids."
    view = build_agent_view(agent_id=actor, capabilities=(), system_instructions=template, contents=segments)
    if phase is None:
        user_text = str(task_view["description"]) + "\n\n## Blackboard Context\n```json\n" + json.dumps(context) + "\n```"
    else:
        user_text = "\n".join(str(message.get("content", "")) for message in task_view["messages"])
        if template_id == "control_unit":
            user_text = build_agent_view(agent_id=actor, capabilities=(), system_instructions="",
                contents=[label_untrusted(user_text, "agent_profile")]).rendered()
    messages = [{"role": "system", "content": view.rendered()}, {"role": "user", "content": user_text}]
    prompt_digest = promote_input(messages, activation_id)
    renderer_digest = promote_input({"renderer_id": "classic-host-renderer", "version": PROMPT_RENDER_ENGINE_VERSION}, activation_id)
    sources = [segment.source_class for segment in segments]
    if template_id == "control_unit":
        sources.append("agent_profile")
    redaction = {"policy_version": "classic-prompt-redaction/1", "untrusted_sources": sources}
    receipt = {"schema_version": spec.prompts.activation_render_receipt_schema_version,
        "run_id": run_id, "activation_id": activation_id, "attempt": attempt, "actor": actor,
        "template_id": template_id, "template_version": spec.prompts.registry_version, "template_digest": template_digest,
        "definition_id": definition.get("slug") if definition else None, "definition_digest": definition_digest,
        "renderer_id": "classic-host-renderer", "renderer_version": PROMPT_RENDER_ENGINE_VERSION, "renderer_digest": renderer_digest,
        "task_view_digest": task_digest, "memory_artifact_digest": memory_digest, "memory_view_digest": memory_view_digest,
        "response_schema_digest": schema_digest, "prompt_digest": prompt_digest,
        "redaction_policy_version": redaction["policy_version"], "redaction_digest": promote_input(redaction, activation_id)}
    receipt_digest = promote_input(receipt, activation_id)
    context.update(render_receipt_digest=receipt_digest, memory_artifact_digest=memory_digest,
                   memory_view_digest=memory_view_digest, rendered_messages=messages, response_schema=schema)
    if spec.randomness.task_seed is not None:
        context["provider_seed"] = spec.randomness.task_seed
        if phase is not None:
            result["seed"] = spec.randomness.task_seed
    result.update(context=context, render_receipt_digest=receipt_digest)
    if phase is not None:
        result["messages"] = messages
    else:
        result["role_prompt"] = messages[0]["content"]
    row = {**receipt, "receipt_digest": receipt_digest, "request_artifact_digest": promote_input(result, activation_id)}
    identity = await activation_service.run_identity(run_id)
    async def write(connection: Any, cursor: int, now: str) -> None:
        await db.insert_classic_render_receipt(connection, row, cursor)
    await journal.commit_operation(journal.JournalOperation(
        operation_type="prompt_render", task_id=identity["task_id"], run_id=run_id,
        runtime_id=identity["runtime_id"], runtime_contract_version=identity["runtime_contract_version"],
        payload=plain_json(row), authority_type="host", task_fence=task_fence,
        idempotency_token=f"prompt-{activation_id}-{attempt}"), extra_writes=write)
    return result
