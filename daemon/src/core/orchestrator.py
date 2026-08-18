# /opt/bmas/daemon/src/core/orchestrator.py
"""
bMAS Orchestrator: decomposes tasks, dispatches to agents, manages debate cycles.

Dual-write pattern: Every lifecycle event writes to both Redis (real-time
blackboard for live UI) and SQLite (permanent task history). SQLite writes
are best-effort — they log warnings on failure but never interrupt a running task.
"""

import asyncio
import contextlib
import json
import logging
import random
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

import database as db
from config import (
    AGENT_ENDPOINTS,
    AGENT_TURN_TIMEOUT_S,
    BMAS_EXECUTE_KEY,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_RECOVERY_S,
    COORDINATION_VARIANT,
    EDGE_NODE_MODELS,
    LITELLM_KEY,
    LITELLM_URL,
    LOCK_TTL_MS,
    MODEL_POOLS,
    MODEL_PRICING,
    ROLE_REGISTRY,
    ROUND_EXECUTION,
    TRADITIONAL_CONFIG,
    TRIAGE_URL,
    VIEW_BUDGET_TOKENS,
)
from core.blackboard import Blackboard, normalize_level
from core.circuit_breaker import EndpointCircuitBreaker
from core.gateway import LeaseLostError
from core.triage import MODEL_ROUTING, Complexity, TriageResult, TriageRouter

logger = logging.getLogger("bmas.orchestrator")


