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
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

import database as db
from config import (
    AGENT_ENDPOINTS,
    COORDINATION_VARIANT,
    LITELLM_KEY,
    LITELLM_URL,
    MODEL_PRICING,
    ROLE_REGISTRY,
    TRADITIONAL_CONFIG,
    TRIAGE_URL,
)
from config import (
    MODEL_ROUTING as CONFIG_MODEL_ROUTING,
)
from core.blackboard import Blackboard
from core.triage import MODEL_ROUTING, Complexity, TriageResult, TriageRouter  # Phase 4 §4.4
from models.personas import DEFAULT_PERSONAS, generate_expert_persona

logger = logging.getLogger("bmas.orchestrator")


class Orchestrator:
    def __init__(self):
        self.bb = Blackboard()
        self.triage = TriageRouter(
            triage_url=TRIAGE_URL,
            litellm_url=LITELLM_URL,
            litellm_key=LITELLM_KEY,
        )
        self.http = httpx.AsyncClient(timeout=120.0)

    async def _safe_log(self, node_id: str, message: str, task_id: str | None = None):
        """Log to Redis Streams AND SQLite with fallback.

        Redis write provides live SSE streaming to the dashboard.
        SQLite write provides permanent archival for task history.
        Neither failure interrupts the caller.
        """
        try:
            await self.bb.publish_log(node_id, message, task_id=task_id)
        except Exception:
            logger.warning(f"Redis log failed | {node_id}: {message}")

        if task_id:
            try:
                await db.insert_log_entry(task_id, node_id, "info", message)
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

    async def process_task(self, user_task: str, task_id: str | None = None) -> dict:
        """Main entry point: triage → plan → execute → audit → publish."""
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
                    await db.create_task(task_id, user_task[:80], user_task)
                except Exception as e:
                    logger.error(f"SQLite create_task failed for {task_id}: {e}")
                    # Continue — Redis still tracks the task for the live UI

            await self._set_phase("triage", 1, task_id=task_id)
            await self._safe_log("daemon", f"Processing: {task_id}", task_id=task_id)

            # Publish initial task state so the UI can show it
            await self._publish_task_state(task_id, user_task[:80], "running", [
                {"id": f"{task_id}-triage",  "label": "Triage classification", "status": "running",  "agent_role": "planner",  "depends_on": []},
                {"id": f"{task_id}-plan",    "label": "Plan decomposition",    "status": "pending",  "agent_role": "planner",  "depends_on": [f"{task_id}-triage"]},
                {"id": f"{task_id}-exec",    "label": "Execute sub-tasks",     "status": "pending",  "agent_role": "executor", "depends_on": [f"{task_id}-plan"]},
                {"id": f"{task_id}-audit",   "label": "Audit & consensus",     "status": "pending",  "agent_role": "auditor",  "depends_on": [f"{task_id}-exec"]},
            ])

            # 2. Triage complexity (fail-fast: default to MEDIUM if triage is unreachable)
            try:
                triage = await self.triage.classify(user_task)
            except Exception as e:
                await self._safe_log("daemon",
                    f"WARN: Triage unavailable ({e}), defaulting to MEDIUM", task_id=task_id)
                triage = TriageResult(
                    complexity=Complexity.MEDIUM,
                    litellm_model=MODEL_ROUTING.get(Complexity.MEDIUM, "medium"),
                )
            await self._safe_log("daemon",
                f"Triage: {triage.complexity.value} → {triage.litellm_model}", task_id=task_id)

            # Update task with triage result (SQLite)
            try:
                await db.update_task_status(
                    task_id,
                    status="running",
                    complexity=triage.complexity.value,
                    model_used=triage.litellm_model,
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

            # 3. Phase 3b: Traditional variant intercept (doc 05)
            # When coordination.variant == 'traditional', the cyclic
            # blackboard loop replaces both legacy pipelines.
            if COORDINATION_VARIANT == "traditional":
                return await self._run_traditional(
                    task_id, session_id, user_task, triage,
                )

            # 4. For COMPLEX tasks, use dynamic expert personas (legacy)
            if triage.complexity == Complexity.COMPLEX:
                return await self._complex_research_flow(
                    task_id, session_id, user_task
                )

            # 5. For SIMPLE/LIGHT/MEDIUM, use standard Planner→Executor→Auditor flow (legacy)
            return await self._standard_flow(task_id, session_id, user_task, triage)

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

    async def _standard_flow(
        self, task_id: str, session_id: str, user_task: str, triage
    ) -> dict:
        """Standard bMAS flow: Plan → Execute → Audit."""
        sub_tasks = [
            {"id": f"{task_id}-triage", "label": f"Triage: {triage.complexity.value}", "status": "completed", "agent_role": "planner",  "depends_on": []},
            {"id": f"{task_id}-plan",   "label": "Plan decomposition",    "status": "pending",   "agent_role": "planner",  "depends_on": [f"{task_id}-triage"]},
            {"id": f"{task_id}-exec",   "label": "Execute sub-tasks",     "status": "pending",   "agent_role": "executor", "depends_on": [f"{task_id}-plan"]},
            {"id": f"{task_id}-audit",  "label": "Audit & consensus",     "status": "pending",   "agent_role": "auditor",  "depends_on": [f"{task_id}-exec"]},
        ]

        # Step 1: Planner decomposes the task
        await self._check_abort(task_id)
        await self._set_phase("planning", 1, task_id=task_id)
        sub_tasks[1]["status"] = "running"
        await self._publish_task_state(task_id, user_task[:80], "running", sub_tasks)

        # Gather file attachments for context (doc 17 §4)
        attachment_context: dict | None = None
        try:
            from config import STORAGE_CONFIG, STORAGE_ENABLED
            if STORAGE_ENABLED:
                task_files = await db.get_task_files(task_id)
                if task_files:
                    preview_chars = int(STORAGE_CONFIG.get("attachment_preview_chars", 1500))
                    attachments = []
                    for tf in task_files:
                        att = {
                            "file_id": tf["id"],
                            "name": tf["name"],
                            "mime": tf["mime"],
                            "bytes": tf["bytes"],
                            "sha256": tf["sha256"],
                        }
                        # Include extracted text preview if available
                        if tf.get("extracted_chars", 0) > 0:
                            stored = tf.get("stored_path", "")
                            if stored:
                                try:
                                    # Read from sidecar extracted text file first
                                    text_path = stored + ".extracted.txt"
                                    if os.path.exists(text_path):
                                        with open(text_path, encoding="utf-8") as fh:
                                            full_text = fh.read()
                                        att["text_preview"] = full_text[:preview_chars]
                                    else:
                                        # Fallback: read raw file bytes and extract
                                        from file_utils import extract_text_file
                                        with open(stored, "rb") as fh:
                                            file_bytes = fh.read()
                                        preview = extract_text_file(file_bytes, max_chars=preview_chars)
                                        att["text_preview"] = preview
                                except Exception:
                                    pass
                        attachments.append(att)
                    attachment_context = {"_task_id": task_id, "attachments": attachments}
                    await self._safe_log("daemon",
                        f"Attachments: {len(attachments)} file(s) attached", task_id=task_id)
        except Exception as e:
            logger.warning(f"Attachment gathering failed for {task_id}: {e}")

        plan = await self._dispatch_agent(
            "planner", task_id, user_task, DEFAULT_PERSONAS["planner"],
            context=attachment_context,
            model=triage.litellm_model,
        )
        # Post planner debate entry via pub/sub (legacy pipeline transport)
        await self.bb.post_debate(session_id, "planner", plan.get("result", ""))
        try:
            await db.insert_debate_entry(task_id, session_id, "planner", plan.get("result", ""))
        except Exception:
            logger.warning("SQLite debate insert failed for %s/planner", task_id)
        with contextlib.suppress(Exception):
            await self.bb.publish_event(task_id, "debate", {
                "agent_role": "planner",
                "content": plan.get("result", ""),
                "created_at": datetime.now(UTC).isoformat(),
            })
        sub_tasks[1]["status"] = "completed"

        # Step 2: Executor handles sub-tasks
        await self._check_abort(task_id)
        await self._set_phase("executing", 2, task_id=task_id)
        sub_tasks[2]["status"] = "running"
        await self._publish_task_state(task_id, user_task[:80], "running", sub_tasks)

        exec_result = await self._dispatch_agent(
            "executor", task_id, plan.get("result", user_task),
            DEFAULT_PERSONAS["executor"],
            model=triage.litellm_model,
        )
        # Post executor debate entry
        await self.bb.post_debate(session_id, "executor", exec_result.get("result", ""))
        try:
            await db.insert_debate_entry(task_id, session_id, "executor", exec_result.get("result", ""))
        except Exception:
            logger.warning("SQLite debate insert failed for %s/executor", task_id)
        with contextlib.suppress(Exception):
            await self.bb.publish_event(task_id, "debate", {
                "agent_role": "executor",
                "content": exec_result.get("result", ""),
                "created_at": datetime.now(UTC).isoformat(),
            })
        sub_tasks[2]["status"] = "completed"

        # Step 3: Auditor reviews, resolves, cleans
        await self._check_abort(task_id)
        await self._set_phase("auditing", 3, task_id=task_id)
        sub_tasks[3]["status"] = "running"
        await self._publish_task_state(task_id, user_task[:80], "running", sub_tasks)

        debate = await self.bb.get_debate(session_id)
        audit_context = json.dumps(debate, indent=2)
        audit = await self._dispatch_agent(
            "auditor", task_id,
            f"Review this debate and produce consensus:\n\n{audit_context}",
            DEFAULT_PERSONAS["auditor"],
            model=triage.litellm_model,
        )
        sub_tasks[3]["status"] = "completed"

        # Step 4: Publish consensus and cleanup
        await self._set_phase("finalizing", 4, task_id=task_id)
        await self._publish_task_state(task_id, user_task[:80], "completed", sub_tasks)
        result_data = {
            "consensus": audit.get("result", ""),
            "triage": triage.complexity.value,
        }
        await self.bb.publish_result(task_id, result_data)
        await self.bb.clear_private(session_id)
        await self._safe_log("daemon", f"Completed: {task_id}", task_id=task_id)

        # Persist final result in SQLite
        try:
            await db.complete_task(
                task_id,
                result_summary=audit.get("result", ""),
                result_json=json.dumps(result_data),
            )
            await db.update_task_cost_totals(task_id)
        except Exception as e:
            logger.warning("SQLite complete_task failed for %s: %s", task_id, e)

        with contextlib.suppress(Exception):
            await self.bb.publish_event(task_id, "complete", {
                "result_summary": audit.get("result", ""),
                "result_json": result_data,
                "duration_ms": None,
                "total_cost_usd": None,
            })

        with contextlib.suppress(Exception):
            await self.bb.publish_system_event("task-completed", {
                "task_id": task_id, "status": "completed", "label": user_task[:80]
            })

        return {"task_id": task_id, "result": audit.get("result", "")}

    async def _complex_research_flow(
        self, task_id: str, session_id: str, user_task: str
    ) -> dict:
        """Dynamic expert persona flow for complex research tasks."""
        await self._safe_log("daemon", "Activating dynamic expert personas", task_id=task_id)

        # Use Gemini Pro to generate 3 expert personas (fail-fast with defaults
        # if Gemini Pro is unreachable or returns malformed JSON)
        default_experts = [
            {"domain": "Systems Architecture", "expertise": "distributed systems design"},
            {"domain": "Domain Analysis", "expertise": "domain-specific subject matter"},
            {"domain": "Quality Assurance", "expertise": "verification and validation"},
        ]
        try:
            persona_response = await self.http.post(
                f"{LITELLM_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                json={
                    "model": MODEL_ROUTING.get(Complexity.COMPLEX, "gemini-pro"),
                    "messages": [{
                        "role": "user",
                        "content": f"""Given this research task, identify exactly 3 expert domains needed.
Respond in JSON: {{"experts": [{{"domain": "...", "expertise": "..."}}]}}

Task: {user_task}"""
                    }],
                    "max_tokens": 256,
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            )
            persona_response.raise_for_status()
            experts = json.loads(
                persona_response.json()["choices"][0]["message"]["content"]
            )["experts"][:3]
        except Exception as e:
            await self._safe_log("daemon",
                f"WARN: Expert persona generation failed ({e}), using defaults", task_id=task_id)
            experts = default_experts

        # Inject expert personas and run parallel debate
        await self._check_abort(task_id)
        roles = ["planner", "executor", "auditor"]
        tasks = []
        for _i, (expert, role) in enumerate(zip(experts, roles, strict=False)):
            persona = generate_expert_persona(
                expert["domain"], expert["expertise"], user_task
            )
            tasks.append(
                self._dispatch_agent(
                    role, task_id, user_task, persona,
                    model=MODEL_ROUTING.get(Complexity.COMPLEX, "gemini-pro"),
                )
            )

        results = await asyncio.gather(*tasks)

        # Post all debate entries via pub/sub
        for expert, result in zip(experts, results, strict=False):
            await self.bb.post_debate(
                session_id, expert["domain"], result.get("result", "")
            )
            try:
                await db.insert_debate_entry(
                    task_id, session_id, expert["domain"], result.get("result", "")
                )
            except Exception:
                logger.warning("SQLite debate insert failed for %s/%s", task_id, expert["domain"])
            with contextlib.suppress(Exception):
                await self.bb.publish_event(task_id, "debate", {
                    "agent_role": expert["domain"],
                    "content": result.get("result", ""),
                    "created_at": datetime.now(UTC).isoformat(),
                })

        # Revert to Auditor persona for synthesis
        await self._check_abort(task_id)
        debate = await self.bb.get_debate(session_id)
        synthesis = await self._dispatch_agent(
            "auditor", task_id,
            f"Synthesize these expert perspectives into a unified analysis:\n\n{json.dumps(debate, indent=2)}",
            DEFAULT_PERSONAS["auditor"],
            model=MODEL_ROUTING.get(Complexity.COMPLEX, "gemini-pro"),
        )

        result_data = {
            "consensus": synthesis.get("result", ""),
            "experts_used": [e["domain"] for e in experts],
            "triage": "complex",
        }
        await self.bb.publish_result(task_id, result_data)
        await self.bb.clear_private(session_id)

        # Persist final result in SQLite
        try:
            await db.complete_task(
                task_id,
                result_summary=synthesis.get("result", ""),
                result_json=json.dumps(result_data),
            )
            await db.update_task_cost_totals(task_id)
        except Exception as e:
            logger.warning(f"SQLite complete_task failed for {task_id}: {e}")

        with contextlib.suppress(Exception):
            await self.bb.publish_event(task_id, "complete", {
                "result_summary": synthesis.get("result", ""),
                "result_json": result_data,
                "duration_ms": None,
                "total_cost_usd": None,
            })

        with contextlib.suppress(Exception):
            await self.bb.publish_system_event("task-completed", {
                "task_id": task_id, "status": "completed", "label": user_task[:80]
            })

        return {"task_id": task_id, "result": synthesis.get("result", "")}

    async def _dispatch_agent(
        self, role: str, task_id: str, description: str, persona: str,
        context: dict | None = None,
        model: str | None = None,
    ) -> dict:
        """Send a task to a Hermes Agent node via its REST API.

        Payload matches the TaskRequest schema from Phase 1: task_id,
        description, role_prompt, context, timeout, turn_id, model, role.

        Phase 1 additions:
        - Sends turn_id, model, and role for trace correlation
        - Reads v2 response fields: usage, trace_count, entries, artifacts
        - Computes cost_usd DAEMON-SIDE from MODEL_PRICING (doc 06 §3.1)
          ONLY when trace_count == 0 (fallback path). When traces are
          present, the ingest endpoint is the sole cost authority (B2 fix).
        - Creates turn records for trace tracking

        Retries up to 3 times with exponential backoff to handle Hermes
        cold-start delays (Gotcha #2 — first call after restart takes 5-15s).
        """
        # Phase 3a: Resolve profile and endpoint from the role registry
        # (doc 12 §2.5). Falls back to AGENT_ENDPOINTS for roles not in
        # the registry (backward compatible with legacy dispatch).
        _reg = ROLE_REGISTRY.get(role, {})
        _profile = _reg.get("profile")
        if _reg and _reg.get("endpoints"):
            url = _reg["endpoints"][0]  # preferred host first, fallbacks after
        else:
            url = AGENT_ENDPOINTS[role]

        turn_id = f"turn-{str(uuid.uuid4())[:8]}"

        payload: dict[str, Any] = {
            "task_id": task_id,
            "description": description,
            "role_prompt": persona,
            # Phase 1 additions (B1 fix: model is now sent)
            "turn_id": turn_id,
            "role": role,
            "model": model,
            # Phase 3a: Hermes profile for role-scoped SOUL/toolset isolation
            "profile": _profile,
        }
        if context:
            payload["context"] = context

        # Create turn record (best-effort)
        try:
            await db.create_turn({
                "id": turn_id,
                "task_id": task_id,
                "round_no": 1,
                "role": role,
                "node": url,
                "model": model,
                "status": "running",
            })
        except Exception as e:
            logger.warning(f"Turn create failed for {task_id}/{turn_id}: {e}")

        for attempt in range(3):
            try:
                response = await self.http.post(
                    f"{url}/execute", json=payload,
                )
                response.raise_for_status()
                response_data = response.json()

                # ── Phase 1: v2 cost computation (daemon-side) ──────────
                #
                # B2 fix: The ingest endpoint is the sole cost authority
                # when traces are present (trace_count > 0). Only record
                # cost HERE for the fallback path (trace_count == 0) where
                # no traces reach the ingest endpoint.
                usage = response_data.get("usage")
                trace_count = response_data.get("trace_count", 0)
                model_used = "unknown"
                cost_usd = 0.0

                if usage and isinstance(usage, dict):
                    model_used = usage.get("model", response_data.get("model", model or "unknown"))
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)

                    # Compute cost from MODEL_PRICING (daemon-side)
                    pricing = MODEL_PRICING.get(model_used, {})
                    if pricing:
                        cost_usd = (
                            prompt_tokens * float(pricing.get("input_cost_per_token", 0))
                            + completion_tokens * float(pricing.get("output_cost_per_token", 0))
                        )
                        cost_usd = round(cost_usd, 8)
                        price_source = str(pricing.get("source", "bmas.yaml"))
                    else:
                        price_source = "missing"
                        if model_used != "unknown":
                            logger.warning(
                                f"No pricing for model '{model_used}' — cost_usd=0.0"
                            )

                    # B2 fix: Only insert cost entry when no traces flowed
                    # through the ingest endpoint (fallback path). When
                    # trace_count > 0, the ingest endpoint already recorded
                    # cost from the final trace's usage.
                    if trace_count == 0:
                        with contextlib.suppress(Exception):  # Cost tracking is best-effort
                            await db.insert_cost_entry_v2(
                                task_id=task_id,
                                model=model_used,
                                input_tokens=prompt_tokens,
                                output_tokens=completion_tokens,
                                cost_usd=cost_usd,
                                phase=None,
                                node_id=response_data.get("node_id"),
                                turn_id=response_data.get("turn_id", turn_id),
                                provider=None,
                                price_source=price_source,
                                joules_estimate=0.0,  # Beszel stub
                            )

                        with contextlib.suppress(Exception):
                            await self.bb.publish_event(task_id, "cost", {
                                "model": model_used,
                                "input_tokens": prompt_tokens,
                                "output_tokens": completion_tokens,
                                "cost_usd": cost_usd,
                                "node_id": response_data.get("node_id"),
                                "turn_id": response_data.get("turn_id", turn_id),
                                "price_source": price_source,
                            })
                    else:
                        logger.debug(
                            f"Skipping cost insert for {task_id}/{turn_id} — "
                            f"trace_count={trace_count}, ingest endpoint owns cost"
                        )

                elif usage is None:
                    # Legacy hermes -z fallback — usage unknown
                    logger.debug(
                        f"No usage in response for {task_id}/{role} "
                        f"(likely hermes -z fallback)"
                    )

                # Complete the turn record
                # B3 fix: map declined to 'declined', not 'failed'.
                # Declined is a first-class blackboard behavior (doc 03 §4).
                try:
                    agent_status = response_data.get("status", "")
                    if agent_status == "completed":
                        turn_status = "completed"
                    elif agent_status == "declined":
                        turn_status = "declined"
                    else:
                        turn_status = "failed"

                    await db.complete_turn(
                        turn_id=turn_id,
                        status=turn_status,
                        entries_added=len(response_data.get("entries") or []),
                        cost_usd=cost_usd,
                        joules_estimate=0.0,
                    )
                except Exception as e:
                    logger.warning(f"Turn complete failed for {turn_id}: {e}")

                return response_data
            except Exception as e:
                if attempt < 2:
                    delay = 2 ** attempt  # 1s, 2s
                    await self._safe_log(role,
                        f"Retry {attempt + 1} of 2 after {delay}s: {e}", task_id=task_id)
                    await asyncio.sleep(delay)
                    continue
                await self._safe_log(role, f"ERROR after 3 attempts: {e}", task_id=task_id)

                # Complete turn as failed
                with contextlib.suppress(Exception):
                    await db.complete_turn(turn_id, "failed", 0, 0.0)

                return {"task_id": task_id, "status": "failed", "result": str(e)}

        # Unreachable — loop always returns, but satisfies mypy [return]
        return {"task_id": task_id, "status": "failed", "result": "max retries"}  # pragma: no cover

    # ── Traditional Variant Integration (Phase 3b, doc 05) ────────────

    async def _run_traditional(
        self, task_id: str, session_id: str, user_task: str, triage: TriageResult,
    ) -> dict:
        """Run the paper's cyclic blackboard loop (doc 05).

        The orchestrator owns lifecycle (lock, abort, events, SQLite).
        The TraditionalVariant owns the loop (genesis, step, finalize).
        CU and AG calls are control-plane LiteLLM calls, never Hermes runs.
        """
        from config import MODEL_PRICING
        from core.board_store import InMemoryBoardStore
        from core.event_emitter import RedisEventEmitter
        from core.gateway import BoardGateway, salience_recompute_hook
        from core.variants.traditional import TraditionalVariant

        await self._safe_log("daemon",
            f"Traditional variant | tier={triage.complexity.value}", task_id=task_id)

        # Boot board infrastructure
        # Use RedisEventEmitter so board_entry / entry_removed SSE events
        # flow through Redis Pub/Sub → SSE endpoint → frontend.
        board_store = InMemoryBoardStore()
        event_emitter = RedisEventEmitter(self.bb.redis)
        gateway = BoardGateway(
            board_store, event_emitter,
            recompute_hooks=[salience_recompute_hook],
        )

        # Build node endpoint list
        node_endpoints = list({ep for ep in AGENT_ENDPOINTS.values()})

        variant = TraditionalVariant(
            gateway=gateway,
            board_store=board_store,
            event_emitter=event_emitter,
            triage=self.triage,
            config=dict(TRADITIONAL_CONFIG),
            litellm_url=LITELLM_URL,
            litellm_key=LITELLM_KEY,
            node_endpoints=node_endpoints,
            role_registry=dict(ROLE_REGISTRY),
            model_routing=dict(CONFIG_MODEL_ROUTING),
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

            await self._safe_log("daemon",
                f"Genesis complete | roster={len(variant.roster.all_actors()) if variant.roster else 0} agents",
                task_id=task_id)

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
                        task_id=task_id)
                    break

                # Dispatch activations concurrently
                if step_result.activations:
                    dispatch_tasks = []
                    for activation in step_result.activations:
                        dispatch_tasks.append(
                            self._dispatch_traditional_turn(
                                variant, task, activation, round_no,
                            )
                        )
                    results = await asyncio.gather(
                        *dispatch_tasks, return_exceptions=True,
                    )

                    # Process results and track cost
                    for activation, result in zip(step_result.activations, results, strict=False):
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
                        task_id=task_id)

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
    ) -> dict:
        """Dispatch one turn for the traditional variant.

        Uses build_turn_payload → _dispatch_agent → parse_agent_response → apply.
        Emits turn_start/turn_end SSE events for WorkerLane + AgentTrace.
        """
        task_id = task["task_id"]
        board = await variant.store.get_snapshot(task_id)

        # Build payload
        payload = variant.build_turn_payload(task, activation.actor, board)
        payload["model"] = activation.model
        turn_id = payload.get("turn_id", "")

        # Emit turn_start SSE event for WorkerLane/AgentTrace
        with contextlib.suppress(Exception):  # SSE is best-effort
            await self.bb.publish_event(task_id, "turn_start", {
                "turn_id": turn_id,
                "actor": activation.actor,
                "round": round_no,
                "model": activation.model,
                "node": activation.node_endpoint,
            })

        # Dispatch to agent node
        response = await self._dispatch_agent(
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
        )

        # Parse response into board entries
        entries = variant.parse_agent_response(task, activation.actor, response)

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
                else:
                    mutation["entries"] = [entry]
                await variant.apply(task, [mutation])

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

    async def close(self):
        await self.bb.close()
        await self.triage.close()
        await self.http.aclose()
