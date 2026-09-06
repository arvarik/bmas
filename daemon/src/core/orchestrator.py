# /opt/bmas/daemon/src/core/orchestrator.py
"""Run shared task lifecycle services and registered coordination runtimes.

SQLite stores authoritative task state and durable events. Redis provides
leases, live projections, and low-latency event notifications.
"""

import asyncio
import contextlib
import json
import logging
import math
import random
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

import database as db
from config import (
    AGENT_ENDPOINT_MAX_CONCURRENCY,
    AGENT_ENDPOINT_WAIT_TIMEOUT_S,
    AGENT_ENDPOINTS,
    AGENT_TURN_TIMEOUT_S,
    BMAS_EXECUTE_KEY,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_RECOVERY_S,
    COORDINATION_VARIANT,
    LITELLM_KEY,
    LITELLM_URL,
    LOCK_TTL_MS,
    MODEL_PRICING,
    ROLE_REGISTRY,
    TRIAGE_URL,
)
from core.blackboard import Blackboard, normalize_level
from core.circuit_breaker import EndpointCircuitBreaker
from core.event_delivery import ensure_system_terminal_event, ensure_terminal_event
from core.gateway import LeaseLostError
from core.triage import MODEL_ROUTING, Complexity, TriageResult, TriageRouter
from core.variants import (
    RuntimeKey,
    UnknownVariantError,
    VariantConfigurationError,
    VariantExecutionRequest,
    VariantOutcome,
    canonical_variant_id,
    require_runtime,
    require_variant_class,
)
from file_utils import read_extracted_text

logger = logging.getLogger("bmas.orchestrator")


class LeaseBusyError(RuntimeError):
    """Another daemon currently owns the task execution lease."""


class EndpointOverloadedError(RuntimeError):
    """An agent endpoint did not provide a request slot in time."""


def _infer_level(message: str) -> str:
    """Infer a canonical log level from a legacy free-text message prefix."""
    head = (message or "").lstrip()[:12].lower()
    if head.startswith(("error", "err ", "fatal", "exception", "failed")):
        return "error"
    if head.startswith(("warn", "wrn")):
        return "warning"
    if head.startswith(("debug", "dbg")):
        return "debug"
    return "info"


