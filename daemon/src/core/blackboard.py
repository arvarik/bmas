# /opt/bmas/daemon/src/core/blackboard.py
"""
Redis Blackboard client with atomic Redlock for race-condition prevention.
Uses single-instance Redis lock (sufficient for homelab; upgrade to
multi-instance Redlock via aioredlock for production HA).
"""

import uuid
import json
from datetime import datetime, timezone
import redis.asyncio as aioredis
from config import REDIS_URL, LOCK_TTL_MS, AGENT_ENDPOINTS


class Blackboard:
    def __init__(self) -> None:
        self.redis: aioredis.Redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # ── Lock Management ──────────────────────────────────────────────
    async def acquire_lock(self, resource: str, ttl_ms: int = LOCK_TTL_MS) -> tuple[bool, str]:
        """Acquire a distributed lock using SET NX PX (single-instance Redlock).
        Returns (acquired: bool, lock_id: str). The lock_id must be passed to release_lock()."""
        lock_id = str(uuid.uuid4())
        key = f"bmas:locks:{resource}"
        acquired = await self.redis.set(key, lock_id, nx=True, px=ttl_ms)
        return bool(acquired), lock_id

    async def release_lock(self, resource: str, lock_id: str) -> bool:
        """Release lock only if we own it (atomic Lua script)."""
        key = f"bmas:locks:{resource}"
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = await self.redis.eval(lua, 1, key, lock_id)
        return bool(result)

    # ── Public Namespace ─────────────────────────────────────
    async def publish_task(self, task_id: str, task_data: dict):
        """Write a task to the public task queue."""
        await self.redis.hset(
            "bmas:public:tasks", task_id,
            json.dumps({**task_data, "created_at": datetime.now(timezone.utc).isoformat()})
        )

    async def publish_result(self, task_id: str, result: dict):
        """Write a consensus result to the public results store."""
        await self.redis.hset(
            "bmas:public:results", task_id,
            json.dumps({**result, "finalized_at": datetime.now(timezone.utc).isoformat()})
        )

    async def get_state(self) -> dict:
        """Get the full public state snapshot.

        Returns the shape expected by the Mission Control frontend:
        { phase, iteration, paused, tasks: { id: Task }, agents: { role: AgentStatus } }
        """
        # Orchestrator metadata from bmas:public:state hash
        state_meta = await self.redis.hgetall("bmas:public:state")

        tasks_raw = await self.redis.hgetall("bmas:public:tasks")
        results = await self.redis.hgetall("bmas:public:results")

        # Parse tasks and merge in result data
        tasks = {}
        for k, v in tasks_raw.items():
            try:
                task = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                task = {"raw": v}
            # Ensure required frontend fields exist
            task.setdefault("id", k)
            task.setdefault("label", task.get("description", k))
            task.setdefault("status", "pending")
            task.setdefault("sub_tasks", [])
            task.setdefault("created_at", task.get("created_at", ""))
            task.setdefault("updated_at", task.get("updated_at", task.get("created_at", "")))
            tasks[k] = task

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

    # ── Private Namespace (Debate) ───────────────────────────
    async def post_debate(self, session_id: str, agent_role: str, content: str):
        """Post a debate entry to the private debate space."""
        entry = json.dumps({
            "role": agent_role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self.redis.rpush(f"bmas:private:{session_id}:debate", entry)

    async def get_debate(self, session_id: str) -> list[dict]:
        """Read all debate entries for a session."""
        raw = await self.redis.lrange(f"bmas:private:{session_id}:debate", 0, -1)
        return [json.loads(r) for r in raw]

    async def clear_private(self, session_id: str):
        """Auditor cleanup: wipe private debate space to prevent context bloat.
        Uses SCAN instead of KEYS to avoid blocking the Redis event loop."""
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(
                cursor, match=f"bmas:private:{session_id}:*", count=100
            )
            if keys:
                await self.redis.delete(*keys)
            if cursor == 0:
                break

    # ── SSE Pub/Sub Events ───────────────────────────────────
    async def publish_event(self, task_id: str, event: str, data: dict):
        """Publish a typed event to the task's Pub/Sub channel.
        
        Consumed by the SSE endpoint /events/{task_id}.
        """
        await self.redis.publish(
            f"bmas:events:{task_id}",
            json.dumps({"event": event, "data": data})
        )

    async def publish_system_event(self, event: str, data: dict):
        """Publish to the system Pub/Sub channel (consumed by /events/system)."""
        await self.redis.publish(
            "bmas:events:system",
            json.dumps({"event": event, "data": data})
        )

    # ── Logging (Streams) ────────────────────────────────────
    async def publish_log(self, node_id: str, message: str, task_id: str | None = None):
        """Push a log entry to global stream, task stream, and Pub/Sub."""
        ts = datetime.now(timezone.utc).isoformat()
        fields = {"node": node_id, "msg": message, "ts": ts}
        
        # 1. Global stream (existing behavior — /api/logs global view)
        await self.redis.xadd(
            f"bmas:logs:{node_id}",
            fields,
            maxlen=1000,
            approximate=True
        )
        
        if task_id:
            # 2. Task-scoped stream (archival to SQLite on completion)
            await self.redis.xadd(
                f"bmas:logs:task:{task_id}", {**fields, "task_id": task_id}
            )
            await self.redis.expire(f"bmas:logs:task:{task_id}", 86400)
            
            # 3. Pub/Sub (live SSE delivery)
            await self.redis.publish(
                f"bmas:events:{task_id}",
                json.dumps({"event": "log", "data": {
                    "agent_role": node_id,
                    "level": "info",
                    "message": message,
                    "ts": ts,
                }})
            )

    # ── Metrics ──────────────────────────────────────────────
    async def track_cost(self, model: str, tokens: int, cost_usd: float):
        """Increment cost tracking counters."""
        await self.redis.hincrbyfloat("bmas:metrics:cost", model, cost_usd)
        await self.redis.hincrby("bmas:metrics:tokens", model, tokens)

    # ── HITL (Human-in-the-Loop) ─────────────────────────────
    async def set_pause(self, paused: bool = True):
        """Set or clear the swarm pause flag (used by Mission Control UI)."""
        if paused:
            await self.redis.hset("bmas:public:state", "pause", "true")
        else:
            await self.redis.hdel("bmas:public:state", "pause")

    async def is_paused(self) -> bool:
        """Check if the swarm is paused by the operator."""
        val = await self.redis.hget("bmas:public:state", "pause")
        return val == "true"

    async def push_hint(self, task_id: str, hint: str) -> None:
        """Push an operator hint for a specific task (read on resume).

        Uses RPUSH so hints are processed in FIFO order — consistent with
        the inject_directive HITL endpoint which also uses RPUSH.
        """
        await self.redis.rpush(f"bmas:public:hints:{task_id}", hint)

    async def pop_hints(self, task_id: str) -> list[str]:
        """Pop all pending hints for a task (destructive read)."""
        hints = await self.redis.lrange(f"bmas:public:hints:{task_id}", 0, -1)
        if hints:
            await self.redis.delete(f"bmas:public:hints:{task_id}")
        return hints

    async def close(self):
        await self.redis.aclose()
