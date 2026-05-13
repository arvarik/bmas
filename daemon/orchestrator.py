# /opt/bmas/daemon/orchestrator.py
"""
bMAS Orchestrator: decomposes tasks, dispatches to agents, manages debate cycles.
"""

import logging
import uuid
import asyncio
import json
import httpx
from typing import Optional

from datetime import datetime, timezone

from blackboard import Blackboard
from triage_router import TriageRouter, TriageResult, Complexity  # Phase 4 §4.4
from personas import DEFAULT_PERSONAS, generate_expert_persona
from config import AGENT_ENDPOINTS, LITELLM_URL, LITELLM_KEY, TRIAGE_URL

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

    async def _safe_log(self, node_id: str, message: str):
        """Log to Redis Stream with local fallback if Redis is down."""
        try:
            await self.bb.publish_log(node_id, message)
        except Exception:
            logger.warning(f"Redis log failed | {node_id}: {message}")

    async def _set_phase(self, phase: str, iteration: int = 0):
        """Update the orchestrator phase in Redis (read by Mission Control TopBar)."""
        try:
            await self.bb.redis.hset("bmas:public:state", mapping={
                "phase": phase,
                "iteration": str(iteration),
            })
        except Exception:
            pass

    async def _publish_task_state(self, task_id: str, label: str, status: str,
                                  sub_tasks: list[dict] | None = None):
        """Write task state in the format expected by the Mission Control frontend."""
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

    async def process_task(self, user_task: str) -> dict:
        """Main entry point: triage → plan → execute → audit → publish."""
        session_id = str(uuid.uuid4())[:8]
        task_id = f"task-{session_id}"

        # 1. Acquire global lock
        acquired, lock_id = await self.bb.acquire_lock(f"orchestrator:{task_id}")
        if not acquired:
            return {"error": "Could not acquire lock — another task is running"}

        try:
            await self._set_phase("triage", 1)
            await self._safe_log("daemon", f"Processing: {task_id}")

            # Publish initial task state so the UI can show it
            await self._publish_task_state(task_id, user_task[:80], "running", [
                {"id": f"{task_id}-triage",  "label": "Triage classification", "status": "running",  "agent": "planner",  "depends_on": []},
                {"id": f"{task_id}-plan",    "label": "Plan decomposition",    "status": "pending",  "agent": "planner",  "depends_on": [f"{task_id}-triage"]},
                {"id": f"{task_id}-exec",    "label": "Execute sub-tasks",     "status": "pending",  "agent": "executor", "depends_on": [f"{task_id}-plan"]},
                {"id": f"{task_id}-audit",   "label": "Audit & consensus",     "status": "pending",  "agent": "auditor",  "depends_on": [f"{task_id}-exec"]},
            ])

            # 2. Triage complexity (fail-fast: default to MEDIUM if triage is unreachable)
            try:
                triage = await self.triage.classify(user_task)
            except Exception as e:
                await self._safe_log("daemon",
                    f"WARN: Triage unavailable ({e}), defaulting to MEDIUM")
                triage = TriageResult(
                    complexity=Complexity.MEDIUM, litellm_model="medium"
                )
            await self._safe_log("daemon",
                f"Triage: {triage.complexity.value} → {triage.litellm_model}")

            # Update triage sub-task to completed
            await self._publish_task_state(task_id, user_task[:80], "running", [
                {"id": f"{task_id}-triage",  "label": f"Triage: {triage.complexity.value}", "status": "completed", "agent": "planner",  "depends_on": []},
                {"id": f"{task_id}-plan",    "label": "Plan decomposition",    "status": "pending",  "agent": "planner",  "depends_on": [f"{task_id}-triage"]},
                {"id": f"{task_id}-exec",    "label": "Execute sub-tasks",     "status": "pending",  "agent": "executor", "depends_on": [f"{task_id}-plan"]},
                {"id": f"{task_id}-audit",   "label": "Audit & consensus",     "status": "pending",  "agent": "auditor",  "depends_on": [f"{task_id}-exec"]},
            ])

            # 3. For COMPLEX tasks, use dynamic expert personas
            if triage.complexity == Complexity.COMPLEX:
                return await self._complex_research_flow(
                    task_id, session_id, user_task
                )

            # 4. For SIMPLE/LIGHT/MEDIUM, use standard Planner→Executor→Auditor flow
            return await self._standard_flow(task_id, session_id, user_task, triage)

        finally:
            await self._set_phase("idle", 0)
            await self.bb.release_lock(f"orchestrator:{task_id}", lock_id)

    async def _standard_flow(
        self, task_id: str, session_id: str, user_task: str, triage
    ) -> dict:
        """Standard bMAS flow: Plan → Execute → Audit."""
        sub_tasks = [
            {"id": f"{task_id}-triage", "label": f"Triage: {triage.complexity.value}", "status": "completed", "agent": "planner",  "depends_on": []},
            {"id": f"{task_id}-plan",   "label": "Plan decomposition",    "status": "pending",   "agent": "planner",  "depends_on": [f"{task_id}-triage"]},
            {"id": f"{task_id}-exec",   "label": "Execute sub-tasks",     "status": "pending",   "agent": "executor", "depends_on": [f"{task_id}-plan"]},
            {"id": f"{task_id}-audit",  "label": "Audit & consensus",     "status": "pending",   "agent": "auditor",  "depends_on": [f"{task_id}-exec"]},
        ]

        # Step 1: Planner decomposes the task
        await self._set_phase("planning", 1)
        sub_tasks[1]["status"] = "running"
        await self._publish_task_state(task_id, user_task[:80], "running", sub_tasks)

        plan = await self._dispatch_agent(
            "planner", task_id, user_task, DEFAULT_PERSONAS["planner"]
        )
        await self.bb.post_debate(session_id, "planner", plan.get("result", ""))
        sub_tasks[1]["status"] = "completed"

        # Step 2: Executor handles sub-tasks
        await self._set_phase("executing", 2)
        sub_tasks[2]["status"] = "running"
        await self._publish_task_state(task_id, user_task[:80], "running", sub_tasks)

        exec_result = await self._dispatch_agent(
            "executor", task_id, plan.get("result", user_task),
            DEFAULT_PERSONAS["executor"]
        )
        await self.bb.post_debate(session_id, "executor", exec_result.get("result", ""))
        sub_tasks[2]["status"] = "completed"

        # Step 3: Auditor reviews, resolves, cleans
        await self._set_phase("auditing", 3)
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
        await self._set_phase("finalizing", 4)
        await self._publish_task_state(task_id, user_task[:80], "completed", sub_tasks)
        await self.bb.publish_result(task_id, {
            "consensus": audit.get("result", ""),
            "triage": triage.complexity.value,
        })
        await self.bb.clear_private(session_id)
        await self._safe_log("daemon", f"Completed: {task_id}")

        return {"task_id": task_id, "result": audit.get("result", "")}

    async def _complex_research_flow(
        self, task_id: str, session_id: str, user_task: str
    ) -> dict:
        """Dynamic expert persona flow for complex research tasks."""
        await self._safe_log("daemon", "Activating dynamic expert personas")

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
                    "model": "heavy",  # Gemini Pro — persona generation needs capable model
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
                f"WARN: Expert persona generation failed ({e}), using defaults")
            experts = default_experts

        # Inject expert personas and run parallel debate
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

        # Post all debate entries
        for expert, result in zip(experts, results):
            await self.bb.post_debate(
                session_id, expert["domain"], result.get("result", "")
            )

        # Revert to Auditor persona for synthesis
        debate = await self.bb.get_debate(session_id)
        synthesis = await self._dispatch_agent(
            "auditor", task_id,
            f"Synthesize these expert perspectives into a unified analysis:\n\n{json.dumps(debate, indent=2)}",
            DEFAULT_PERSONAS["auditor"]
        )

        await self.bb.publish_result(task_id, {
            "consensus": synthesis.get("result", ""),
            "experts_used": [e["domain"] for e in experts],
            "triage": "complex",
        })
        await self.bb.clear_private(session_id)

        return {"task_id": task_id, "result": synthesis.get("result", "")}

    async def _dispatch_agent(
        self, role: str, task_id: str, description: str, persona: str,
        context: dict | None = None,
    ) -> dict:
        """Send a task to a Hermes Agent node via its REST API.

        Payload matches the TaskRequest schema from Phase 2 §2.2 Step 5
        (api_server.py): task_id, description, role_prompt, context, timeout.

        Retries up to 3 times with exponential backoff to handle Hermes
        cold-start delays (Gotcha #2 — first call after restart takes 5-15s).
        """
        url = AGENT_ENDPOINTS[role]
        payload = {
            "task_id": task_id,
            "description": description,
            "role_prompt": persona,
        }
        if context:
            payload["context"] = context

        for attempt in range(3):
            try:
                response = await self.http.post(
                    f"{url}/execute", json=payload,
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                if attempt < 2:
                    delay = 2 ** attempt  # 1s, 2s
                    await self._safe_log(role,
                        f"Retry {attempt + 1} of 2 after {delay}s: {e}")
                    await asyncio.sleep(delay)
                    continue
                await self._safe_log(role, f"ERROR after 3 attempts: {e}")
                return {"task_id": task_id, "status": "failed", "result": str(e)}

    async def close(self):
        await self.bb.close()
        await self.triage.close()
        await self.http.aclose()