def _summarize(text: str, limit: int = 280) -> str:
    """One-line preview for a log message header (full text kept in fields)."""
    if not text:
        return ""
    first = " ".join(str(text).split())
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Return a bounded Retry-After delay for a numeric header."""
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        return min(8.0, max(0.0, float(raw_value)))
    except (TypeError, ValueError):
        return None


def build_attachment_context(task_files: list[dict]) -> list[dict]:
    """Build the stable attachment contract that agent nodes receive."""
    return [
        {
            "file_id": file_row.get("id"),
            "name": file_row.get("name", "file"),
            "mime": file_row.get("mime", "application/octet-stream"),
            "bytes": file_row.get("bytes", 0),
            "sha256": file_row.get("sha256", ""),
            "text_preview": read_extracted_text(
                str(file_row.get("stored_path") or "")
            ),
        }
        for file_row in task_files
    ]


class Orchestrator:
    def __init__(self):
        self.bb = Blackboard()
        self.triage = TriageRouter(
            triage_url=TRIAGE_URL,
            litellm_url=LITELLM_URL,
            litellm_key=LITELLM_KEY,
        )
        self.http = httpx.AsyncClient(timeout=120.0)
        self._task_lock_ids: dict[str, str] = {}
        self._lease_lost: dict[str, asyncio.Event] = {}
        self._task_runtime_keys: dict[str, RuntimeKey] = {}
        self._active_gateways: dict[str, Any] = {}
        self._agent_circuits = EndpointCircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout_s=CIRCUIT_BREAKER_RECOVERY_S,
        )
        self._endpoint_slots: dict[str, asyncio.BoundedSemaphore] = {}
        self._endpoint_active: dict[str, int] = {}
        self._endpoint_waiting: dict[str, int] = {}

    def _circuits(self) -> EndpointCircuitBreaker:
        """Return the endpoint circuit registry.

        Some focused tests construct an orchestrator without calling the
        initializer. The lazy path keeps those tests representative.
        """
        circuits = getattr(self, "_agent_circuits", None)
        if circuits is None:
            circuits = EndpointCircuitBreaker(
                failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                recovery_timeout_s=CIRCUIT_BREAKER_RECOVERY_S,
            )
            self._agent_circuits = circuits
        return circuits

    def _endpoint_semaphore(self, endpoint: str) -> asyncio.BoundedSemaphore:
        """Return the bounded request semaphore for one endpoint."""
        slots = getattr(self, "_endpoint_slots", None)
        if slots is None:
            slots = {}
            self._endpoint_slots = slots
        semaphore = slots.get(endpoint)
        if semaphore is None:
            semaphore = asyncio.BoundedSemaphore(
                AGENT_ENDPOINT_MAX_CONCURRENCY
            )
            slots[endpoint] = semaphore
        return semaphore

    def runtime_snapshot(self) -> dict[str, Any]:
        """Return current task, endpoint, and circuit load."""
        active = getattr(self, "_endpoint_active", {})
        waiting = getattr(self, "_endpoint_waiting", {})
        circuits = self._circuits()
        endpoints = sorted(
            set(active) | set(waiting) | set(getattr(self, "_endpoint_slots", {}))
        )
        return {
            "active_tasks": len(getattr(self, "_task_lock_ids", {})),
            "endpoint_requests": {
                endpoint: {
                    "active": active.get(endpoint, 0),
                    "waiting": waiting.get(endpoint, 0),
                    "limit": AGENT_ENDPOINT_MAX_CONCURRENCY,
                    "circuit": circuits.status(endpoint),
                    "consecutive_failures": circuits.failures(endpoint),
                }
                for endpoint in endpoints
            },
        }

    async def _acquire_endpoint_slot(
        self, endpoint: str,
    ) -> asyncio.BoundedSemaphore:
        """Acquire one endpoint slot within the configured wait limit."""
        semaphore = self._endpoint_semaphore(endpoint)
        active = getattr(self, "_endpoint_active", None)
        if active is None:
            active = {}
            self._endpoint_active = active
        waiting = getattr(self, "_endpoint_waiting", None)
        if waiting is None:
            waiting = {}
            self._endpoint_waiting = waiting
        waiting[endpoint] = waiting.get(endpoint, 0) + 1
        try:
            if AGENT_ENDPOINT_WAIT_TIMEOUT_S == 0:
                if semaphore.locked():
                    raise EndpointOverloadedError(
                        f"Agent endpoint is at capacity: {endpoint}"
                    )
                await semaphore.acquire()
            else:
                try:
                    await asyncio.wait_for(
                        semaphore.acquire(),
                        timeout=AGENT_ENDPOINT_WAIT_TIMEOUT_S,
                    )
                except TimeoutError as exc:
                    raise EndpointOverloadedError(
                        "Agent endpoint capacity wait expired: "
                        f"{endpoint}"
                    ) from exc
        finally:
            waiting[endpoint] = max(0, waiting.get(endpoint, 1) - 1)
            if waiting[endpoint] == 0:
                waiting.pop(endpoint, None)
        active[endpoint] = active.get(endpoint, 0) + 1
        return semaphore

    def _release_endpoint_slot(
        self, endpoint: str, semaphore: asyncio.BoundedSemaphore,
    ) -> None:
        """Release one endpoint slot and update its request count."""
        active = getattr(self, "_endpoint_active", {})
        active[endpoint] = max(0, active.get(endpoint, 1) - 1)
        semaphore.release()
        if active[endpoint] == 0:
            active.pop(endpoint, None)
        waiting = getattr(self, "_endpoint_waiting", {})
        if not active.get(endpoint) and not waiting.get(endpoint):
            getattr(self, "_endpoint_slots", {}).pop(endpoint, None)

    async def _renew_task_lease(self, task_id: str, lock_id: str) -> None:
        """Renew one task lease until cancellation or ownership loss."""
        resource = f"orchestrator:{task_id}"
        interval_s = max(1.0, LOCK_TTL_MS / 3000.0)
        while True:
            await asyncio.sleep(interval_s)
            try:
                renewed = await self.bb.renew_lock(resource, lock_id)
            except Exception:
                renewed = False
            if renewed:
                try:
                    heartbeat_saved = await db.update_run_state(
                        task_id, "running", lease_token=lock_id,
                    )
                except Exception:
                    heartbeat_saved = False
                if not heartbeat_saved:
                    event = self._lease_lost.get(task_id)
                    if event is not None:
                        event.set()
                    logger.error("SQLite task lease lost for %s", task_id)
                    return
                continue
            event = self._lease_lost.get(task_id)
            if event is not None:
                event.set()
            logger.error("Task lease lost for %s", task_id)
            return

    async def _commit_allowed(self, task_id: str) -> bool:
        """Reject commits from a task owner that lost its Redis lease."""
        event = self._lease_lost.get(task_id)
        lock_id = self._task_lock_ids.get(task_id)
        if event is None or event.is_set() or not lock_id:
            return False
        try:
            return await self.bb.owns_lock(f"orchestrator:{task_id}", lock_id)
        except Exception:
            return False

    async def _safe_log(
        self,
        node_id: str,
        message: str,
        task_id: str | None = None,
        level: str | None = None,
        fields: dict | None = None,
        node: str | None = None,
        turn_id: str | None = None,
    ):
        """Log a structured entry to Redis Streams AND SQLite with fallback.

        Redis write provides live SSE streaming to the dashboard.
        SQLite write provides permanent archival for task history.
        Neither failure interrupts the caller.

        `level` is canonicalized (INFO/WARNING/ERROR/DEBUG). When omitted it is
        inferred from the message prefix so legacy "WARN:"/"ERROR ..." strings
        still surface with the right severity. `fields` carries arbitrary
        structured metadata (reasoning, tool calls, usage, routing rationale,
        board reads/writes, …) transported verbatim for the detail view.
        """
        resolved_level = normalize_level(level) if level else _infer_level(message)
        try:
            await self.bb.publish_log(
                node_id, message, task_id=task_id,
                level=resolved_level, fields=fields, node=node, turn_id=turn_id,
            )
        except Exception:
            logger.warning(f"Redis log failed | {node_id}: {message}")

        if task_id:
            try:
                await db.insert_log_entry(
                    task_id, node_id, resolved_level, message,
                    fields=fields, node=node, turn_id=turn_id,
                )
            except Exception:
                logger.warning(f"SQLite log failed | {task_id}: {message}")

    async def _set_phase(self, phase: str, iteration: int = 0, task_id: str | None = None):
        """Update the orchestrator phase in Redis and publish Pub/Sub event."""
        if task_id and phase != "idle":
            phase_saved = await db.update_task_phase(
                task_id,
                phase,
                lease_token=self._task_lock_ids.get(task_id),
            )
            if not phase_saved:
                raise LeaseLostError(f"Task lease expired: {task_id}")
        with contextlib.suppress(Exception):
            phases_key = "bmas:public:task_phases"
            if task_id:
                if phase == "idle":
                    await self.bb.redis.hdel(phases_key, task_id)
                else:
                    await self.bb.redis.hset(
                        phases_key,
                        task_id,
                        json.dumps({"phase": phase, "iteration": iteration}),
                    )
            active_count = await self.bb.redis.hlen(phases_key)
            public_phase = phase
            public_iteration = iteration
            if active_count and phase == "idle":
                raw_active = (await self.bb.redis.hvals(phases_key))[0]
                if isinstance(raw_active, bytes):
                    raw_active = raw_active.decode("utf-8")
                active_phase = json.loads(raw_active)
                public_phase = str(active_phase.get("phase", "running"))
                public_iteration = int(active_phase.get("iteration", 0))
            await self.bb.redis.hset("bmas:public:state", mapping={
                "phase": public_phase if active_count else "idle",
                "iteration": str(public_iteration if active_count else 0),
                "active_tasks": str(active_count),
            })

        if task_id:
            with contextlib.suppress(Exception):
                await self.bb.publish_event(task_id, "phase", {
                    "phase": phase, "iteration": iteration
                })

    async def publish_phase(
        self, phase: str, iteration: int, task_id: str,
    ) -> None:
        """Publish and persist one phase through the shared lifecycle."""
        await self._set_phase(phase, iteration, task_id)

    async def check_abort(self, task_id: str) -> None:
        """Stop execution when the task lost its lease or was cancelled."""
        await self._check_abort(task_id)

    async def log_event(
        self,
        node_id: str,
        message: str,
        task_id: str,
        **kwargs: Any,
    ) -> None:
        """Write one structured task log."""
        await self._safe_log(node_id, message, task_id, **kwargs)

    async def dispatch_agent(
        self,
        *,
        task_id: str,
        activation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Dispatch one activation through the fenced idempotent seam."""
        stable_id = str(activation_id or "").strip()
        if not stable_id:
            raise ValueError("An agent dispatch requires an activation_id")
        requested_turn_id = kwargs.pop("turn_id", None)
        if requested_turn_id is not None and str(requested_turn_id) != stable_id:
            raise ValueError(
                "The turn_id must match the stable activation_id"
            )
        kwargs["turn_id"] = stable_id
        return await self._dispatch_turn(
            task_id=task_id,
            activation_id=stable_id,
            **kwargs,
        )

    async def _assert_dispatch_lease(self, task_id: str) -> None:
        """Reject one external dispatch unless both task leases remain valid."""
        lock_id = self._task_lock_ids.get(task_id)
        lease_event = self._lease_lost.get(task_id)
        if not lock_id or lease_event is None or lease_event.is_set():
            raise LeaseLostError(f"Task lease expired: {task_id}")
        try:
            redis_owned, sqlite_owned = await asyncio.gather(
                self.bb.owns_lock(f"orchestrator:{task_id}", lock_id),
                db.owns_task_lease(task_id, lock_id),
            )
        except Exception as exc:
            lease_event.set()
            raise LeaseLostError(f"Task lease expired: {task_id}") from exc
        if not redis_owned or not sqlite_owned:
            lease_event.set()
            raise LeaseLostError(f"Task lease expired: {task_id}")

    async def publish_progress(
        self,
        task_id: str,
        label: str,
        status: str,
        items: list[dict[str, Any]],
    ) -> None:
        """Publish variant-defined progress items."""
        await self._publish_task_state(task_id, label, status, items)

    def task_lease_token(self, task_id: str) -> str | None:
        """Return the current fenced lifecycle token for one task."""
        return self._task_lock_ids.get(task_id)

    async def load_variant_checkpoint(
        self,
        task_id: str,
        variant_id: str,
    ) -> dict[str, Any] | None:
        """Load one checkpoint only when it matches the stored runtime pair.

        A checkpoint that names another runtime or another contract
        version raises an error. A silent fresh start would discard
        durable coordination state, so this boundary fails closed.
        """
        metadata = await db.get_board_meta(task_id)
        checkpoint = metadata.get("variant_checkpoint")
        if not isinstance(checkpoint, dict):
            return None
        if checkpoint.get("variant_id") != variant_id:
            raise VariantConfigurationError(
                f"The stored checkpoint of task {task_id} belongs to "
                f"runtime {checkpoint.get('variant_id')!r}, not {variant_id!r}"
            )
        expected = self._task_runtime_keys.get(task_id)
        recorded_version = str(
            checkpoint.get("runtime_contract_version")
            or checkpoint.get("contract_version")
            or ""
        )
        if (
            expected is not None
            and recorded_version
            and recorded_version != expected.runtime_contract_version
        ):
            raise VariantConfigurationError(
                f"The stored checkpoint of task {task_id} carries contract "
                f"version {recorded_version!r}; the stored runtime pair "
                f"requires {expected.runtime_contract_version!r}"
            )
        return checkpoint

    async def save_variant_checkpoint(
        self,
        task_id: str,
        variant_id: str,
        checkpoint: dict[str, Any],
    ) -> None:
        """Save one checkpoint and reject a stale runtime lease."""
        value = {**checkpoint, "variant_id": variant_id}
        expected = self._task_runtime_keys.get(task_id)
        if expected is not None:
            value["runtime_contract_version"] = (
                expected.runtime_contract_version
            )
        lease_token = self._task_lock_ids.get(task_id)
        await db.patch_board_meta(
            task_id,
            {"variant_checkpoint": value},
            lease_token=lease_token,
        )
        saved = await db.mark_task_checkpoint(task_id, lease_token)
        if not saved:
            raise LeaseLostError(f"Task lease expired: {task_id}")

    async def _check_abort(self, task_id: str):
        """Check if the operator requested an abort for this task.

        Reads `bmas:public:abort:{task_id}` from Redis. If set, raises
        RuntimeError which is caught by the process_task exception handler,
        marking the task as failed with an explicit abort message.
        """
        lease_event = self._lease_lost.get(task_id)
        if lease_event is not None and lease_event.is_set():
            raise RuntimeError("Task lease expired")
        try:
            abort_key = f"bmas:public:abort:{task_id}"
            val = await self.bb.redis.get(abort_key)
            if val:
                await self.bb.redis.delete(abort_key)
                raise RuntimeError("Task aborted by operator")
        except RuntimeError:
            raise  # Re-raise the abort — don't swallow it
        except Exception:
            pass  # Redis read failure is non-fatal

    async def _publish_task_state(self, task_id: str, label: str, status: str,
                                  sub_tasks: list[dict] | None = None):
        """Write task state to Redis (real-time) AND sub-tasks to SQLite (persistent)."""
        now = datetime.now(UTC).isoformat()
        task_data = {
            "id": task_id,
            "label": label,
            "description": label,
            "status": status,
            "sub_tasks": sub_tasks or [],
            "created_at": now,
            "updated_at": now,
        }
        await self.bb.publish_task(task_id, task_data)

        # Save durable subtask state after the live projection update.
        if sub_tasks:
            try:
                await db.upsert_sub_tasks(task_id, sub_tasks)
            except Exception:
                logger.warning(f"SQLite sub-task upsert failed for {task_id}")

            # Publish sub-task status changes via Pub/Sub
            for st in sub_tasks:
                with contextlib.suppress(Exception):
                    await self.bb.publish_event(task_id, "subtask", {
                        "id": st["id"],
                        "label": st.get("label", ""),
                        "status": st.get("status", "pending"),
                        "agent_role": st.get("agent_role", "unknown"),
                    })

    async def process_task(
        self,
        user_task: str,
        task_id: str | None = None,
        *,
        overrides: dict | None = None,
        variant_id: str | None = None,
        effective_configuration: dict[str, Any] | None = None,
        resume: bool = False,
    ) -> dict:
        """Run shared triage and the selected coordination runtime.

        Args:
            user_task: The raw user task description.
            task_id: Optional pre-assigned task ID (created by submit endpoint).
            overrides: Optional per-task settings overrides.
                The submit route persists them for restart recovery.
                Keys: 'routing' (dict[tier, model]), 'role_registry' (dict[role, entry]).
        """
        session_id = str(uuid.uuid4())[:8]
        if task_id is None:
            task_id = f"task-{session_id}"

        generated_task = task_id == f"task-{session_id}"
        selected_variant = canonical_variant_id(
            variant_id or COORDINATION_VARIANT
        )

        # 1. Acquire the Redis lease, then fence SQLite with the same token.
        acquired, lock_id = await self.bb.acquire_lock(f"orchestrator:{task_id}")
        if not acquired:
            raise LeaseBusyError(f"Task lease is busy: {task_id}")

        renewal_task: asyncio.Task | None = None
        lease_claimed = False

        try:
            # Create persistent task record (SQLite)
            # Only create if we generated the ID here.
            # If task_id was passed, it was already created by the async submit endpoint.
            if generated_task:
                generated_variant_class = require_variant_class(
                    selected_variant
                )
                if effective_configuration is None:
                    effective_configuration = (
                        await generated_variant_class.capture_configuration(
                            overrides
                        )
                    )
                await db.create_task_with_meta(
                    task_id,
                    user_task[:80],
                    user_task,
                    selected_variant,
                    {"effective_configuration": effective_configuration},
                    runtime_contract_version=(
                        generated_variant_class.descriptor.contract_version
                    ),
                )

            lease_claimed = await db.claim_task_lease(task_id, lock_id)
            if not lease_claimed:
                raise LeaseLostError(
                    f"Task cannot claim its SQLite lease: {task_id}"
                )
            self._task_lock_ids[task_id] = lock_id
            self._lease_lost[task_id] = asyncio.Event()
            renewal_task = asyncio.create_task(
                self._renew_task_lease(task_id, lock_id),
                name=f"bmas-lease-{task_id}",
            )

            with contextlib.suppress(Exception):
                await self.bb.publish_system_event("task-started", {
                    "task_id": task_id, "label": user_task[:80]
                })

            await self._set_phase("triage", 1, task_id=task_id)
            await self._safe_log("daemon", f"Processing: {task_id}", task_id=task_id)

            # Log per-task overrides if provided
            if overrides:
                await self._safe_log("daemon",
                    f"Per-task overrides applied: {list(overrides.keys())}",
                    task_id=task_id, level="info",
                    fields={"event": "task_overrides", "overrides": overrides})

            row = await db.get_task(task_id)
            if row is None:
                raise RuntimeError(f"Cannot execute missing task: {task_id}")
            stored_variant = canonical_variant_id(
                str(row.get("variant") or selected_variant)
            )
            if stored_variant != selected_variant and variant_id is not None:
                raise RuntimeError(
                    "The requested variant does not match the stored task variant"
                )
            selected_variant = stored_variant
            stored_contract_version = str(
                row.get("runtime_contract_version") or ""
            ).strip()
            if not stored_contract_version:
                raise VariantConfigurationError(
                    f"Task {task_id} stores no runtime contract version"
                )
            runtime_key = RuntimeKey(selected_variant, stored_contract_version)
            try:
                variant_class = require_runtime(runtime_key)
            except UnknownVariantError as exc:
                raise VariantConfigurationError(str(exc)) from exc
            self._task_runtime_keys[task_id] = runtime_key

            persisted_meta = await db.get_board_meta(task_id)
            stored_configuration = variant_class.configuration_from_metadata(
                persisted_meta
            )
            if stored_configuration is not None:
                effective_configuration = stored_configuration
                if not isinstance(
                    persisted_meta.get("effective_configuration"), dict
                ):
                    await db.upsert_board_meta(
                        task_id,
                        {"effective_configuration": effective_configuration},
                    )
            elif effective_configuration is None:
                effective_configuration = await variant_class.capture_configuration(
                    overrides
                )
                await db.upsert_board_meta(
                    task_id,
                    {"effective_configuration": effective_configuration},
                )
            else:
                effective_configuration = (
                    variant_class.configuration_from_metadata(
                        {"effective_configuration": effective_configuration}
                    )
                )

            # Admit the task into its Foundation run before the runtime
            # executes. With the writer gates off the task keeps the
            # legacy path and the agent dispatch uses the bearer route.
            await self._admit_foundation_run(
                task_id, runtime_key, effective_configuration, overrides,
            )

            if resume:
                try:
                    complexity = Complexity(str(row.get("complexity") or "medium"))
                except ValueError:
                    complexity = Complexity.MEDIUM
                triage = TriageResult(
                    complexity=complexity,
                    litellm_model=str(row.get("model_used") or "medium"),
                )
                if not await db.update_task_status(
                    task_id, status="running", lease_token=lock_id,
                ):
                    raise LeaseLostError(f"Task lease expired: {task_id}")
                if not await db.update_run_state(
                    task_id, "running", lease_token=lock_id,
                ):
                    raise LeaseLostError(f"Task lease expired: {task_id}")
                await self._safe_log(
                    "daemon",
                    "Resuming task from its durable coordination checkpoint",
                    task_id=task_id,
                    fields={
                        "event": "task_resumed",
                        "variant": selected_variant,
                    },
                )
                return await self._run_variant(
                    selected_variant,
                    VariantExecutionRequest(
                        task_id=task_id,
                        session_id=session_id,
                        user_task=user_task,
                        triage=triage,
                        overrides=overrides,
                        resume=True,
                        effective_configuration=effective_configuration,
                    ),
                )

            # 2. Triage complexity
            # Build effective routing: session overrides merged with per-task overrides
            effective_routing = dict(
                (effective_configuration or {}).get("model_routing") or {}
            )

            try:
                triage = await self.triage.classify(user_task, routing_override=effective_routing)
            except Exception as e:
                await self._safe_log("daemon",
                    f"WARN: Triage unavailable ({e}), defaulting to MEDIUM", task_id=task_id)
                triage = TriageResult(
                    complexity=Complexity.MEDIUM,
                    litellm_model=effective_routing.get("medium", MODEL_ROUTING.get(Complexity.MEDIUM, "medium")),
                )
            await self._safe_log("daemon",
                f"Triage: {triage.complexity.value} → {triage.litellm_model}", task_id=task_id,
                level="info",
                fields={
                    "event": "triage",
                    "complexity": triage.complexity.value,
                    "model": triage.litellm_model,
                })

            # Update task with triage result + active variant (SQLite)
            try:
                updated = await db.update_task_status(
                    task_id,
                    status="running",
                    complexity=triage.complexity.value,
                    model_used=triage.litellm_model,
                    lease_token=lock_id,
                )
                if not updated:
                    raise LeaseLostError(f"Task lease expired: {task_id}")
            except Exception as e:
                if isinstance(e, LeaseLostError):
                    raise
                logger.warning(f"SQLite update_task_status failed for {task_id}: {e}")

            # 3. Run the blackboard coordination loop
            return await self._run_variant(
                selected_variant,
                VariantExecutionRequest(
                    task_id=task_id,
                    session_id=session_id,
                    user_task=user_task,
                    triage=triage,
                    overrides=overrides,
                    resume=False,
                    effective_configuration=effective_configuration,
                ),
            )

        except (LeaseLostError, db.LeaseFenceError) as exc:
            raise LeaseLostError(f"Task lease expired: {task_id}") from exc
        except asyncio.CancelledError:
            if lease_claimed:
                with contextlib.suppress(Exception):
                    await db.update_run_state(
                        task_id, "recovering", lease_token=lock_id,
                    )
            raise
        except VariantConfigurationError:
            if lease_claimed:
                with contextlib.suppress(Exception):
                    await db.update_run_state(
                        task_id, "blocked", lease_token=lock_id,
                    )
            raise
        except Exception as e:
            # Record failure in SQLite before re-raising
            failed = await self._fail_task_with_cost(
                task_id,
                str(e),
                lease_token=lock_id if lease_claimed else None,
            )

            if not failed:
                raise

            with contextlib.suppress(Exception):
                terminal_task = await db.get_task(task_id)
                if terminal_task is not None:
                    await ensure_terminal_event(self.bb.redis, terminal_task)
                    await ensure_system_terminal_event(
                        self.bb.redis, terminal_task,
                    )

            with contextlib.suppress(Exception):
                await self._publish_task_state(
                    task_id, user_task[:80], "failed",
                )

            raise

        finally:
            if renewal_task is not None:
                renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)
            await self._set_phase("idle", 0, task_id=task_id)
            with contextlib.suppress(Exception):
                await self.bb.release_lock(f"orchestrator:{task_id}", lock_id)
            if lease_claimed:
                with contextlib.suppress(Exception):
                    await db.release_task_lease(task_id, lock_id)
            self._task_lock_ids.pop(task_id, None)
            self._lease_lost.pop(task_id, None)
            self._task_runtime_keys.pop(task_id, None)

    async def rollup_task_cost(
        self,
        task_id: str,
        lease_token: str | None = None,
    ) -> bool:
        """Save current task cost totals without blocking termination."""
        try:
            rolled_up = await db.update_task_cost_totals(
                task_id,
                lease_token=lease_token,
            )
        except Exception:
            logger.warning(
                "Final cost rollup failed for task %s",
                task_id,
                exc_info=True,
            )
            return False
        if not rolled_up:
            logger.warning("Final cost rollup did not update task %s", task_id)
        return rolled_up

    async def _fail_task_with_cost(
        self,
        task_id: str,
        error_message: str,
        lease_token: str | None = None,
    ) -> bool:
        """Roll up partial costs before one fenced failure transition."""
        await self.rollup_task_cost(task_id, lease_token)
        try:
            return await db.fail_task(
                task_id,
                error_message,
                lease_token=lease_token,
            )
        except Exception:
            logger.warning(
                "SQLite fail_task failed for %s",
                task_id,
                exc_info=True,
            )
            return False

    async def _run_variant(
        self,
        variant_id: str,
        request: VariantExecutionRequest,
    ) -> dict[str, Any]:
        """Run the registered coordination runtime for one task."""
        variant_class = require_variant_class(variant_id)
        outcome = await variant_class.run(self, request)
        if not isinstance(outcome, VariantOutcome):
            raise TypeError(
                f"Variant '{variant_id}' returned an invalid outcome"
            )
        if outcome.variant_id != variant_id:
            raise ValueError(
                "The variant outcome identifier does not match the task"
            )
        await self._complete_variant_task(request, outcome)
        return outcome.public_result

    async def _checkpoint_variant(self, variant: Any, task_id: str) -> None:
        """Save one variant checkpoint and record its durable boundary."""
        await variant.checkpoint(task_id)
        saved = await db.mark_task_checkpoint(
            task_id,
            self._task_lock_ids.get(task_id),
        )
        if not saved:
            raise LeaseLostError(f"Task lease expired: {task_id}")

    # ── Classic Variant Host Services ────────────────────────────────

    async def run_classic_runtime(
        self,
        request: VariantExecutionRequest,
        *,
        engine_class: type,
        step_result_class: type,
    ) -> VariantOutcome:
        """Run the paper's cyclic blackboard loop (doc 05).

        The orchestrator owns lifecycle (lock, abort, events, SQLite).
        The TraditionalVariant owns the loop (genesis, step, finalize).
        CU and AG calls are control-plane LiteLLM calls, never Hermes runs.

        Args:
            overrides: Optional per-task overrides dict with keys:
                'routing' (dict[str, str]) and/or 'role_registry' (dict[str, dict]).
                These are merged on top of the session settings_store values.
        """
        from core.board_store import SqliteRedisBoardStore, make_board_persist_hook
        from core.event_emitter import RedisEventEmitter
        from core.gateway import BoardGateway, salience_recompute_hook

        task_id = request.task_id
        user_task = request.user_task
        triage = request.triage
        resume = request.resume

        if not resume:
            await self.publish_progress(
                task_id,
                user_task[:80],
                "running",
                [
                    {
                        "id": f"{task_id}-triage",
                        "label": f"Triage: {triage.complexity.value}",
                        "status": "completed",
                        "agent_role": "planner",
                        "depends_on": [],
                    },
                    {
                        "id": f"{task_id}-plan",
                        "label": "Plan decomposition",
                        "status": "pending",
                        "agent_role": "planner",
                        "depends_on": [f"{task_id}-triage"],
                    },
                    {
                        "id": f"{task_id}-exec",
                        "label": "Execute sub-tasks",
                        "status": "pending",
                        "agent_role": "executor",
                        "depends_on": [f"{task_id}-plan"],
                    },
                    {
                        "id": f"{task_id}-audit",
                        "label": "Audit and consensus",
                        "status": "pending",
                        "agent_role": "auditor",
                        "depends_on": [f"{task_id}-exec"],
                    },
                ],
            )

        await self._safe_log("daemon",
            f"Classic variant | tier={triage.complexity.value}", task_id=task_id)

        # Boot board infrastructure
        # Use RedisEventEmitter so board_entry / entry_removed SSE events
        # flow through Redis Pub/Sub → SSE endpoint → frontend.
        lease_token = self._task_lock_ids.get(task_id)
        board_store = SqliteRedisBoardStore(lease_token=lease_token)
        await board_store.load_task(task_id)
        event_emitter = RedisEventEmitter(self.bb.redis)
        # Durable persistence: mirror the in-process snapshot into Redis
        # (no TTL) after every commit so the board survives for the life
        # of the task and is retained for completed tasks.
        gateway = BoardGateway(
            board_store, event_emitter,
            recompute_hooks=[
                salience_recompute_hook,
                make_board_persist_hook(self.bb),
            ],
            commit_guard=self._commit_allowed,
        )
        events = await board_store.get_events(task_id)
        if resume and not events:
            with contextlib.suppress(Exception):
                legacy = await self.bb.get_board_snapshot(task_id)
                if legacy.get("entries"):
                    imported = await board_store.import_snapshot(
                        task_id,
                        list(legacy["entries"]),
                        dict(legacy.get("meta") or {}),
                    )
                    if imported:
                        await self._safe_log(
                            "daemon",
                            f"Imported {imported} legacy Redis board entries",
                            task_id=task_id,
                            fields={"event": "legacy_board_import"},
                        )
                        events = await board_store.get_events(task_id)

        persisted_meta = await board_store.get_meta(task_id)
        effective_configuration = request.effective_configuration or {}
        effective_task_config = dict(
            effective_configuration.get("settings") or {}
        )
        effective_routing = dict(
            effective_configuration.get("model_routing") or {}
        )
        effective_registry = dict(
            effective_configuration.get("role_registry") or {}
        )
        saved_model_pricing = effective_task_config.get("model_pricing")
        effective_model_pricing = dict(
            saved_model_pricing
            if isinstance(saved_model_pricing, dict)
            else MODEL_PRICING
        )

        variant = engine_class(
            gateway=gateway,
            board_store=board_store,
            event_emitter=event_emitter,
            triage=self.triage,
            config=dict(
                effective_task_config.get("classic")
                or effective_task_config.get("traditional")
                or {}
            ),
            litellm_url=LITELLM_URL,
            litellm_key=LITELLM_KEY,
            node_endpoints=list(effective_task_config.get("node_endpoints") or []),
            role_registry=effective_registry,
            model_routing=effective_routing,
            model_pools=dict(effective_task_config.get("model_pools") or {}),
            edge_node_models=list(
                effective_task_config.get("edge_node_models") or []
            ),
            model_pricing=effective_model_pricing,
        )
        self._active_gateways[task_id] = gateway

        try:
            # ── Genesis ──────────────────────────────────────────────
            await self._set_phase("genesis", 0, task_id=task_id)

            # Get file attachments for context (doc 17 §4)
            attachments = []
            try:
                from config import STORAGE_ENABLED
                if STORAGE_ENABLED:
                    task_files = await db.get_task_files(task_id)
                    if task_files:
                        attachments = build_attachment_context(task_files)
            except Exception as e:
                logger.warning(f"Failed to get attachments for {task_id}: {e}")

            task = {
                "task_id": task_id,
                "query": user_task,
                "triage_result": triage,
                "attachments": attachments,
            }
            await gateway.set_meta(
                task_id,
                effective_configuration=effective_configuration,
                effective_task_config=effective_task_config,
                effective_routing=effective_routing,
                effective_registry=effective_registry,
            )

            if (
                resume
                and events
                and variant.genesis_checkpoint_complete(persisted_meta)
            ):
                await variant.resume(task)
                await gateway.refresh(task_id)
            else:
                await variant.genesis(task)

            persisted_meta = await board_store.get_meta(task_id)
            if (
                resume
                and persisted_meta.get("phase") == "Solved"
                and isinstance(persisted_meta.get("final_answer"), str)
            ):
                result = {
                    "answer": persisted_meta["final_answer"],
                    "terminated_by": persisted_meta.get("terminated_by", "recovered"),
                    "answer_source": persisted_meta.get("answer_source", "recovered"),
                    "verification_status": persisted_meta.get(
                        "verification_status", "unverified",
                    ),
                    "rounds_completed": int(persisted_meta.get("round", 0)),
                    "budget_spent": variant.budget_spent,
                }
                return self._classic_outcome(
                    task_id, triage, result, variant.budget_spent,
                )

            roster_actors = variant.roster.all_actors() if variant.roster else []
            await self._safe_log("daemon",
                f"Genesis complete | roster={len(roster_actors)} agents",
                task_id=task_id, level="info",
                fields={
                    "event": "genesis",
                    "roster": [
                        {"actor": a, "ability": d} for a, d in roster_actors
                    ],
                    "max_rounds": variant.max_rounds,
                    "budget_ceiling_usd": variant.budget_ceiling,
                })

            # ── Round loop ───────────────────────────────────────────
            loop_meta = await board_store.get_meta(task_id)
            active_round = loop_meta.get("round_state")
            if (
                isinstance(active_round, dict)
                and active_round.get("status") == "active"
            ):
                start_round = int(active_round.get("round", loop_meta.get("round", 0)))
            else:
                start_round = int(loop_meta.get("round", 0)) + 1
            for round_no in range(start_round, variant.max_rounds + 4):
                await self._check_abort(task_id)

                # Phase 5: Inject operator directives (doc 05 §6)
                await variant.inject_directives(task_id)

                # Phase 5: Check pause-at-round-boundary (doc 05 §6)
                await variant.check_pause(task_id)

                await self._set_phase("round", round_no, task_id=task_id)

                board = await board_store.get_snapshot(task_id)
                step_result = await variant.restore_active_round(task_id)
                if step_result is None:
                    step_result = await variant.step(task, board)

                if step_result.terminal:
                    await self._safe_log("daemon",
                        f"Terminal at round {round_no}: {step_result.reason}",
                        task_id=task_id, level="info",
                        fields={
                            "event": "terminal",
                            "round": round_no,
                            "reason": step_result.reason,
                        })
                    break

                # Coordinator routing decision: log WHO was selected and WHY,
                # attributed to the control unit so the rationale is auditable.
                await self._safe_log(
                    "control_unit",
                    f"Round {round_no} routing → {', '.join(step_result.selected) or 'none'}"
                    + (f" ({step_result.selection_source})" if step_result.selection_source else ""),
                    task_id=task_id, level="info",
                    fields={
                        "event": "routing_decision",
                        "round": round_no,
                        "selected": step_result.selected,
                        "source": step_result.selection_source,
                        "rationale": step_result.rationale,
                        "phase": step_result.phase,
                    },
                )

                # Dispatch activations — decider runs AFTER all others
                # so it can see the critic's board writes (doc 05 §1.1).
                if step_result.activations:
                    # Phase 0: Intercept conflict_resolver if there are open conflicts
                    conflict_activations = [a for a in step_result.activations if a.actor == "conflict_resolver"]
                    open_conflicts = [e for e in board.values() if e.type == "conflict" and e.status == "open"]
                    
                    if conflict_activations and open_conflicts:
                        logger.info("Conflict resolver selected with open conflicts — triggering private debate")
                        conflict_entry = sorted(open_conflicts, key=lambda e: getattr(e, 'round', 0))[0]
                        try:
                            await variant.handle_conflict_resolution(
                                task, conflict_entry, self._dispatch_traditional_turn
                            )
                            activation_id = conflict_activations[0].activation_id or ""
                            await variant.mark_activation_complete(
                                task_id, activation_id, "completed",
                            )
                        except Exception as e:
                            logger.error(f"Error during private conflict resolution: {e}")
                        # Remove conflict_resolver from activations since we handled the mediation
                        step_result.activations = [a for a in step_result.activations if a.actor != "conflict_resolver"]

                if step_result.activations:
                    # Split into non-decider and decider groups
                    non_decider = [a for a in step_result.activations
                                   if a.actor != "decider"]
                    decider = [a for a in step_result.activations
                               if a.actor == "decider"]

                    all_activations = []
                    all_results = []

                    # Phase 1: dispatch non-decider agents using the configured mode.
                    if non_decider:
                        phase1_results = await self._dispatch_traditional_group(
                            variant,
                            task,
                            non_decider,
                            round_no,
                            rationale=step_result.rationale,
                            phase=step_result.phase,
                        )
                        all_activations.extend(non_decider)
                        all_results.extend(phase1_results)

                    # Phase 2: dispatch decider AFTER non-decider agents finish
                    if decider:
                        if non_decider:
                            logger.info(
                                "Decider deferred until after %d non-decider agents | task=%s round=%d",
                                len(non_decider), task_id, round_no,
                            )
                        phase2_results = await self._dispatch_traditional_group(
                            variant,
                            task,
                            decider,
                            round_no,
                            rationale=step_result.rationale,
                            phase=step_result.phase,
                        )
                        all_activations.extend(decider)
                        all_results.extend(phase2_results)

                    # Process results and track cost
                    for activation, result in zip(all_activations, all_results, strict=False):
                        if isinstance(result, Exception):
                            logger.warning(
                                f"Turn failed for {activation.actor}: {result}"
                            )
                            continue
                    await self._safe_log("daemon",
                        f"Round {round_no} complete | "
                        f"{len(step_result.activations)} turns, "
                        f"budget=${variant.budget_spent:.4f}",
                        task_id=task_id, level="info",
                        fields={
                            "event": "round_complete",
                            "round": round_no,
                            "turns": len(step_result.activations),
                            "actors": [a.actor for a in step_result.activations],
                            "budget_spent_usd": round(variant.budget_spent, 6),
                            "budget_ceiling_usd": variant.budget_ceiling,
                        })

                # Phase 5: Emit budget event after each round (doc 09 §5)
                await variant.finish_round(task_id)
                await variant.emit_budget_event(task_id)
                await self._checkpoint_variant(variant, task_id)
            else:
                step_result = step_result_class(
                    terminal=True, reason="max_rounds"
                )

            # ── Finalize ─────────────────────────────────────────────
            await self._set_phase("finalize", 0, task_id=task_id)
            board = await board_store.get_snapshot(task_id)
            result = await variant.finalize(
                task, board, step_result.reason or "unknown",
            )
            await self._checkpoint_variant(variant, task_id)

            # Persist the terminal snapshot + meta durably (no TTL) so the
            # completed board (incl. final phase/answer_source) is retained.
            with contextlib.suppress(Exception):
                from core.entry import entry_to_dict
                final_snap = await board_store.get_snapshot(task_id)
                final_meta = await board_store.get_meta(task_id)
                await self.bb.save_board_snapshot(
                    task_id,
                    {eid: entry_to_dict(e) for eid, e in final_snap.items()},
                    final_meta,
                )

            return self._classic_outcome(
                task_id, triage, result, variant.budget_spent,
            )

        finally:
            self._active_gateways.pop(task_id, None)
            await variant.close()
            with contextlib.suppress(Exception):
                await gateway.unload_task(task_id)

    async def _complete_variant_task(
        self,
        request: VariantExecutionRequest,
        outcome: VariantOutcome,
    ) -> None:
        """Persist and publish one completed coordination task."""
        task_id = request.task_id
        user_task = request.user_task
        result = outcome.result
        answer = outcome.answer
        lease_token = self._task_lock_ids.get(task_id)
        reported_cost = float(outcome.cost_usd)
        if not math.isfinite(reported_cost) or reported_cost < 0:
            raise ValueError(
                "VariantOutcome.cost_usd must be finite and nonnegative"
            )
        try:
            rolled_up = await db.update_task_cost_totals(
                task_id,
                lease_token=lease_token,
                reported_cost_usd=reported_cost,
            )
            if not rolled_up:
                raise LeaseLostError(f"Task lease expired: {task_id}")
        except LeaseLostError:
            raise
        except Exception:
            logger.warning(
                "Cost rollup failed for completed task %s",
                task_id,
                exc_info=True,
            )
        completed = await db.complete_task(
            task_id,
            result_summary=answer[:10000],
            result_json=json.dumps(result),
            lease_token=lease_token,
        )
        if not completed:
            raise LeaseLostError(f"Task lease expired: {task_id}")
        with contextlib.suppress(Exception):
            await self._publish_task_state(
                task_id,
                user_task[:80],
                "completed",
                list(outcome.completed_subtasks),
            )
        with contextlib.suppress(Exception):
            await self.bb.publish_result(task_id, {
                "task_id": task_id,
                **result,
            })
        with contextlib.suppress(Exception):
            terminal_task = await db.get_task(task_id)
            if terminal_task is not None:
                await ensure_terminal_event(self.bb.redis, terminal_task)
                await ensure_system_terminal_event(self.bb.redis, terminal_task)

    async def _complete_traditional_task(
        self,
        task_id: str,
        user_task: str,
        result: dict[str, Any],
        budget_spent: float,
    ) -> None:
        """Preserve the legacy classic completion helper for callers."""
        await self._complete_variant_task(
            VariantExecutionRequest(
                task_id=task_id,
                session_id="legacy",
                user_task=user_task,
                triage=None,
            ),
            VariantOutcome(
                variant_id="classic",
                answer=str(result.get("answer", "")),
                result=result,
                public_result={"task_id": task_id, **result},
                cost_usd=budget_spent,
            ),
        )

    @staticmethod
    def _traditional_result(
        task_id: str,
        triage: TriageResult,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the stable public result shape for a classic task."""
        return {
            "task_id": task_id,
            "answer": result.get("answer", ""),
            "variant": "classic",
            "terminated_by": result.get("terminated_by"),
            "answer_source": result.get("answer_source"),
            "verification_status": result.get("verification_status"),
            "rounds": result.get("rounds_completed"),
            "budget_spent": result.get("budget_spent", 0.0),
            "complexity": triage.complexity.value,
        }

    @classmethod
    def _classic_outcome(
        cls,
        task_id: str,
        triage: TriageResult,
        result: dict[str, Any],
        cost_usd: float,
    ) -> VariantOutcome:
        """Build the classic result and shared terminal outcome."""
        completed_subtasks = tuple(
            {
                "id": f"{task_id}-{suffix}",
                "label": label,
                "status": "completed",
                "agent_role": role,
                "depends_on": dependencies,
            }
            for suffix, label, role, dependencies in (
                ("triage", "Triage classification", "planner", []),
                ("plan", "Plan decomposition", "planner", [f"{task_id}-triage"]),
                ("exec", "Execute sub-tasks", "executor", [f"{task_id}-plan"]),
                ("audit", "Audit and consensus", "auditor", [f"{task_id}-exec"]),
            )
        )
        return VariantOutcome(
            variant_id="classic",
            answer=str(result.get("answer", "")),
            result=result,
            public_result=cls._traditional_result(task_id, triage, result),
            cost_usd=cost_usd,
            completed_subtasks=completed_subtasks,
        )

    async def steer_entry(
        self, task_id: str, entry_id: str, action: str,
    ) -> dict[str, Any]:
        """Apply an operator change through the durable board gateway."""
        gateway = await self._task_gateway(task_id)
        store = gateway.store
        entry = await store.get_entry(task_id, entry_id)
        if entry is None:
            raise KeyError(entry_id)
        if action == "boost":
            old_salience = float(entry.salience)
            new_salience = await gateway.set_salience(
                task_id,
                entry_id,
                min(1.0, old_salience * 2.0),
                "operator",
            )
            return {
                "status": "boosted",
                "entry_id": entry_id,
                "old_salience": old_salience,
                "salience": new_salience,
            }
        if action == "retract":
            old_status = entry.status
            await gateway.set_status(
                task_id, entry_id, "superseded", "operator",
            )
            return {
                "status": "retracted",
                "entry_id": entry_id,
                "old_status": old_status,
            }
        raise ValueError(f"Unknown steering action: {action}")

    async def _task_gateway(self, task_id: str) -> Any:
        """Return the active gateway or open one for a saved task."""
        gateway = self._active_gateways.get(task_id)
        if gateway is not None:
            return gateway

        from core.board_store import SqliteRedisBoardStore, make_board_persist_hook
        from core.event_emitter import RedisEventEmitter
        from core.gateway import BoardGateway, salience_recompute_hook

        store = SqliteRedisBoardStore()
        await store.load_task(task_id)
        return BoardGateway(
            store,
            RedisEventEmitter(self.bb.redis),
            recompute_hooks=[
                salience_recompute_hook,
                make_board_persist_hook(self.bb),
            ],
        )

    async def append_task_entry(
        self,
        *,
        task_id: str,
        actor: str,
        capabilities: list[str],
        proposed: list[dict[str, Any]],
        turn_id: str,
        round_no: int,
    ) -> list[Any]:
        """Append external task data through the durable board gateway."""
        gateway = await self._task_gateway(task_id)
        return await gateway.append(
            task_id=task_id,
            actor=actor,
            capabilities=capabilities,
            proposed=proposed,
            turn_id=turn_id,
            round_no=round_no,
        )

    async def cancel_remote_task(self, task_id: str) -> int:
        """Request cancellation from every agent node for one task."""
        endpoints = set(AGENT_ENDPOINTS.values())
        for registration in ROLE_REGISTRY.values():
            endpoints.update(registration.get("endpoints", []))
        gateway = self._active_gateways.get(task_id)
        if gateway is not None:
            meta = await gateway.store.get_meta(task_id)
            registry = meta.get("effective_registry", {})
            if isinstance(registry, dict):
                for registration in registry.values():
                    if isinstance(registration, dict):
                        endpoints.update(registration.get("endpoints", []))
        headers = (
            {"Authorization": f"Bearer {BMAS_EXECUTE_KEY}"}
            if BMAS_EXECUTE_KEY
            else None
        )

        async def cancel(endpoint: str) -> int:
            try:
                response = await self.http.post(
                    f"{endpoint}/tasks/{task_id}/cancel",
                    headers=headers,
                    timeout=10.0,
                )
                if response.status_code == 200:
                    return int(response.json().get("cancelled", 0))
            except Exception as exc:
                logger.warning(
                    "Remote cancellation failed for task %s on %s: %s",
                    task_id, endpoint, exc,
                )
            return 0

        return sum(await asyncio.gather(*(cancel(endpoint) for endpoint in endpoints)))

    async def _dispatch_traditional_turn(
        self,
        variant: Any,
        task: dict,
        activation: Any,
        round_no: int,
        rationale: str | None = None,
        phase: str | None = None,
        space: str = "public",
        apply_to_board: bool = True,
        budget_limit_usd: float | None = None,
    ) -> dict:
        """Dispatch one turn for the classic runtime.

        Uses build_turn_payload → _dispatch_agent → parse_agent_response → apply.
        Emits turn_start/turn_end SSE events for WorkerLane + AgentTrace.

        ``rationale``/``phase`` are the Control Unit's routing decision for this
        round; they are persisted on the turn and echoed on the turn_start SSE
        event so the Graph tab can show WHY each agent was activated.
        """
        task_id = task["task_id"]
        if space == "public":
            board = await variant.store.get_snapshot(task_id)
        else:
            board = await variant.store.get_private_snapshot(task_id, space)

        # Build payload
        payload = variant.build_turn_payload(task, activation.actor, board)
        payload["model"] = activation.model
        turn_id = activation.activation_id or (
            "activation-"
            + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"bmas:{task_id}:{round_no}:{activation.actor}:{space}",
            ).hex
        )
        payload["turn_id"] = turn_id
        if budget_limit_usd is not None:
            payload["budget_remaining_usd"] = max(0.0, budget_limit_usd)

        # Per-agent log: this agent is being activated. Attributed to the
        # actor (persona) so the Logs tab shows the agent, not the daemon.
        board_entries_ctx = (payload.get("board") or {}).get("entries", []) \
            if isinstance(payload.get("board"), dict) else []
        await self._safe_log(
            activation.actor,
            f"Activated for round {round_no} → {activation.role} on {activation.model}",
            task_id=task_id,
            level="info",
            node=activation.node_endpoint,
            turn_id=turn_id,
            fields={
                "event": "turn_dispatch",
                "actor": activation.actor,
                "role": activation.role,
                "profile": activation.profile,
                "model": activation.model,
                "node": activation.node_endpoint,
                "round": round_no,
                "objective": payload.get("objective"),
                "budget_remaining_usd": payload.get("budget_remaining_usd"),
                "board_entries_seen": len(board_entries_ctx),
                "previous_response_id": payload.get("previous_response_id"),
                "persona_preview": _summarize(payload.get("role_prompt", ""), 400),
            },
        )

        # Emit turn_start SSE event for WorkerLane/AgentTrace
        with contextlib.suppress(Exception):  # SSE is best-effort
            await self.bb.publish_event(task_id, "turn_start", {
                "turn_id": turn_id,
                "actor": activation.actor,
                "role": activation.role,
                "round": round_no,
                "model": activation.model,
                "node": activation.node_endpoint,
                "rationale": rationale,
                "phase": phase,
            })

        # Dispatch to agent node
        role_registry = getattr(variant, "role_registry", {})
        turn_timeout_s = AGENT_TURN_TIMEOUT_S
        genesis_time = getattr(variant, "genesis_time", 0.0)
        max_duration_s = getattr(variant, "max_duration_s", 0)
        if genesis_time and max_duration_s:
            turn_timeout_s = max(
                10,
                min(
                    AGENT_TURN_TIMEOUT_S,
                    int(
                        max_duration_s
                        - (asyncio.get_running_loop().time() - genesis_time)
                    ),
                ),
            )
        if getattr(variant, "closing_sequence", False):
            # A closing turn (forced decider, grace review, revision) gets a
            # full window even past the duration cap: clamping it to the
            # remaining seconds guarantees a timeout and an unverified stop.
            closing_floor = getattr(variant, "closing_turn_timeout_s", None)
            if callable(closing_floor):
                turn_timeout_s = max(
                    turn_timeout_s,
                    min(AGENT_TURN_TIMEOUT_S, int(closing_floor())),
                )
        response = await self._dispatch_turn(
            role=activation.role,
            task_id=task_id,
            description=task["query"],
            persona=payload.get("role_prompt", ""),
            context={
                "board": payload.get("board"),
                "objective": payload.get("objective"),
                "attachments": task.get("attachments", []),
                "round": round_no,
                "budget_remaining_usd": payload.get("budget_remaining_usd"),
                # Phase 5: stateful turns (doc 12 §5.2)
                "session_id": payload.get("session_id"),
                "previous_response_id": payload.get("previous_response_id"),
            },
            model=activation.model,
            round_no=round_no,
            actor=activation.actor,
            rationale=rationale,
            phase=phase,
            turn_id=turn_id,
            endpoint=activation.node_endpoint,
            profile=activation.profile,
            session_id=payload.get("session_id"),
            activation_id=turn_id,
            endpoints=[
                candidate
                for candidate in dict.fromkeys([
                    activation.node_endpoint,
                    *role_registry.get(
                        activation.role, {},
                    ).get("endpoints", []),
                ])
                if candidate
            ],
            timeout_s=turn_timeout_s,
            model_pricing=getattr(variant, "model_pricing", MODEL_PRICING),
        )

        # Account for every worker call here. Conflict turns call this method
        # directly, outside the normal round group.
        if isinstance(response, dict):
            actual_endpoint = response.get("endpoint")
            if (
                actual_endpoint
                and actual_endpoint != activation.node_endpoint
            ):
                variant.clear_response_id(activation.actor)
                variant.set_actor_node(activation.actor, str(actual_endpoint))
            note_duration = getattr(variant, "note_turn_duration", None)
            if callable(note_duration):
                note_duration(response.get("duration_ms"))
            usage = response.get("usage")
            if usage:
                variant.track_cost(self._compute_cost(
                    usage,
                    getattr(variant, "model_pricing", MODEL_PRICING),
                ))
                await variant.gateway.set_meta(
                    task_id,
                    budget_spent=variant.budget_spent,
                )

            response_id = response.get("response_id")
            if response_id:
                variant.set_response_id(activation.actor, response_id)

        # Per-agent log: capture the agent's reasoning / output verbatim so
        # operators can understand AGENT THINKING. The full text lives in
        # `fields`; the header is a one-line preview.
        resp_status = response.get("status", "") if isinstance(response, dict) else ""
        resp_text = response.get("result", "") if isinstance(response, dict) else str(response)
        usage = response.get("usage") if isinstance(response, dict) else None
        log_level = "error" if resp_status in ("failed", "timeout") else "info"
        await self._safe_log(
            activation.actor,
            f"Responded ({resp_status or 'completed'}): {_summarize(resp_text)}",
            task_id=task_id,
            level=log_level,
            node=response.get("node_id") if isinstance(response, dict) else activation.node_endpoint,
            turn_id=turn_id,
            fields={
                "event": "turn_response",
                "actor": activation.actor,
                "role": activation.role,
                "model": activation.model,
                "round": round_no,
                "status": resp_status or "completed",
                "output": resp_text,
                "output_chars": len(resp_text or ""),
                "usage": usage,
                "duration_ms": response.get("duration_ms") if isinstance(response, dict) else None,
                "trace_count": response.get("trace_count") if isinstance(response, dict) else None,
                "run_id": response.get("run_id") if isinstance(response, dict) else None,
            },
        )

        # Parse response into board entries.
        # Pass known_ids so the parser can validate ref mentions against the
        # actual board state (only IDs that exist are promoted from prose refs).
        known_ids = set(board.keys()) if isinstance(board, dict) else None
        if resp_status in ("failed", "timeout"):
            entries = []
        else:
            entries = variant.parse_agent_response(
                task,
                activation.actor,
                response,
                known_ids=known_ids,
            )

        # Apply through gateway (if agent contributed anything)
        committed_entries: list[Any] = []
        if entries:
            for mutation_index, entry in enumerate(entries):
                mutation = {
                    "actor": activation.actor,
                    "turn_id": turn_id,
                    "round": round_no,
                    "_mutation_id": f"{turn_id}:{mutation_index}",
                    **entry,
                }
                if entry.get("_action") == "clean":
                    mutation["_action"] = "clean"
                    removals = entry.get("removals", [])
                    await self._safe_log(
                        activation.actor,
                        f"Board write: cleaned {len(removals)} entry(ies)",
                        task_id=task_id, level="info",
                        node=activation.node_endpoint, turn_id=turn_id,
                        fields={
                            "event": "board_clean",
                            "actor": activation.actor,
                            "round": round_no,
                            "removals": removals,
                        },
                    )
                else:
                    mutation["entries"] = [entry]
                    await self._safe_log(
                        activation.actor,
                        f"Board write: {entry.get('type', 'finding')} — "
                        f"{_summarize(entry.get('title') or entry.get('body', ''), 120)}",
                        task_id=task_id, level="info",
                        node=activation.node_endpoint, turn_id=turn_id,
                        fields={
                            "event": "board_write",
                            "actor": activation.actor,
                            "round": round_no,
                            "entry_type": entry.get("type"),
                            "title": entry.get("title"),
                            "body": entry.get("body"),
                            "refs": entry.get("refs", []),
                            "confidence": entry.get("confidence"),
                        },
                    )
                if apply_to_board:
                    committed_entries.extend(
                        await variant.apply(task, [mutation])
                    )
        elif resp_status not in ("failed", "timeout"):
            # Agent ran but contributed no board entries (declined/no-op).
            await self._safe_log(
                activation.actor,
                "Declined — no board contribution this turn",
                task_id=task_id, level="debug",
                node=activation.node_endpoint, turn_id=turn_id,
                fields={
                    "event": "turn_declined",
                    "actor": activation.actor,
                    "round": round_no,
                    "status": resp_status or "completed",
                },
            )

        if (
            apply_to_board
            and activation.actor == "critic"
            and resp_status == "completed"
            and committed_entries
        ):
            from core.entry import BoardEntry

            await variant.mark_solution_reviewed(
                task_id,
                [
                    entry
                    for entry in committed_entries
                    if isinstance(entry, BoardEntry)
                ],
            )

        # Emit turn_end SSE event
        try:
            turn_status = response.get("status", "completed") if isinstance(response, dict) else "completed"
            await self.bb.publish_event(task_id, "turn_end", {
                "turn_id": turn_id,
                "actor": activation.actor,
                "round": round_no,
                "status": turn_status,
                "entries_added": len(committed_entries),
            })
        except Exception:
            pass  # SSE is best-effort

        return response

    async def _dispatch_traditional_group(
        self,
        variant: Any,
        task: dict,
        activations: list[Any],
        round_no: int,
        *,
        rationale: str | None = None,
        phase: str | None = None,
    ) -> list[Any]:
        """Dispatch one activation group with explicit budget reservations."""
        if not activations:
            return []

        task_id = task["task_id"]
        budgets = variant.reserve_activation_budgets(len(activations))
        reserved = sum(budgets)
        await variant.gateway.set_meta(task_id, budget_reserved=reserved)

        async def _run(activation: Any, budget: float) -> Any:
            try:
                result = await self._dispatch_traditional_turn(
                    variant,
                    task,
                    activation,
                    round_no,
                    rationale=rationale,
                    phase=phase,
                    budget_limit_usd=budget,
                )
            except Exception as exc:
                if isinstance(exc, (LeaseLostError, db.LeaseFenceError)):
                    raise LeaseLostError(
                        f"Task lease expired: {task_id}"
                    ) from exc
                return exc

            if hasattr(variant, "mark_activation_complete"):
                await variant.mark_activation_complete(
                    task_id,
                    activation.activation_id or "",
                    str(result.get("status", "completed"))
                    if isinstance(result, dict)
                    else "failed",
                    actor=activation.actor,
                    response_id=(
                        str(result["response_id"])
                        if isinstance(result, dict) and result.get("response_id")
                        else None
                    ),
                    node_endpoint=(
                        str(result.get("endpoint") or activation.node_endpoint)
                        if isinstance(result, dict)
                        else activation.node_endpoint
                    ),
                )

            return result

        try:
            if variant.round_execution == "sequential":
                results = []
                for activation, budget in zip(activations, budgets, strict=True):
                    results.append(await _run(activation, budget))
                return results

            return await asyncio.gather(
                *(
                    _run(activation, budget)
                    for activation, budget in zip(activations, budgets, strict=True)
                )
            )
        finally:
            await variant.gateway.set_meta(
                task_id,
                budget_spent=variant.budget_spent,
                budget_reserved=0.0,
            )

    @staticmethod
    def _compute_cost(usage: dict, pricing: dict) -> float:
        """Compute cost from usage and pricing tables."""
        model = usage.get("model", "unknown")
        model_pricing = pricing.get(model, {})
        if not model_pricing:
            return 0.0
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        cost = (
            prompt_tokens * float(model_pricing.get("input_cost_per_token", 0))
            + completion_tokens * float(model_pricing.get("output_cost_per_token", 0))
        )
        return round(cost, 8)

    async def _admit_foundation_run(
        self,
        task_id: str,
        runtime_key: RuntimeKey,
        effective_configuration: dict[str, Any] | None,
        overrides: dict | None,
    ) -> dict[str, Any] | None:
        """Admit one interactive task into its Foundation run, or stay legacy.

        A disabled writer gate returns None. A prerequisite or
        reservation failure fails the task closed, because a run the
        admission writer rejected must not execute.
        """
        import interactive_admission
        import run_admission

        seed = (overrides or {}).get("seed") if isinstance(overrides, dict) else None
        try:
            return await interactive_admission.admit_task_run(
                task_id=task_id,
                runtime_key=runtime_key,
                effective_configuration=effective_configuration,
                requested_seed=seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
            )
        except (
            run_admission.AdmissionPrerequisiteError,
            run_admission.AdmissionReservationError,
        ) as exc:
            raise VariantConfigurationError(
                f"The Foundation admission rejected task {task_id}: {exc}"
            ) from exc

    async def _native_plan(
        self, task_id: str, candidate_urls: list[str],
    ) -> dict[str, Any] | None:
        """Choose the native protocol when the task runs under a run control
        row and the first healthy endpoint publishes a qualified document."""
        import agent_dispatch

        if not candidate_urls:
            return None
        try:
            context = await agent_dispatch.native_context(task_id)
            if context is None:
                return None
            document = await agent_dispatch.endpoint_capabilities(self.http, candidate_urls[0])
        except Exception as exc:  # noqa: BLE001 - the legacy path stays available
            logger.debug(f"Native dispatch plan unavailable for {task_id}: {exc}")
            return None
        if not agent_dispatch.supports_native_protocol(document):
            return None
        return {"context": context, "document": document, "url": candidate_urls[0]}

    async def _post_activation(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None,
        native_plan: dict[str, Any] | None,
        attempt_number: int = 1,
    ) -> httpx.Response:
        """Deliver one activation: a signed grant for a native endpoint, else
        the bearer ``/execute`` request.

        The agent's answer arrives as one HTTP response. A daemon-side
        ledger error is not an endpoint failure, so it returns one
        failed turn without touching the endpoint circuit.
        """
        timeout = float(payload["timeout"]) + 15.0
        if native_plan is None or native_plan["url"] != url:
            return await self.http.post(
                f"{url}/execute", json=payload, headers=headers, timeout=timeout,
            )
        import activation_service
        import agent_dispatch
        import agent_protocol
        from core.signing import SigningError

        request = httpx.Request("POST", f"{url}/bmas/activations")
        try:
            outcome = await agent_dispatch.dispatch_activation(
                self.http, agent_url=url,
                run_id=native_plan["context"].run_id,
                task_id=str(payload["task_id"]),
                activation_id=str(payload["activation_id"]),
                request=payload,
                task_fence=native_plan["context"].task_fence,
                attempt=attempt_number,
                retry_of_attempt=attempt_number - 1 if attempt_number > 1 else None,
                timeout_s=timeout,
                document=native_plan["document"],
            )
        except agent_dispatch.DispatchError as exc:
            return httpx.Response(502, text=str(exc), request=request)
        except (
            activation_service.ActivationServiceError,
            agent_protocol.AgentProtocolError,
            SigningError,
        ) as exc:
            logger.warning(f"Native dispatch ledger error for {payload['task_id']}: {exc}")
            return httpx.Response(200, json={
                "task_id": str(payload["task_id"]),
                "status": "failed",
                "result": f"Native dispatch ledger error: {exc}",
                "native_protocol": {"error": type(exc).__name__},
            }, request=request)
        data = dict(outcome.get("result") or {})
        data.setdefault("status", "failed")
        data["native_protocol"] = {
            "grant_id": outcome["grant_id"],
            "acknowledgement_status": outcome["acknowledgement_status"],
            "activation_state": outcome["activation_state"],
            "replayed": outcome["replayed"],
        }
        return httpx.Response(200, json=data, request=request)

    async def _dispatch_turn(
        self, role: str, task_id: str, description: str, persona: str,
        context: dict | None = None,
        model: str | None = None,
        round_no: int = 1,
        actor: str | None = None,
        rationale: str | None = None,
        phase: str | None = None,
        turn_id: str | None = None,
        endpoint: str | None = None,
        profile: str | None = None,
        session_id: str | None = None,
        activation_id: str | None = None,
        endpoints: list[str] | None = None,
        timeout_s: int = AGENT_TURN_TIMEOUT_S,
        model_pricing: dict[str, dict[str, Any]] | None = None,
    ) -> dict:
        """Send one classic activation to a Hermes agent node.

        Handles endpoint resolution (role registry → AGENT_ENDPOINTS fallback),
        turn tracking in SQLite, 3-attempt retry with backoff, and best-effort
        cost recording via MODEL_PRICING.

        ``round_no``/``actor``/``rationale``/``phase`` enrich the persisted turn
        record (doc 05 §1) so the Graph tab can reconstruct the real execution:
        the true round index, the full actor identity (e.g.
        ``expert.valuation_analyst``), and the Control Unit's routing rationale.
        """
        _reg = ROLE_REGISTRY.get(role, {})
        if endpoint:
            url = endpoint
        elif _reg and _reg.get("endpoints"):
            url = _reg["endpoints"][0]
        else:
            url = AGENT_ENDPOINTS.get(role, "")

        turn_id = turn_id or f"turn-{str(uuid.uuid4())[:8]}"
        payload: dict[str, Any] = {
            "task_id": task_id,
            "description": description,
            "role_prompt": persona,
            "turn_id": turn_id,
            "role": role,
            "model": model,
            "profile": profile if profile is not None else _reg.get("profile"),
            "session_id": session_id,
            "activation_id": activation_id or turn_id,
        }
        if context:
            payload["context"] = context
        payload["timeout"] = max(10, min(3600, int(timeout_s)))

        configured_urls = [
            candidate
            for candidate in dict.fromkeys(endpoints or [url])
            if candidate
        ]
        circuits = self._circuits()
        candidate_urls = [
            candidate for candidate in configured_urls
            if circuits.allow(candidate)
        ]
        if not candidate_urls:
            return {
                "task_id": task_id,
                "status": "failed",
                "result": (
                    f"No healthy agent endpoint is available for role {role}"
                    if configured_urls
                    else f"No agent endpoint configured for role {role}"
                ),
            }

        try:
            await db.create_turn({
                "id": turn_id, "task_id": task_id, "round_no": round_no,
                "role": role, "actor": actor or role,
                "node": candidate_urls[0], "model": model, "status": "running",
                "rationale": rationale, "phase": phase,
            })
        except Exception as e:
            logger.warning(f"Turn create failed {task_id}/{turn_id}: {e}")

        native_plan = await self._native_plan(task_id, candidate_urls)
        max_attempts = len(candidate_urls) + 2
        candidate_index = 0
        rate_limited_until: dict[str, float] = {}
        for attempt in range(max_attempts):
            url = candidate_urls[candidate_index]
            try:
                headers = (
                    {"Authorization": f"Bearer {BMAS_EXECUTE_KEY}"}
                    if BMAS_EXECUTE_KEY
                    else None
                )
                endpoint_slot = await self._acquire_endpoint_slot(url)
                try:
                    await self._assert_dispatch_lease(task_id)
                    resp = await self._post_activation(
                        url, payload, headers, native_plan, attempt + 1,
                    )
                finally:
                    self._release_endpoint_slot(url, endpoint_slot)
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp)
                    delay = (
                        retry_after
                        if retry_after is not None
                        else min(8.0, 2 ** attempt) + random.uniform(0.0, 0.25)
                    )
                    next_index = (
                        (candidate_index + 1) % len(candidate_urls)
                        if candidate_urls
                        else candidate_index
                    )
                    next_url = candidate_urls[next_index]
                    rate_limited_until[url] = (
                        asyncio.get_running_loop().time() + delay
                    )
                    await self._safe_log(
                        role,
                        f"Agent endpoint returned HTTP 429. Retry in {delay:.2f}s.",
                        task_id=task_id,
                        level="warning",
                        node=url,
                        turn_id=turn_id,
                        fields={
                            "event": "endpoint_rate_limited",
                            "role": role,
                            "node": url,
                            "next_node": next_url,
                            "attempt": attempt + 1,
                            "delay_s": delay,
                        },
                    )
                    if attempt >= max_attempts - 1:
                        with contextlib.suppress(Exception):
                            await db.complete_turn(
                                turn_id, "failed", 0, 0.0, node=url,
                            )
                        return {
                            "task_id": task_id,
                            "status": "failed",
                            "error_code": "endpoint_rate_limited",
                            "result": "All agent endpoints remained at capacity.",
                            "retry_after": delay,
                        }
                    if next_url != url:
                        context_payload = payload.get("context")
                        if isinstance(context_payload, dict):
                            context_payload["previous_response_id"] = None
                    candidate_index = next_index
                    next_wait = max(
                        0.0,
                        rate_limited_until.get(next_url, 0.0)
                        - asyncio.get_running_loop().time(),
                    )
                    if next_wait:
                        await asyncio.sleep(next_wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict) or not data:
                    raise ValueError("Agent returned an empty or invalid response")
                agent_status = str(data.get("status", "")).lower()
                if agent_status not in {
                    "completed", "declined", "failed", "timeout",
                }:
                    raise ValueError(
                        f"Agent returned an invalid status: {agent_status or 'missing'}"
                    )
                circuits.record_success(url)
                # The daemon creates the durable turn identity. Do not let an
                # agent node replace it with a local or stale identifier.
                data["turn_id"] = turn_id
                data["activation_id"] = activation_id or turn_id
                data["endpoint"] = url
                if session_id is not None:
                    data["session_id"] = session_id

                # Best-effort cost tracking
                usage = data.get("usage")
                cost_usd = 0.0
                if usage and isinstance(usage, dict):
                    model_used = usage.get("model", model or "unknown")
                    effective_pricing = (
                        MODEL_PRICING
                        if model_pricing is None
                        else model_pricing
                    )
                    pricing = effective_pricing.get(model_used, {})
                    if pricing:
                        cost_usd = round(
                            usage.get("prompt_tokens", 0) * float(pricing.get("input_cost_per_token", 0))
                            + usage.get("completion_tokens", 0) * float(pricing.get("output_cost_per_token", 0)),
                            8,
                        )
                    # The response records usage before task completion. Final
                    # trace ingestion uses the same phase and INSERT OR IGNORE.
                    with contextlib.suppress(Exception):
                        await db.insert_cost_entry_v2(
                            task_id=task_id, model=model_used,
                            input_tokens=usage.get("prompt_tokens", 0),
                            output_tokens=usage.get("completion_tokens", 0),
                            cost_usd=cost_usd, phase="trace",
                            node_id=data.get("node_id"),
                            turn_id=turn_id,
                            provider=None, price_source="bmas.yaml",
                            joules_estimate=0.0,
                        )

                turn_status = "completed" if agent_status == "completed" else (
                    "declined" if agent_status == "declined" else "failed"
                )
                with contextlib.suppress(Exception):
                    await db.complete_turn(
                        turn_id=turn_id, status=turn_status,
                        entries_added=len(data.get("entries") or []),
                        cost_usd=cost_usd, joules_estimate=0.0,
                        node=url,
                    )
                return data

            except LeaseLostError:
                raise
            except EndpointOverloadedError as e:
                await self._safe_log(
                    role,
                    str(e),
                    task_id=task_id,
                    level="warning",
                    node=url,
                    turn_id=turn_id,
                    fields={
                        "event": "endpoint_overloaded",
                        "role": role,
                        "node": url,
                        "limit": AGENT_ENDPOINT_MAX_CONCURRENCY,
                        "wait_timeout_s": AGENT_ENDPOINT_WAIT_TIMEOUT_S,
                    },
                )
                if candidate_index + 1 < len(candidate_urls):
                    candidate_index += 1
                    continue
                with contextlib.suppress(Exception):
                    await db.complete_turn(
                        turn_id, "failed", 0, 0.0, node=url,
                    )
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error_code": "endpoint_overloaded",
                    "result": str(e),
                }
            except Exception as e:
                circuits.record_failure(url)
                # A connect failure proves that the selected node did not
                # receive the request. Other transport errors are ambiguous.
                # Retry ambiguous calls on the same node so its idempotency
                # cache can join or return the original activation.
                if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                    candidate_index = min(
                        candidate_index + 1,
                        len(candidate_urls) - 1,
                    )
                    if candidate_urls[candidate_index] != url:
                        context_payload = payload.get("context")
                        if isinstance(context_payload, dict):
                            context_payload["previous_response_id"] = None
                if attempt < max_attempts - 1:
                    delay = min(8.0, 2 ** attempt) + random.uniform(0.0, 0.25)
                    await self._safe_log(role,
                        f"Retry {attempt + 1}/{max_attempts - 1} after {delay}s: {e}", task_id=task_id,
                        level="warning", node=url, turn_id=turn_id,
                        fields={
                            "event": "dispatch_retry",
                            "role": role,
                            "attempt": attempt + 1,
                            "delay_s": delay,
                            "node": url,
                            "error": str(e),
                        })
                    await asyncio.sleep(delay)
                    continue
                await self._safe_log(role, f"ERROR after {max_attempts} attempts: {e}", task_id=task_id,
                    level="error", node=url, turn_id=turn_id,
                    fields={
                        "event": "dispatch_failed",
                        "role": role,
                        "node": url,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    })
                with contextlib.suppress(Exception):
                    await db.complete_turn(
                        turn_id, "failed", 0, 0.0, node=url,
                    )
                return {"task_id": task_id, "status": "failed", "result": str(e)}

        return {"task_id": task_id, "status": "failed", "result": "max retries"}  # pragma: no cover

    async def close(self):
        await self.bb.close()
        await self.triage.close()
        await self.http.aclose()
