# /opt/bmas/daemon/orchestrator.py
"""
bMAS Orchestrator: decomposes tasks, dispatches to agents, manages debate cycles.

Dual-write pattern: Every lifecycle event writes to both Redis (real-time
blackboard for live UI) and SQLite (permanent task history). SQLite writes
are best-effort — they log warnings on failure but never interrupt a running task.
"""

import logging
import uuid
import asyncio
import json
import httpx
from typing import Optional

from datetime import datetime, timezone

from core.blackboard import Blackboard
from core.triage import TriageRouter, TriageResult, Complexity, MODEL_ROUTING  # Phase 4 §4.4
from models.personas import DEFAULT_PERSONAS, generate_expert_persona
from config import AGENT_ENDPOINTS, LITELLM_URL, LITELLM_KEY, TRIAGE_URL
import database as db

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
        try:
            await self.bb.redis.hset("bmas:public:state", mapping={
                "phase": phase,
                "iteration": str(iteration),
            })
        except Exception:
            pass

        if task_id:
            try:
                await self.bb.publish_event(task_id, "phase", {
                    "phase": phase, "iteration": iteration
                })
            except Exception:
                pass

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
                raise RuntimeError(f"Task aborted by operator")
        except RuntimeError:
            raise  # Re-raise the abort — don't swallow it
        except Exception:
            pass  # Redis read failure is non-fatal

    async def _publish_task_state(self, task_id: str, label: str, status: str,
                                  sub_tasks: list[dict] | None = None):
        """Write task state to Redis (real-time) AND sub-tasks to SQLite (persistent)."""
        now = datetime.now(timezone.utc).isoformat()
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
                try:
                    await self.bb.publish_event(task_id, "subtask", {
                        "id": st["id"],
                        "label": st.get("label", ""),
                        "status": st.get("status", "pending"),
                        "agent_role": st.get("agent_role", "unknown"),
                    })
                except Exception:
                    pass

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
            try:
                await self.bb.publish_system_event("task-started", {
                    "task_id": task_id, "label": user_task[:80]
                })
            except Exception:
                pass

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

            # 3. For COMPLEX tasks, use dynamic expert personas
            if triage.complexity == Complexity.COMPLEX:
                return await self._complex_research_flow(
                    task_id, session_id, user_task
                )

            # 4. For SIMPLE/LIGHT/MEDIUM, use standard Planner→Executor→Auditor flow
            return await self._standard_flow(task_id, session_id, user_task, triage)

        except Exception as e:
            # Record failure in SQLite before re-raising
            try:
                await db.fail_task(task_id, str(e))
            except Exception:
                logger.warning(f"SQLite fail_task failed for {task_id}")
            
            # Emit error event
            try:
                await self.bb.publish_event(task_id, "error", {
                    "error_message": str(e)
                })
            except Exception:
                pass

            # Emit system task-completed (failed) event
            try:
                await self.bb.publish_system_event("task-completed", {
                    "task_id": task_id, "status": "failed", "label": user_task[:80]
                })
            except Exception:
                pass

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

        plan = await self._dispatch_agent(
            "planner", task_id, user_task, DEFAULT_PERSONAS["planner"]
        )
        # Dual-write debate: Redis (ephemeral) + SQLite (permanent)
        await self.bb.post_debate(session_id, "planner", plan.get("result", ""))
        try:
            await db.insert_debate_entry(task_id, session_id, "planner", plan.get("result", ""))
        except Exception:
            logger.warning(f"SQLite debate insert failed for {task_id}/planner")
        try:
            await self.bb.publish_event(task_id, "debate", {
                "agent_role": "planner",
                "content": plan.get("result", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
        sub_tasks[1]["status"] = "completed"

        # Step 2: Executor handles sub-tasks
        await self._check_abort(task_id)
        await self._set_phase("executing", 2, task_id=task_id)
        sub_tasks[2]["status"] = "running"
        await self._publish_task_state(task_id, user_task[:80], "running", sub_tasks)

        exec_result = await self._dispatch_agent(
            "executor", task_id, plan.get("result", user_task),
            DEFAULT_PERSONAS["executor"]
        )
        # Dual-write debate
        await self.bb.post_debate(session_id, "executor", exec_result.get("result", ""))
        try:
            await db.insert_debate_entry(task_id, session_id, "executor", exec_result.get("result", ""))
        except Exception:
            logger.warning(f"SQLite debate insert failed for {task_id}/executor")
        try:
            await self.bb.publish_event(task_id, "debate", {
                "agent_role": "executor",
                "content": exec_result.get("result", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
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
            DEFAULT_PERSONAS["auditor"]
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
            logger.warning(f"SQLite complete_task failed for {task_id}: {e}")

        try:
            await self.bb.publish_event(task_id, "complete", {
                "result_summary": audit.get("result", ""),
                "result_json": result_data,
                "duration_ms": None,
                "total_cost_usd": None,
            })
        except Exception:
            pass

        try:
            await self.bb.publish_system_event("task-completed", {
                "task_id": task_id, "status": "completed", "label": user_task[:80]
            })
        except Exception:
            pass

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
        for i, (expert, role) in enumerate(zip(experts, roles)):
            persona = generate_expert_persona(
                expert["domain"], expert["expertise"], user_task
            )
            tasks.append(
                self._dispatch_agent(role, task_id, user_task, persona)
            )

        results = await asyncio.gather(*tasks)

        # Post all debate entries — dual-write Redis + SQLite
        for expert, result in zip(experts, results):
            await self.bb.post_debate(
                session_id, expert["domain"], result.get("result", "")
            )
            try:
                await db.insert_debate_entry(
                    task_id, session_id, expert["domain"], result.get("result", "")
                )
            except Exception:
                logger.warning(f"SQLite debate insert failed for {task_id}/{expert['domain']}")
            try:
                await self.bb.publish_event(task_id, "debate", {
                    "agent_role": expert["domain"],
                    "content": result.get("result", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

        # Revert to Auditor persona for synthesis
        await self._check_abort(task_id)
        debate = await self.bb.get_debate(session_id)
        synthesis = await self._dispatch_agent(
            "auditor", task_id,
            f"Synthesize these expert perspectives into a unified analysis:\n\n{json.dumps(debate, indent=2)}",
            DEFAULT_PERSONAS["auditor"]
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

        try:
            await self.bb.publish_event(task_id, "complete", {
                "result_summary": synthesis.get("result", ""),
                "result_json": result_data,
                "duration_ms": None,
                "total_cost_usd": None,
            })
        except Exception:
            pass

        try:
            await self.bb.publish_system_event("task-completed", {
                "task_id": task_id, "status": "completed", "label": user_task[:80]
            })
        except Exception:
            pass

        return {"task_id": task_id, "result": synthesis.get("result", "")}

    async def _dispatch_agent(
        self, role: str, task_id: str, description: str, persona: str,
        context: dict | None = None,
    ) -> dict:
        """Send a task to a Hermes Agent node via its REST API.

        Payload matches the TaskRequest schema from Phase 1: task_id,
        description, role_prompt, context, timeout, turn_id, model, role.

        Phase 1 additions:
        - Sends turn_id, model, and role for trace correlation
        - Reads v2 response fields: usage, trace_count, entries, artifacts
        - Computes cost_usd DAEMON-SIDE from MODEL_PRICING (doc 06 §3.1)
        - Creates turn records for trace tracking

        Retries up to 3 times with exponential backoff to handle Hermes
        cold-start delays (Gotcha #2 — first call after restart takes 5-15s).
        """
        from config import MODEL_PRICING

        url = AGENT_ENDPOINTS[role]
        turn_id = f"turn-{str(uuid.uuid4())[:8]}"

        payload = {
            "task_id": task_id,
            "description": description,
            "role_prompt": persona,
            # Phase 1 additions
            "turn_id": turn_id,
            "role": role,
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
                "model": None,  # Set from response
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
                usage = response_data.get("usage")
                model_used = "unknown"
                cost_usd = 0.0

                if usage and isinstance(usage, dict):
                    model_used = usage.get("model", response_data.get("model", "unknown"))
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

                    # Insert v2 cost entry with extended columns
                    try:
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
                    except Exception:
                        pass  # Cost tracking is best-effort

                    try:
                        await self.bb.publish_event(task_id, "cost", {
                            "model": model_used,
                            "input_tokens": prompt_tokens,
                            "output_tokens": completion_tokens,
                            "cost_usd": cost_usd,
                            "node_id": response_data.get("node_id"),
                            "turn_id": response_data.get("turn_id", turn_id),
                            "price_source": price_source,
                        })
                    except Exception:
                        pass

                elif usage is None:
                    # Legacy hermes -z fallback — usage unknown
                    logger.debug(
                        f"No usage in response for {task_id}/{role} "
                        f"(likely hermes -z fallback)"
                    )

                # Complete the turn record
                trace_count = response_data.get("trace_count", 0)
                try:
                    turn_status = (
                        "completed" if response_data.get("status") == "completed"
                        else "failed"
                    )
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
                try:
                    await db.complete_turn(turn_id, "failed", 0, 0.0)
                except Exception:
                    pass

                return {"task_id": task_id, "status": "failed", "result": str(e)}

    async def close(self):
        await self.bb.close()
        await self.triage.close()
        await self.http.aclose()
