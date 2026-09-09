"""The Classic native pair.

``ClassicRuntime`` is the runtime of the native Classic pair. Work
package 1 registered it as a test-only pair that delegates every call
to the legacy engine through the same host call as the legacy adapter.
Work package 4 compiles every submission into one immutable
specification: the capture validates the fidelity profile and the
effort level, snapshots the deployment, compiles the specification
once to validate it, and stores the specification input in the
envelope. The admission compiles the input again with the asset
manifest digest, promotes the specification as an artifact, and binds
its digest. The resolved values project into the legacy settings so
the delegated engine honors the compiled limits and policies. Each
later work package replaces one delegated step with a native step.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import database as db
import runtime_journal as journal
from core.failpoints import failpoint
from core.foundation_gates import WriterDisabledError, require_writer_gates
from core.gateway import LeaseLostError
from core.runtime_services import AuthorityError
from core.variants import VariantConfigurationError
from core.variants.classic.adapter import ClassicHost, ClassicVariantRuntime
from core.variants.classic.compiler import (
    ClassicSpecError,
    StoredSpecification,
    compile_specification,
    legacy_settings_from_spec,
    specification_store,
    store_specification,
)
from core.variants.classic.outcomes import (
    CLASSIC_REASON_TABLE_VERSION,
    TERMINAL_OUTCOME_PRODUCER,
    classic_benchmark_reasons,
    classic_reason_registry,
    commit_terminal_outcome,
    reason_for_exception,
    reason_for_result,
)
from core.variants.classic.profiles import resolve_effort_level, resolve_fidelity
from core.variants.classic.projection import (
    BoardMutation,
    ClassicIntegrityError,
    RunCancelledError,
    RunDeadlineError,
    VerifiedCheckpoint,
    board_projection_digest,
    build_checkpoint,
    content_state_after,
    promote_text,
    read_checkpoint,
    replay_run_board,
)
from core.variants.classic.spec import (
    BoardSettings,
    ClassicSpecInput,
    DeploymentSnapshot,
    ModelProfileSnapshot,
    PriceSnapshot,
    TaskOverrideSet,
)
from core.variants.traditional import StepResult, TraditionalVariant

if TYPE_CHECKING:
    from core.run_context import RunContext
    from core.run_contracts import ReasonRegistry
    from core.runtime_services import ReferenceRuntimeServices
    from core.variants import (
        VariantExecutionRequest,
        VariantHost,
        VariantOutcome,
    )

logger = logging.getLogger("bmas.classic.runtime")

NATIVE_CONTRACT_VERSION = "2"
NATIVE_CONFIGURATION_SCHEMA_VERSION = "2"
# The writer gates the native pair checks before its first journal
# write. The admission checked the run context and the unit of work;
# the runtime checks the unit of work again and the trace envelope.
NATIVE_WRITER_GATES = ("runtime_unit_of_work", "trace_envelope")


@dataclass
class NativeRunBinding:
    """The unit-of-work binding of one native run.

    The binding holds the fenced run context, the runtime services the
    host built from the durable rows, and the board content state it
    commits against. It is the committer of the native board gateway:
    every mutation validates the live run-control row, promotes its
    bodies, and commits one ``proposal_decision`` with the projection
    rows in the same transaction. It also writes the run's one
    terminal outcome.
    """

    context: RunContext
    services: ReferenceRuntimeServices
    tenant_id: str = "tenant-default"
    board: dict[str, Any] = field(default_factory=journal.empty_board_state)
    phase: str = "start"
    reason_registry: ReasonRegistry | None = None
    outcome_record: journal.JournalRecord | None = None
    mutation_count: int = 0

    @property
    def run_id(self) -> str:
        return self.context.run_id

    @property
    def task_id(self) -> str:
        return self.context.task_id

    @property
    def task_fence(self) -> str:
        return self.context.task_fence

    @property
    def artifacts(self) -> Any:
        return self.services.artifacts

    def board_state(self) -> dict[str, Any]:
        return self.board

    def board_digest(self) -> str:
        return board_projection_digest(self.board)

    async def load_board(self) -> None:
        """Rebuild the board content state from the run's journal."""
        board, _run_state, _cursor = await replay_run_board(self.run_id)
        self.board = board

    async def authorize(self) -> dict[str, Any]:
        """Validate the live run-control row or raise the matching stop."""
        try:
            return await self.services.authority.authorize()
        except AuthorityError as exc:
            raise self._authority_stop(exc.reason) from exc

    @staticmethod
    def _authority_stop(reason: str) -> Exception:
        if reason == "cancelled":
            return RunCancelledError("The run was cancelled")
        if reason == "deadline":
            return RunDeadlineError("The run passed its deadline")
        if reason in ("clock_fault", "unknown_run"):
            return ClassicIntegrityError(f"The run authority is unusable: {reason}")
        return LeaseLostError(f"The run authority rejected the mutation: {reason}")

    async def commit(self, mutation: BoardMutation) -> journal.JournalRecord:
        """Commit one validated board mutation as one proposal decision."""
        await self.authorize()
        accepted = mutation.decision == "accepted"
        section = mutation.section if accepted else None
        if accepted and section is not None:
            for entry_id, body in mutation.bodies.items():
                promote_text(self.artifacts, body, referenced_by=f"{self.run_id}:{entry_id}",
                             cleaner=mutation.kind == "condensation")
            next_board = content_state_after(self.board, section, task_id=self.task_id)
        else:
            next_board = self.board
        digest = board_projection_digest(next_board)
        payload: dict[str, Any] = {
            "decision": mutation.decision,
            "proposal_digest": mutation.proposal_digest(),
            "execution_envelope_digest": mutation.envelope_digest(),
            "projection_changes": {"board_projection_digest": digest} if accepted else {},
            "checkpoint_digest": digest,
            "circuit_state": "closed",
            "circuit_decision": "allow",
            "activation_id": mutation.activation_id or "host",
            "activation_state": "proposal_recorded",
            "budget": {"reserved": 0, "consumed": 0},
            "trace_event": {
                "event": f"board.{mutation.kind}",
                "decision": mutation.decision,
                "entry_ids": mutation.entry_ids(),
            },
            "mutation": {
                "kind": mutation.kind,
                "actor": mutation.actor,
                "activation_id": mutation.activation_id,
                "round": int(mutation.round),
                "mutation_id": (section or {}).get("mutation_id") if section else mutation.proposal.get("mutation_id"),
            },
            "policy_set_digest": self.context.policy_set_digest,
            "specification_digest": self.context.effective_spec_digest,
        }
        if accepted and section is not None:
            payload["board"] = section
        else:
            payload["rejection"] = {"reason": mutation.reason or "rejected"}
        operation = journal.JournalOperation(
            operation_type="proposal_decision",
            task_id=self.task_id,
            run_id=self.run_id,
            runtime_id=self.context.runtime_key.runtime_id,
            runtime_contract_version=self.context.runtime_key.runtime_contract_version,
            payload=payload,
            idempotency_token=mutation.token,
            producer=TERMINAL_OUTCOME_PRODUCER,
            authority_type="runtime",
            correlation_id=mutation.activation_id,
            tenant_id=self.tenant_id,
            task_fence=self.task_fence,
        )
        try:
            if mutation.kind in ("model_proposal", "condensation"):
                import activation_service

                activation = await activation_service.get_activation(str(mutation.activation_id),
                    int(mutation.proposal["activation_attempt"]))
                if mutation.kind == "condensation":
                    payload["budget_reference"] = str(activation["reservation_id"])
                record = await activation_service.commit_proposal_decision(
                    run_id=self.run_id, activation_id=str(mutation.activation_id),
                    attempt=int(activation["attempt"]), decision=mutation.decision,
                    proposal_digest=str(activation["proposal_digest"]),
                    request_digest=str(activation["request_digest"]),
                    execution_envelope_digest=str(activation["execution_envelope_digest"]),
                    projection_changes=payload["projection_changes"], checkpoint_digest=digest,
                    decision_payload=payload,
                    projection_writer=self._projection_writer(section) if accepted else None,
                    task_fence=self.task_fence,
                    expected_projection_version=mutation.proposal.get("expected_projection_version"),
                )
            else:
                record = await journal.commit_operation(
                    operation, extra_writes=self._projection_writer(section) if accepted else None,
                )
        except journal.JournalFenceError as exc:
            raise LeaseLostError(f"The task fence is stale: {exc}") from exc
        except journal.JournalIntegrityError as exc:
            raise ClassicIntegrityError(str(exc)) from exc
        if accepted:
            self.board = next_board
        self.mutation_count += 1
        return record

    def _projection_writer(self, section: dict[str, Any] | None) -> Any:
        run_id = self.run_id
        task_id = self.task_id
        actor = str((section or {}).get("actor") or "")
        activation_id = (section or {}).get("activation_id")

        async def write(connection: Any, journal_cursor: int, now: str) -> None:
            if section is None:
                return
            rows = [
                {
                    **entry, "run_id": run_id, "task_id": task_id,
                    "created_cursor": journal_cursor, "journal_cursor": journal_cursor,
                    "created_at": now, "updated_at": now,
                }
                for entry in section.get("entries") or []
            ]
            cleaner = section.get("kind") == "condensation"
            if cleaner:
                failpoint("cleaner.before_summary_write")
            await db.insert_classic_board_projection_rows(connection, rows)
            if cleaner:
                failpoint("cleaner.after_summary_write")
            for change in section.get("status_changes") or []:
                await db.update_classic_board_projection_status(
                    connection, run_id=run_id, entry_id=str(change["entry_id"]),
                    status=str(change["status"]), journal_cursor=journal_cursor, updated_at=now,
                )
            tombstones = []
            for tombstone in section.get("tombstones") or []:
                if cleaner:
                    failpoint("cleaner.before_removed_status_write")
                await db.update_classic_board_projection_status(
                    connection, run_id=run_id, entry_id=str(tombstone["entry_id"]),
                    status=str(tombstone.get("status") or "removed"),
                    journal_cursor=journal_cursor, updated_at=now,
                )
                if cleaner:
                    failpoint("cleaner.after_removed_status_write")
                tombstones.append({
                    "run_id": run_id, "entry_id": str(tombstone["entry_id"]), "task_id": task_id,
                    "actor": actor, "activation_id": activation_id,
                    "reason": str(tombstone.get("reason") or ""),
                    "journal_cursor": journal_cursor, "removed_at": now,
                })
            for tombstone in tombstones:
                if cleaner:
                    failpoint("cleaner.before_tombstone_write")
                await db.insert_classic_board_tombstones(connection, [tombstone])
                if cleaner:
                    failpoint("cleaner.after_tombstone_write")

        return write

    async def checkpoint(self, control_meta: dict[str, Any]) -> dict[str, Any]:
        """Build the verified snapshot of the run at its current journal head."""
        board, run_state, last_cursor = await replay_run_board(self.run_id)
        if board_projection_digest(board) != self.board_digest():
            raise ClassicIntegrityError(
                "The board content state disagrees with the journal replay"
            )
        self.board = board
        return build_checkpoint(
            run_id=self.run_id,
            task_fence=self.task_fence,
            policy_set_digest=self.context.policy_set_digest,
            board=board,
            run_state=run_state,
            control_meta=control_meta,
            last_cursor=last_cursor,
        )

    async def verify_checkpoint(self, checkpoint: dict[str, Any]) -> VerifiedCheckpoint:
        """Verify one stored checkpoint under the live fence."""
        verified = await read_checkpoint(
            checkpoint, run_id=self.run_id, task_fence=self.task_fence,
        )
        self.board = verified.board
        return verified

    async def set_deadline(self, seconds: float) -> None:
        """Set the durable run deadline once, from the database clock."""
        from core.variants.classic.projection import deadline_after

        control = await self.services.run_controls.read()
        if control is None or control.get("deadline_at"):
            return
        now = await self.services.database_clock.now()
        await self.services.run_controls.set_deadline(deadline_after(now, seconds), "cancel")

    async def ensure_terminal_outcome(
        self,
        reason_code: str,
        *,
        final_references: tuple[str, ...] = (),
        resource_references: tuple[str, ...] = (),
        detail: dict[str, Any] | None = None,
    ) -> journal.JournalRecord:
        """Write the run's one terminal outcome, or return the written one."""
        if self.outcome_record is not None:
            return self.outcome_record
        if not resource_references:
            admission = await db.get_runtime_admission(self.run_id) or {}
            resource_references = tuple(
                str(admission[name]) for name in ("run_budget_id", "initial_reservation_id")
                if admission.get(name)
            )
        self.outcome_record = await commit_terminal_outcome(
            context=self.context,
            reason_code=reason_code,
            tenant_id=self.tenant_id,
            final_references=final_references,
            resource_references=resource_references,
            detail=detail,
            registry=self.reason_registry,
        )
        return self.outcome_record

    def outcome_reason_for_result(self, result: dict[str, Any]) -> str:
        return reason_for_result(result)

    def outcome_reason_for_exception(self, exc: BaseException) -> str | None:
        return reason_for_exception(exc, phase=self.phase)

    def promote_final_answer(self, answer: str) -> tuple[str, ...]:
        """Promote the final answer as the outcome's final reference."""
        if not answer:
            return ()
        return (promote_text(self.artifacts, answer, referenced_by=f"{self.run_id}:final-answer"),)


