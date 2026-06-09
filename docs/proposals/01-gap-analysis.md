[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [➡️ Next: Peer Review](02-peer-review.md)

# 01 — Gap Analysis: Why This Is Orchestrator-Worker, Not a Blackboard

> [!NOTE]
> This document is deliberately evidence-first. Every claim cites the file and lines that prove it, so the team can verify the diagnosis independently before agreeing to the cure.

---

## 1. The defining test

A blackboard system has one defining property: **knowledge sources (agents) observe a shared workspace and decide for themselves when and how to contribute.** The control component schedules *opportunistically* based on what is on the board — it does not encode the solution path.

bMAS fails this test in three independent ways. Each is sufficient on its own to disqualify the "blackboard" label.

| Property of a true blackboard | bMAS today | Verdict |
|:--|:--|:--|
| Agents *read* shared state to decide what to do | Agents receive a prompt string and return a string; they never touch Redis | ❌ |
| Control is opportunistic / data-driven | Control is a hardcoded `plan → execute → audit` DAG | ❌ |
| Contributions are concurrent and incremental | Per-task orchestration is sequential; complex tasks have only one fixed expert fan-out | ❌ |
| Shared state is the coordination medium | Redis is daemon-side plumbing (locks, abort flags, one debate read-back into the auditor prompt) plus a UI event bus; no *agent* ever reads or writes it | ❌ |

---

## 2. Evidence: the control component encodes the solution

The orchestrator hardcodes the entire problem-solving strategy. The "flow" is a fixed sequence of three dispatches, regardless of what any agent actually says:

```237:312:daemon/src/core/orchestrator.py
    async def _standard_flow(
        self, task_id: str, session_id: str, user_task: str, triage
    ) -> dict:
        """Standard bMAS flow: Plan → Execute → Audit."""
        ...
        plan = await self._dispatch_agent(
            "planner", task_id, user_task, DEFAULT_PERSONAS["planner"]
        )
        ...
        exec_result = await self._dispatch_agent(
            "executor", task_id, plan.get("result", user_task),
            DEFAULT_PERSONAS["executor"]
        )
        ...
        audit = await self._dispatch_agent(
            "auditor", task_id,
            f"Review this debate and produce consensus:\n\n{audit_context}",
            DEFAULT_PERSONAS["auditor"]
        )
```

There is no point at which the system reads the board and *decides* what should happen next. The planner's output does not change *which* agent runs next or *how many* rounds occur — it is merely concatenated into the next prompt. This is a [pipeline](https://en.wikipedia.org/wiki/Pipeline_(software)), the architectural opposite of opportunistic control.

The roadmap already names this exact problem — "A DAG cannot loop by definition. This conflicts with the blackboard pattern" — in [control-unit.md](../roadmap/control-unit.md). This proposal is the implementation of that intent.

## 3. Evidence: agents are stateless text functions, blind to the board

The daemon's only channel to an agent is a single blocking HTTP POST that sends a prompt and waits for a string:

```476:531:daemon/src/core/orchestrator.py
    async def _dispatch_agent(
        self, role: str, task_id: str, description: str, persona: str,
        context: dict | None = None,
    ) -> dict:
        ...
        payload = {
            "task_id": task_id,
            "description": description,
            "role_prompt": persona,
        }
        ...
        response = await self.http.post(f"{url}/execute", json=payload)
        response.raise_for_status()
        response_data = response.json()
```

On the edge, the agent runs Hermes as a **one-shot subprocess** and returns only the final stdout. Everything the agent thought, searched, or called is thrown away:

```195:206:agent/api_server.py
        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            ...
            return TaskStatus.failed, errors or f"Exit code {proc.returncode}"

        logger.info(f"[{request_id}] Task completed | output_len={len(output)}")
        return TaskStatus.completed, output
```

The `context` field *can* carry a blackboard snapshot (it is appended to `AGENTS.md` in `_run_hermes`), but the orchestrator **never populates it** in the standard or complex flows. So even the one read-path that exists is unused. Agents cannot see each other's work; they only see what the daemon hand-feeds into a prompt.

## 4. Evidence: the "debate" is sequential string concatenation

What the architecture calls a "debate" is a list that each agent appends to once, in turn, which is then JSON-dumped into the auditor's prompt:

```299:311:daemon/src/core/orchestrator.py
        debate = await self.bb.get_debate(session_id)
        audit_context = json.dumps(debate, indent=2)
        audit = await self._dispatch_agent(
            "auditor", task_id,
            f"Review this debate and produce consensus:\n\n{audit_context}",
            DEFAULT_PERSONAS["auditor"]
        )
```

`post_debate` is a plain `rpush`; `get_debate` is an `lrange` — there is no addressing, no reply-to, no cross-examination, no disagreement detection:

```98:111:daemon/src/core/blackboard.py
    async def post_debate(self, session_id: str, agent_role: str, content: str):
        """Post a debate entry to the private debate space."""
        entry = json.dumps({
            "role": agent_role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self.redis.rpush(f"bmas:private:{session_id}:debate", entry)
```

No agent ever *reacts* to another agent's entry. There is exactly one writer per phase and exactly one reader at the end. That is a relay race, not a debate.

## 5. Evidence: Redis is daemon plumbing, not a blackboard

To be precise about who touches Redis today (precision matters — a reviewer will check): the **orchestrator** reads it (locks, `get_debate` for the auditor prompt, the `abort` flag), and the dashboard reads it only at the edges (the `/state` route and the HITL route; live data reaches the UI via SSE and history via SQLite). The party that *never* touches Redis is **an agent** — there is no Redis client anywhere in `agent/`. So Redis functions as the daemon's private bookkeeping plus an event bus, not as a workspace any knowledge source observes. Two details sharpen the point. First, `Blackboard.get_state()`'s `agents` field is **hardcoded to dead values** — the board has no live notion of who is doing what:

```86:96:daemon/src/core/blackboard.py
        return {
            "phase": state_meta.get("phase", "idle"),
            "iteration": int(state_meta.get("iteration", "0")),
            "paused": state_meta.get("pause", "false") == "true",
            "tasks": tasks,
            "results": {k: json.loads(v) for k, v in results.items()},
            "agents": {
                role: {"alive": False, "last_heartbeat": "", "current_task": None}
                for role in AGENT_ENDPOINTS
            },
        }
```

Second, the blackboard namespaces (`bmas:public:*`, `bmas:private:*`) exist, but the private debate list is consumed exactly once — serialized into the auditor's prompt — and then torn down (`clear_private`) before any other agent could act on it. (Related dead wiring, confirmed against the code: the dashboard's pause/hint HITL writes `bmas:public:state.pause` and `bmas:public:hints:{task}`, but the orchestrator never reads either — only `abort` is consumed. And `track_cost`'s `bmas:metrics:*` keys are never written because the method is never called.)

## 6. Evidence: strictly per-task, single-writer concurrency

Each task takes a lock scoped to `orchestrator:{task_id}` and the standard pipeline is sequential. This is not a true process-wide global lock across all task IDs; the important blackboard gap is narrower and more damaging: **within one task**, only the daemon writes meaningful coordination state, and agents do not opportunistically interleave contributions. The architecture doc concedes the resulting limitation directly ("**Single task at a time**", §9.2):

```132:135:daemon/src/core/orchestrator.py
        # 1. Acquire global lock
        acquired, lock_id = await self.bb.acquire_lock(f"orchestrator:{task_id}")
        if not acquired:
            return {"error": "Could not acquire lock — another task is running"}
```

A blackboard's entire reason for existing is **opportunistic parallelism** — multiple knowledge sources contributing concurrently to the same evolving problem state. The current standard flow precludes that by construction; the complex flow has a single parallel expert burst, but those experts still return one-shot strings that are only synthesized afterward.

## 7. The silent observability failure (root cause for the UI work)

This is the finding that most directly blocks the user's visualization goals, and it is easy to miss.

1. **No agent-level traces exist.** The only logs written during a task are daemon-level breadcrumbs — `"Processing: {task_id}"`, `"Completed: {task_id}"` (see `_safe_log` calls in `orchestrator.py`). The agent's reasoning, tool calls, and token stream never reach the daemon, because `hermes -z` only emits final stdout (§3 above).
2. **The Logs tab is therefore nearly empty of real content.** `TaskLogTerminal` faithfully renders whatever `logs[]` contains — but `logs[]` contains a handful of orchestrator status lines, not agent activity.
3. **Cost tracking is effectively dead.** `_dispatch_agent` looks for `usage` in the agent response:

```505:518:daemon/src/core/orchestrator.py
                # Opportunistic cost capture — Hermes agents may include usage metadata
                usage = response_data.get("usage")
                if usage:
                    try:
                        await db.insert_cost_entry(...)
```

   …but `api_server.py`'s `TaskResponse` schema has **no `usage` field** (see `agent/api_server.py` lines 76–84). So `usage` is always absent, no `cost_entry` is ever inserted from the agent path, and the Cost tab shows `$0.00` for token-bearing work.

> [!IMPORTANT]
> **You cannot visualize data you are not collecting.** Document [06 — Agent Traces](06-agent-traces.md) is therefore a hard prerequisite for the UI documents [08](08-ui-blackboard-visualization.md) and [09](09-ui-agent-trace-inspector.md). Fixing the trace pipeline is sequenced *first* in the [migration plan](10-migration-and-rollout.md).

## 8. What is genuinely good (and must be preserved)

The diagnosis is not "rewrite everything." The infrastructure is strong and the inversion can reuse most of it:

- **Dual-write persistence** (`database.py` + Redis) is exactly the right substrate for an event-sourced board. We extend it, not replace it.
- **SSE plumbing** (`routes/events.py`, `useTaskStream.ts`, `TaskStreamContext`) already distributes typed events to the UI with a single connection per task. The new board/trace events ride the same rails.
- **The design system** (`DESIGN.md`, `ui/` primitives, `design-tokens.ts`) is mature and complete. New visualizations compose from it cleanly.
- **React Flow is already wired** (`DAGVisualizer.tsx`) — the live blackboard graph is an evolution of an existing component, not a greenfield dependency.
- **LiteLLM + triage** cost-routing is orthogonal to the blackboard inversion and stays as-is.

## 9. Summary table — the five gaps and where they are fixed

| # | Gap | Primary evidence | Fixed in |
|:--|:--|:--|:--|
| G1 | Control encodes the solution path (fixed DAG) | `orchestrator.py` `_standard_flow` | [05 Control Unit](05-control-unit.md) |
| G2 | Agents are blind, stateless text functions | `_dispatch_agent`, `api_server._run_hermes` | [03 Target Arch](03-target-architecture.md), [04 PatchBoard](04-blackboard-protocol.md) |
| G3 | "Debate" is concatenation, not interaction | `post_debate`/`get_debate` | [04 PatchBoard](04-blackboard-protocol.md), [05 Control Unit](05-control-unit.md) |
| G4 | Redis is daemon plumbing agents never touch; no per-entry concurrent mutation | `get_state`, task-scoped orchestrator lock, sequential standard flow | [04 PatchBoard](04-blackboard-protocol.md) (per-key optimistic locking) |
| G5 | No agent traces; dead cost path | `_run_hermes`, `TaskResponse` schema | [06 Agent Traces](06-agent-traces.md) |

➡️ Continue to [02 — Peer Review](02-peer-review.md).
