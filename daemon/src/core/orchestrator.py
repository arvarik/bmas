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
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

import database as db
from config import (
    AGENT_ENDPOINTS,
    COORDINATION_VARIANT,
    EDGE_NODE_MODELS,
    LITELLM_KEY,
    LITELLM_URL,
    MODEL_PRICING,
    ROLE_REGISTRY,
    TRADITIONAL_CONFIG,
    TRIAGE_URL,
)
from core.blackboard import Blackboard, normalize_level
from core.triage import MODEL_ROUTING, Complexity, TriageResult, TriageRouter

logger = logging.getLogger("bmas.orchestrator")


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
            await self.bb.redis.hset("bmas:public:state", mapping={
                "phase": phase,
                "iteration": str(iteration),
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
    ) -> dict:
        """Main entry point: triage → plan → execute → audit → publish.

        Args:
            user_task: The raw user task description.
            task_id: Optional pre-assigned task ID (created by submit endpoint).
            overrides: Optional per-task settings overrides (session-only, not persisted).
                Keys: 'routing' (dict[tier, model]), 'role_registry' (dict[role, entry]).
        """
        session_id = str(uuid.uuid4())[:8]
        if task_id is None:
            task_id = f"task-{session_id}"

        # 1. Acquire global lock
        acquired, lock_id = await self.bb.acquire_lock(f"orchestrator:{task_id}")
        if not acquired:
            return {"error": "Could not acquire lock — another task is running"}

        try:
            # Emit task-started system event
            with contextlib.suppress(Exception):
                await self.bb.publish_system_event("task-started", {
                    "task_id": task_id, "label": user_task[:80]
                })

            # Create persistent task record (SQLite)
            # Only create if we generated the ID here.
            # If task_id was passed, it was already created by the async submit endpoint.
            if task_id == f"task-{session_id}":
                try:
                    await db.create_task(task_id, user_task[:80], user_task,
                                         variant=COORDINATION_VARIANT)
                except Exception as e:
                    logger.error(f"SQLite create_task failed for {task_id}: {e}")
                    # Continue — Redis still tracks the task for the live UI

            await self._set_phase("triage", 1, task_id=task_id)
            await self._safe_log("daemon", f"Processing: {task_id}", task_id=task_id)

            # Log per-task overrides if provided
            if overrides:
                await self._safe_log("daemon",
                    f"Per-task overrides applied: {list(overrides.keys())}",
                    task_id=task_id, level="info",
                    fields={"event": "task_overrides", "overrides": overrides})

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
                await db.update_task_status(
                    task_id,
                    status="running",
                    complexity=triage.complexity.value,
                    model_used=triage.litellm_model,
                    variant=COORDINATION_VARIANT,  # stamp correct variant, not schema default
                )
            except Exception as e:
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
            )

        except Exception as e:
            # Record failure in SQLite before re-raising
            try:
                await db.fail_task(task_id, str(e))
            except Exception:
                logger.warning(f"SQLite fail_task failed for {task_id}")
            
            # Emit error event
            with contextlib.suppress(Exception):
                await self.bb.publish_event(task_id, "error", {
                    "error_message": str(e)
                })

            # Emit system task-completed (failed) event
            with contextlib.suppress(Exception):
                await self.bb.publish_system_event("task-completed", {
                    "task_id": task_id, "status": "failed", "label": user_task[:80]
                })

            raise

        finally:
            await self._set_phase("idle", 0, task_id=task_id)
            await self.bb.release_lock(f"orchestrator:{task_id}", lock_id)

    # ── Traditional Variant Integration (doc 05) ──────────────────────

    async def _run_traditional(
        self,
        task_id: str,
        session_id: str,
        user_task: str,
        triage: TriageResult,
        *,
        overrides: dict | None = None,
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

        from config import MODEL_PRICING
        from core.board_store import InMemoryBoardStore, make_board_persist_hook
        from core.event_emitter import RedisEventEmitter
        from core.gateway import BoardGateway, salience_recompute_hook
        from core.variants.traditional import TraditionalVariant
        from settings_store import get_store as _get_store

        await self._safe_log("daemon",
            f"Traditional variant | tier={triage.complexity.value}", task_id=task_id)

        # Boot board infrastructure
        # Use RedisEventEmitter so board_entry / entry_removed SSE events
        # flow through Redis Pub/Sub → SSE endpoint → frontend.
        board_store = InMemoryBoardStore()
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
        )

        # Build node endpoint list
        node_endpoints = list({ep for ep in AGENT_ENDPOINTS.values()})

        # ── Effective settings: session overrides → per-task overrides ──
        _store = _get_store()
        # Routing: session store provides the base, per-task overrides on top
        effective_routing = await _store.get_routing()
        if overrides and overrides.get("routing"):
            effective_routing.update(overrides["routing"])

        # Role registry: session store provides the base, per-task overrides on top
        effective_registry = await _store.get_role_registry()
        if overrides and overrides.get("role_registry"):
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
            config=dict(TRADITIONAL_CONFIG),
            litellm_url=LITELLM_URL,
            litellm_key=LITELLM_KEY,
            node_endpoints=node_endpoints,
            role_registry=effective_registry,
            model_routing=effective_routing,
            edge_node_models=EDGE_NODE_MODELS,
        )

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
            await variant.genesis(task)

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
            for round_no in range(1, variant.max_rounds + 2):  # +2 for safety
                await self._check_abort(task_id)

                # Phase 5: Inject operator directives (doc 05 §6)
                await variant.inject_directives(task_id)

                # Phase 5: Check pause-at-round-boundary (doc 05 §6)
                await variant.check_pause(task_id)

                await self._set_phase("round", round_no, task_id=task_id)

                # Step: deterministic guards → CU selection → activations
                board = await board_store.get_snapshot(task_id)
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

                    # Phase 1: dispatch non-decider agents concurrently
                    if non_decider:
                        dispatch_tasks = [
                            self._dispatch_traditional_turn(
                                variant, task, activation, round_no,
                                rationale=step_result.rationale,
                                phase=step_result.phase,
                            )
                            for activation in non_decider
                        ]
                        phase1_results = await asyncio.gather(
                            *dispatch_tasks, return_exceptions=True,
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
                        dispatch_tasks = [
                            self._dispatch_traditional_turn(
                                variant, task, activation, round_no,
                                rationale=step_result.rationale,
                                phase=step_result.phase,
                            )
                            for activation in decider
                        ]
                        phase2_results = await asyncio.gather(
                            *dispatch_tasks, return_exceptions=True,
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
                        if isinstance(result, dict):
                            # Track cost from response usage
                            usage = result.get("usage")
                            if usage:
                                cost = self._compute_cost(usage, MODEL_PRICING)
                                variant.track_cost(cost)
                                await gateway.set_meta(
                                    task_id, budget_spent=variant.budget_spent,
                                )

                            # Phase 5: Store response_id for stateful turns
                            response_id = result.get("response_id")
                            if response_id:
                                variant.set_response_id(
                                    activation.actor, response_id,
                                )

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
                await variant.emit_budget_event(task_id)
            else:
                from core.variants.traditional import StepResult
                step_result = StepResult(terminal=True, reason="max_rounds")

            # ── Finalize ─────────────────────────────────────────────
            await self._set_phase("finalize", 0, task_id=task_id)
            result = await variant.finalize(
                task, board, step_result.reason or "unknown",
            )

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

            # Record final answer
            answer = result.get("answer", "")
            try:
                await db.complete_task(
                    task_id,
                    result_summary=answer[:10000],
                    result_json=json.dumps(result),
                )
                await db.update_task_cost_totals(task_id)
            except Exception as e:
                logger.warning(f"SQLite complete_task failed for {task_id}: {e}")

            # Emit completion events
            try:
                await self.bb.publish_event(task_id, "complete", {
                    "answer": answer[:2000],
                    "terminated_by": result.get("terminated_by"),
                    "answer_source": result.get("answer_source"),
                    "rounds_completed": result.get("rounds_completed"),
                    "budget_spent": variant.budget_spent,
                })
                await self.bb.publish_system_event("task-completed", {
                    "task_id": task_id,
                    "status": "completed",
                    "label": user_task[:80],
                })
            except Exception:
                pass

            return {
                "task_id": task_id,
                "answer": answer,
                "variant": "traditional",
                "terminated_by": result.get("terminated_by"),
                "answer_source": result.get("answer_source"),
                "rounds": result.get("rounds_completed"),
                "budget_spent": variant.budget_spent,
                "complexity": triage.complexity.value,
            }

        finally:
            await variant.close()

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
        turn_id = payload.get("turn_id", "")

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
        )

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
        entries = variant.parse_agent_response(task, activation.actor, response, known_ids=known_ids)

        # Apply through gateway (if agent contributed anything)
        if entries:
            for entry in entries:
                mutation = {
                    "actor": activation.actor,
                    "turn_id": turn_id,
                    "round": round_no,
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
                    await variant.apply(task, [mutation])
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

        # Emit turn_end SSE event
        try:
            turn_status = response.get("status", "completed") if isinstance(response, dict) else "completed"
            await self.bb.publish_event(task_id, "turn_end", {
                "turn_id": turn_id,
                "actor": activation.actor,
                "round": round_no,
                "status": turn_status,
                "entries_added": len(entries),
            })
        except Exception:
            pass  # SSE is best-effort

        return response

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
        if _reg and _reg.get("endpoints"):
            url = _reg["endpoints"][0]
        else:
            url = AGENT_ENDPOINTS.get(role, "")

        turn_id = f"turn-{str(uuid.uuid4())[:8]}"
        payload: dict[str, Any] = {
            "task_id": task_id,
            "description": description,
            "role_prompt": persona,
            "turn_id": turn_id,
            "role": role,
            "model": model,
            "profile": _reg.get("profile"),
        }
        if context:
            payload["context"] = context

        try:
            await db.create_turn({
                "id": turn_id, "task_id": task_id, "round_no": round_no,
                "role": role, "actor": actor or role,
                "node": url, "model": model, "status": "running",
                "rationale": rationale, "phase": phase,
            })
        except Exception as e:
            logger.warning(f"Turn create failed {task_id}/{turn_id}: {e}")

        for attempt in range(3):
            try:
                resp = await self.http.post(f"{url}/execute", json=payload)
                resp.raise_for_status()
                data = resp.json()

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
                    if data.get("trace_count", 0) == 0:
                        with contextlib.suppress(Exception):
                            await db.insert_cost_entry_v2(
                                task_id=task_id, model=model_used,
                                input_tokens=usage.get("prompt_tokens", 0),
                                output_tokens=usage.get("completion_tokens", 0),
                                cost_usd=cost_usd, phase=None,
                                node_id=data.get("node_id"),
                                turn_id=data.get("turn_id", turn_id),
                                provider=None, price_source="bmas.yaml",
                                joules_estimate=0.0,
                            )

                agent_status = data.get("status", "")
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
                if attempt < 2:
                    delay = 2 ** attempt
                    await self._safe_log(role,
                        f"Retry {attempt + 1}/2 after {delay}s: {e}", task_id=task_id,
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
                await self._safe_log(role, f"ERROR after 3 attempts: {e}", task_id=task_id,
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