def _price_text(value: Any) -> str:
    """The decimal text of one configured per-token price."""
    if isinstance(value, (float, bool)):
        raise VariantConfigurationError("Native prices require decimal strings, never binary floating point")
    return str(value)


async def deployment_snapshot(qualification_ids: tuple[str, ...] = ()) -> DeploymentSnapshot:
    """Snapshot the deployment settings in force for one admission."""
    import config
    from config import (
        AGENT_ENDPOINTS,
        MODEL_POOLS,
        MODEL_PRICING,
        TRIAGE_MODEL,
    )
    from settings_store import get_store

    store = get_store()
    profiles = {
        str(alias): ModelProfileSnapshot(
            provider=str(profile.provider), model=str(profile.model),
            reasoning=str(getattr(profile, "reasoning", None) or "provider_default"),
        )
        for alias, profile in (getattr(config, "MODEL_PROFILES", None) or {}).items()
    }
    pricing = {
        str(alias): PriceSnapshot(
            input_cost_per_token=_price_text(price.get("input_cost_per_token", 0)),
            output_cost_per_token=_price_text(price.get("output_cost_per_token", 0)),
            source=str(price.get("source", "bmas.yaml")),
        )
        for alias, price in (MODEL_PRICING or {}).items()
        if "input_cost_per_token" in price and "output_cost_per_token" in price
    }
    board = ClassicVariantRuntime.board_settings()
    return DeploymentSnapshot(
        classic=await store.get_classic(),
        routing=await store.get_routing(),
        role_registry=await store.get_role_registry(),
        board=BoardSettings(**board),
        model_pools={str(tier): list(pool) for tier, pool in (MODEL_POOLS or {}).items()},
        model_profiles=profiles,
        model_pricing=pricing,
        triage_model=str(TRIAGE_MODEL),
        node_endpoints=sorted(set(AGENT_ENDPOINTS.values())),
        endpoint_capability_digests=_cached_capability_digests(),
        qualification_ids=sorted(qualification_ids),
    )