class LeaseBusyError(RuntimeError):
    """Another daemon currently owns the task execution lease."""


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
        self._active_gateways: dict[str, Any] = {}
        self._agent_circuits = EndpointCircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout_s=CIRCUIT_BREAKER_RECOVERY_S,
        )

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

        # Dual-write: persist sub-task state in SQLite
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
        resume: bool = False,
    ) -> dict:
        """Main entry point: triage → plan → execute → audit → publish.

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
                await db.create_task(
                    task_id,
                    user_task[:80],
                    user_task,
                    variant=COORDINATION_VARIANT,
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

            if resume:
                row = await db.get_task(task_id)
                if row is None:
                    raise RuntimeError(f"Cannot resume missing task: {task_id}")
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
                    "Resuming task from its durable classic-board checkpoint",
                    task_id=task_id,
                    fields={"event": "task_resumed"},
                )
                return await self._run_traditional(
                    task_id,
                    session_id,
                    user_task,
                    triage,
                    overrides=overrides,
                    resume=True,
                )

            # Publish initial task state so the UI can show it
            await self._publish_task_state(task_id, user_task[:80], "running", [
                {"id": f"{task_id}-triage",  "label": "Triage classification", "status": "running",  "agent_role": "planner",  "depends_on": []},
                {"id": f"{task_id}-plan",    "label": "Plan decomposition",    "status": "pending",  "agent_role": "planner",  "depends_on": [f"{task_id}-triage"]},
                {"id": f"{task_id}-exec",    "label": "Execute sub-tasks",     "status": "pending",  "agent_role": "executor", "depends_on": [f"{task_id}-plan"]},
                {"id": f"{task_id}-audit",   "label": "Audit & consensus",     "status": "pending",  "agent_role": "auditor",  "depends_on": [f"{task_id}-exec"]},
            ])

            # 2. Triage complexity
            # Build effective routing: session overrides merged with per-task overrides
            from settings_store import get_store as _get_store
            _store = _get_store()
            effective_routing = await _store.get_routing()  # session-level overrides
            if overrides and overrides.get("routing"):
                effective_routing.update(overrides["routing"])  # per-task on top

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
                    variant=COORDINATION_VARIANT,  # stamp correct variant, not schema default
                    lease_token=lock_id,
                )
                if not updated:
                    raise LeaseLostError(f"Task lease expired: {task_id}")
            except Exception as e:
                if isinstance(e, LeaseLostError):
                    raise
                logger.warning(f"SQLite update_task_status failed for {task_id}: {e}")

            # Update triage sub-task to completed
            await self._publish_task_state(task_id, user_task[:80], "running", [
                {"id": f"{task_id}-triage",  "label": f"Triage: {triage.complexity.value}", "status": "completed", "agent_role": "planner",  "depends_on": []},
                {"id": f"{task_id}-plan",    "label": "Plan decomposition",    "status": "pending",  "agent_role": "planner",  "depends_on": [f"{task_id}-triage"]},
                {"id": f"{task_id}-exec",    "label": "Execute sub-tasks",     "status": "pending",  "agent_role": "executor", "depends_on": [f"{task_id}-plan"]},
                {"id": f"{task_id}-audit",   "label": "Audit & consensus",     "status": "pending",  "agent_role": "auditor",  "depends_on": [f"{task_id}-exec"]},
            ])
            # 3. Run the blackboard coordination loop
            return await self._run_traditional(
                task_id, session_id, user_task, triage,
                overrides=overrides,
                resume=False,
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
        except Exception as e:
            # Record failure in SQLite before re-raising
            failed = False
            try:
                failed = await db.fail_task(
                    task_id,
                    str(e),
                    lease_token=lock_id if lease_claimed else None,
                )
            except Exception:
                logger.warning(f"SQLite fail_task failed for {task_id}")

            if not failed:
                raise

            # Emit error event
            with contextlib.suppress(Exception):
                await self.bb.publish_event(task_id, "error", {
                    "error_message": str(e)
                })

            with contextlib.suppress(Exception):
                await self._publish_task_state(
                    task_id, user_task[:80], "failed",
                )

            # Emit system task-completed (failed) event
            with contextlib.suppress(Exception):
                await self.bb.publish_system_event("task-completed", {
                    "task_id": task_id, "status": "failed", "label": user_task[:80]
                })

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

    # ── Traditional Variant Integration (doc 05) ──────────────────────

    async def _run_traditional(
        self,
        task_id: str,
        session_id: str,
        user_task: str,
        triage: TriageResult,
        *,
        overrides: dict | None = None,
        resume: bool = False,
    ) -> dict:
        """Run the paper's cyclic blackboard loop (doc 05).

        The orchestrator owns lifecycle (lock, abort, events, SQLite).
        The TraditionalVariant owns the loop (genesis, step, finalize).
        CU and AG calls are control-plane LiteLLM calls, never Hermes runs.

        Args:
            overrides: Optional per-task overrides dict with keys:
                'routing' (dict[str, str]) and/or 'role_registry' (dict[str, dict]).
                These are merged on top of the session settings_store values.
        """
        import copy as _copy

        from core.board_store import SqliteRedisBoardStore, make_board_persist_hook
        from core.event_emitter import RedisEventEmitter
        from core.gateway import BoardGateway, salience_recompute_hook
        from core.variants.traditional import TraditionalVariant
        from settings_store import get_store as _get_store

        await self._safe_log("daemon",
            f"Traditional variant | tier={triage.complexity.value}", task_id=task_id)

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

        # Build the current task configuration. A resumed task uses its saved
        # copy, so a settings change cannot alter an active task's behavior.
        current_task_config = {
            "traditional": {
                **_copy.deepcopy(dict(TRADITIONAL_CONFIG)),
                "round_execution": ROUND_EXECUTION,
                "view_budget_tokens": VIEW_BUDGET_TOKENS,
            },
            "model_pools": _copy.deepcopy(MODEL_POOLS),
            "edge_node_models": _copy.deepcopy(EDGE_NODE_MODELS),
            "node_endpoints": sorted({ep for ep in AGENT_ENDPOINTS.values()}),
        }

        # ── Effective settings: session overrides → per-task overrides ──
        _store = _get_store()
        # Routing: session store provides the base, per-task overrides on top
        persisted_meta = await board_store.get_meta(task_id)
        effective_task_config = current_task_config
        if resume and isinstance(
            persisted_meta.get("effective_task_config"), dict,
        ):
            effective_task_config = _copy.deepcopy(
                persisted_meta["effective_task_config"]
            )
        effective_routing = await _store.get_routing()
        has_persisted_routing = resume and isinstance(
            persisted_meta.get("effective_routing"), dict,
        )
        if has_persisted_routing:
            effective_routing = dict(persisted_meta["effective_routing"])
        if (
            not has_persisted_routing
            and overrides
            and overrides.get("routing")
        ):
            effective_routing.update(overrides["routing"])

        # Role registry: session store provides the base, per-task overrides on top
        effective_registry = await _store.get_role_registry()
        has_persisted_registry = resume and isinstance(
            persisted_meta.get("effective_registry"), dict,
        )
        if has_persisted_registry:
            effective_registry = dict(persisted_meta["effective_registry"])
        if (
            not has_persisted_registry
            and overrides
            and overrides.get("role_registry")
        ):
            for role_name, role_patch in overrides["role_registry"].items():
                existing = effective_registry.get(role_name, {})
                merged = _copy.deepcopy(existing)
                merged.update(role_patch)
                effective_registry[role_name] = merged

        variant = TraditionalVariant(
            gateway=gateway,
            board_store=board_store,
            event_emitter=event_emitter,
            triage=self.triage,
            config=dict(effective_task_config.get("traditional") or {}),
            litellm_url=LITELLM_URL,
            litellm_key=LITELLM_KEY,
            node_endpoints=list(effective_task_config.get("node_endpoints") or []),
            role_registry=effective_registry,
            model_routing=effective_routing,
            model_pools=dict(effective_task_config.get("model_pools") or {}),
            edge_node_models=list(
                effective_task_config.get("edge_node_models") or []
            ),
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
                        attachments = [
                            {
                                "name": f.get("original_filename", "file"),
                                "text_preview": f.get("text_preview", ""),
                            }
                            for f in task_files
                        ]
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
                effective_task_config=effective_task_config,
                effective_routing=effective_routing,
                effective_registry=effective_registry,
            )

            if resume and events:
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
                await self._complete_traditional_task(
                    task_id, user_task, result, variant.budget_spent,
                )
                return self._traditional_result(task_id, triage, result)

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
            for round_no in range(start_round, variant.max_rounds + 2):
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
                await variant.checkpoint(task_id)
            else:
                from core.variants.traditional import StepResult
                step_result = StepResult(terminal=True, reason="max_rounds")

            # ── Finalize ─────────────────────────────────────────────
            await self._set_phase("finalize", 0, task_id=task_id)
            board = await board_store.get_snapshot(task_id)
            result = await variant.finalize(
                task, board, step_result.reason or "unknown",
            )
            await variant.checkpoint(task_id)

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

            await self._complete_traditional_task(
                task_id, user_task, result, variant.budget_spent,
            )
            return self._traditional_result(task_id, triage, result)

        finally:
            self._active_gateways.pop(task_id, None)
            await variant.close()
            with contextlib.suppress(Exception):
                await gateway.unload_task(task_id)

    async def _complete_traditional_task(
        self,
        task_id: str,
        user_task: str,
        result: dict[str, Any],
        budget_spent: float,
    ) -> None:
        """Persist and publish one completed classic task."""
        answer = str(result.get("answer", ""))
        lease_token = self._task_lock_ids.get(task_id)
        try:
            rolled_up = await db.update_task_cost_totals(
                task_id, lease_token=lease_token,
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
        completed_subtasks = [
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
        ]
        with contextlib.suppress(Exception):
            await self._publish_task_state(
                task_id, user_task[:80], "completed", completed_subtasks,
            )
        with contextlib.suppress(Exception):
            await self.bb.publish_result(task_id, {
                "task_id": task_id,
                **result,
            })
        with contextlib.suppress(Exception):
            await self.bb.publish_event(task_id, "complete", {
                "answer": answer[:2000],
                "terminated_by": result.get("terminated_by"),
                "answer_source": result.get("answer_source"),
                "rounds_completed": result.get("rounds_completed"),
                "budget_spent": budget_spent,
            })
        with contextlib.suppress(Exception):
            await self.bb.publish_system_event("task-completed", {
                "task_id": task_id,
                "status": "completed",
                "label": user_task[:80],
            })

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
            "variant": "traditional",
            "terminated_by": result.get("terminated_by"),
            "answer_source": result.get("answer_source"),
            "verification_status": result.get("verification_status"),
            "rounds": result.get("rounds_completed"),
            "budget_spent": result.get("budget_spent", 0.0),
            "complexity": triage.complexity.value,
        }

    async def steer_entry(
        self, task_id: str, entry_id: str, action: str,
    ) -> dict[str, Any]:
        """Apply an operator change through the durable board gateway."""
        gateway = self._active_gateways.get(task_id)
        if gateway is None:
            from core.board_store import SqliteRedisBoardStore, make_board_persist_hook
            from core.event_emitter import RedisEventEmitter
            from core.gateway import BoardGateway, salience_recompute_hook

            store = SqliteRedisBoardStore()
            await store.load_task(task_id)
            gateway = BoardGateway(
                store,
                RedisEventEmitter(self.bb.redis),
                recompute_hooks=[
                    salience_recompute_hook,
                    make_board_persist_hook(self.bb),
                ],
            )
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
        """Dispatch one turn for the traditional variant.

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
        response = await self._dispatch_turn(
            role=activation.role,
            task_id=task_id,
            description=task["query"],
            persona=payload.get("role_prompt", ""),
            context={
                "board": payload.get("board"),
                "objective": payload.get("objective"),
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
            usage = response.get("usage")
            if usage:
                variant.track_cost(self._compute_cost(usage, MODEL_PRICING))
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
    ) -> dict:
        """HTTP dispatch to a Hermes agent node for the traditional variant.

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

        max_attempts = len(candidate_urls) + 2
        candidate_index = 0
        for attempt in range(max_attempts):
            url = candidate_urls[candidate_index]
            try:
                headers = (
                    {"Authorization": f"Bearer {BMAS_EXECUTE_KEY}"}
                    if BMAS_EXECUTE_KEY
                    else None
                )
                resp = await self.http.post(
                    f"{url}/execute",
                    json=payload,
                    headers=headers,
                    timeout=float(payload["timeout"]) + 15.0,
                )
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
                    pricing = MODEL_PRICING.get(model_used, {})
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
                    )
                return data

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
                    await db.complete_turn(turn_id, "failed", 0, 0.0)
                return {"task_id": task_id, "status": "failed", "result": str(e)}

        return {"task_id": task_id, "status": "failed", "result": "max retries"}  # pragma: no cover

    async def close(self):
        await self.bb.close()
        await self.triage.close()
        await self.http.aclose()