def _cached_capability_digests() -> dict[str, str]:
    """The capability document digests the dispatcher already fetched."""
    try:
        import agent_dispatch
    except ImportError:  # pragma: no cover - the daemon always ships it
        return {}
    cache = getattr(agent_dispatch, "_capability_cache", {})
    digests: dict[str, str] = {}
    for url, cached in cache.items():
        document = cached[1] if isinstance(cached, tuple) and len(cached) == 2 else None
        if document is not None:
            digests[str(url)] = document.digest()
    return digests


def specification_input_from(
    overrides: dict[str, Any] | None, deployment: DeploymentSnapshot,
) -> ClassicSpecInput:
    """Build the specification input of one submission."""
    overrides = dict(overrides or {})
    try:
        fidelity = resolve_fidelity(overrides.get("fidelity"))
        level, _preset = resolve_effort_level(overrides.get("effort"))
        task_overrides = TaskOverrideSet(
            classic=dict(overrides.get("classic") or {}),
            price_overrides=dict(overrides.get("price_overrides") or {}),
            routing=dict(overrides.get("routing") or {}),
            role_registry=copy.deepcopy(overrides.get("role_registry") or {}),
            seed=overrides.get("seed"),
        )
    except ValueError as exc:
        raise VariantConfigurationError(str(exc)) from exc
    return ClassicSpecInput(
        fidelity=fidelity, effort=level, deployment=deployment, task_overrides=task_overrides,
    )


class ClassicRuntime:
    """Run the Classic native pair through the legacy engine for now."""

    descriptor = dataclasses.replace(
        ClassicVariantRuntime.descriptor,
        label="Classic blackboard (native)",
        contract_version=NATIVE_CONTRACT_VERSION,
        configuration_schema_version=NATIVE_CONFIGURATION_SCHEMA_VERSION,
        # The bare identifier and the legacy alias stay bound to the
        # legacy pair. A submission reaches this pair only when it names
        # the exact contract version.
        aliases=(),
        supports_recovery=True,
        # The pair stays out of the public capability document until
        # work package 16 qualifies it.
        listed=False,
    )

    @classmethod
    async def capture_configuration(
        cls, overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile the submission once and store its specification input.

        The envelope keeps the legacy sections the engine reads, with
        the resolved values projected into ``settings.classic``, and
        adds the fidelity profile and the complete specification input.
        """
        from config import EDGE_NODE_MODELS, MODEL_POOLS, MODEL_PRICING
        from settings_store import validate_role_registry

        deployment = await deployment_snapshot()
        spec_input = specification_input_from(overrides, deployment)
        try:
            spec = compile_specification(spec_input)
        except ClassicSpecError as exc:
            raise VariantConfigurationError(f"Invalid classic specification: {exc}") from exc
        routing = dict(deployment.routing)
        routing.update(spec_input.task_overrides.routing)
        registry = {role: entry.model_dump() for role, entry in deployment.role_registry.items()}
        for role, patch in spec_input.task_overrides.role_registry.items():
            existing = copy.deepcopy(registry.get(role, {}))
            existing.update(patch)
            registry[role] = existing
        try:
            validate_role_registry(registry)
        except ValueError as exc:
            raise VariantConfigurationError(str(exc)) from exc
        return {
            "variant": cls.descriptor.id,
            "variant_contract_version": cls.descriptor.contract_version,
            "configuration_schema_version": cls.descriptor.configuration_schema_version,
            "effort": spec.effort.requested_level,
            "fidelity": spec.fidelity.profile_id,
            "settings": {
                "classic": legacy_settings_from_spec(spec),
                "board": deployment.board.model_dump(),
                "model_pools": copy.deepcopy(MODEL_POOLS),
                "model_pricing": copy.deepcopy(MODEL_PRICING),
                "edge_node_models": copy.deepcopy(EDGE_NODE_MODELS),
                "node_endpoints": list(deployment.node_endpoints),
            },
            "model_routing": routing,
            "role_registry": registry,
            "specification_input": spec_input.model_dump(mode="json"),
        }

    @classmethod
    def configuration_from_metadata(
        cls, metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load a saved native envelope.

        The native pair has no legacy metadata shape to migrate, so an
        envelope of another schema version fails closed.
        """
        saved = metadata.get("effective_configuration")
        if not isinstance(saved, dict):
            return None
        version = str(saved.get("configuration_schema_version") or "")
        if version != cls.descriptor.configuration_schema_version:
            raise VariantConfigurationError(
                "Unsupported native classic configuration schema version: "
                f"{version or 'missing'}"
            )
        if str(saved.get("variant") or "") != cls.descriptor.id:
            raise VariantConfigurationError(
                "The saved configuration variant does not match classic"
            )
        if not isinstance(saved.get("specification_input"), dict):
            raise VariantConfigurationError(
                "The saved native configuration carries no specification input"
            )
        return copy.deepcopy(saved)

    @classmethod
    def compile_specification_for_admission(
        cls,
        effective_configuration: dict[str, Any] | None,
        *,
        run_id: str,
        asset_manifest_digest: str,
        qualification_ids: tuple[str, ...] = (),
    ) -> StoredSpecification:
        """Compile the stored input and promote the specification artifact.

        The admission calls this once per new run. The input comes from
        the envelope the submission captured, so the compiled values
        equal the ones the submission validated, plus the asset
        manifest digest and the live qualification identifiers.
        """
        stored_input = (effective_configuration or {}).get("specification_input")
        if not isinstance(stored_input, dict):
            raise ClassicSpecError("The native pair needs a compiled specification input")
        try:
            spec_input = ClassicSpecInput.model_validate(stored_input)
        except ValueError as exc:
            raise ClassicSpecError(f"Invalid specification input: {exc}") from exc
        deployment = spec_input.deployment.model_copy(update={"qualification_ids": sorted(qualification_ids)})
        spec_input = spec_input.model_copy(update={
            "deployment": deployment, "asset_manifest_digest": asset_manifest_digest,
        })
        spec = compile_specification(spec_input)
        return store_specification(spec, store=specification_store(), referenced_by=run_id)

    # The host builds the fenced run context and the runtime services
    # from the durable admission rows for this pair.
    consumes_run_context = True

    @classmethod
    def reason_registry(cls) -> ReasonRegistry:
        """The reason registry that publishes the Classic reason table."""
        return classic_reason_registry()

    @classmethod
    def outcome_reasons(cls) -> dict[str, dict[str, str]]:
        """The benchmark reason table of the native pair."""
        return classic_benchmark_reasons()

    @classmethod
    def reason_table_version(cls) -> str:
        return CLASSIC_REASON_TABLE_VERSION

    @classmethod
    async def read_checkpoint(
        cls, checkpoint: dict[str, Any], *, run_id: str, task_fence: str,
    ) -> VerifiedCheckpoint:
        """The recovery reader: verify one native checkpoint before use."""
        return await read_checkpoint(checkpoint, run_id=run_id, task_fence=task_fence)

    @classmethod
    def bind_run(
        cls, request: VariantExecutionRequest, *, tenant_id: str = "tenant-default",
    ) -> NativeRunBinding | None:
        """Bind the run context and the services the host supplied.

        The writer gates are checked here, before the first native
        write. A request without a run context keeps the delegated
        path with no native write, because the admission was gated off.
        """
        context = request.run_context
        services = request.runtime_services
        if context is None or services is None:
            return None
        try:
            require_writer_gates(*NATIVE_WRITER_GATES)
        except WriterDisabledError as exc:
            raise VariantConfigurationError(
                f"The native classic pair cannot write its journal: {exc}"
            ) from exc
        return NativeRunBinding(
            context=context,
            services=services,
            tenant_id=tenant_id,
            reason_registry=cls.reason_registry(),
        )

    @classmethod
    async def run(
        cls, host: VariantHost, request: VariantExecutionRequest,
    ) -> VariantOutcome:
        """Run the coordination loop through the host under the run binding.

        The engine stays the legacy engine. The binding routes every
        board mutation through the unit of work, verifies the
        checkpoint at resume, and writes the terminal outcome.
        """
        classic_host = cast("ClassicHost", host)
        binding = cls.bind_run(request)
        return await classic_host.run_classic_runtime(
            request,
            engine_class=TraditionalVariant,
            step_result_class=StepResult,
            binding=binding,
        )
