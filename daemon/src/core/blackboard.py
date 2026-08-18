# /opt/bmas/daemon/src/core/blackboard.py
"""
Redis Blackboard client with atomic Redlock for race-condition prevention.
Uses single-instance Redis lock (sufficient for homelab; upgrade to
multi-instance Redlock via aioredlock for production HA).
"""

import hashlib
import json
import os
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

import redis.asyncio as aioredis

from config import AGENT_ENDPOINTS, LOCK_TTL_MS, NODE_URL_TO_NAME, REDIS_URL
from core.event_delivery import record_and_publish, task_stream
from core.log_levels import normalize_level


def _entry_seq(entry_id: str) -> int:
    """Parse the numeric sequence from a gateway-assigned 'e-<n>' id."""
    if entry_id and "-" in entry_id:
        tail = entry_id.rsplit("-", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return 0


# Internal alias used within this module (normalize_level imported above).
_normalize_level = normalize_level


def _projection_cache_capacity() -> int:
    """Return the bounded count of task projections retained in memory."""
    try:
        value = int(os.getenv("BMAS_BOARD_PROJECTION_CACHE_TASKS", "128"))
    except ValueError:
        return 128
    return min(max(value, 1), 10_000)


class Blackboard:
    def __init__(self) -> None:
        self.redis: aioredis.Redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        self._projection_digests: dict[str, dict[str, str]] = {}
        self._projection_meta: dict[str, dict[str, str]] = {}
        self._projection_revision: OrderedDict[str, str] = OrderedDict()
        self._projection_cache_capacity = _projection_cache_capacity()

    def _touch_projection_cache(self, task_id: str) -> None:
        """Mark one task projection recent and evict the oldest task."""
        self._projection_revision.move_to_end(task_id)
        while len(self._projection_revision) > self._projection_cache_capacity:
            evicted_task, _ = self._projection_revision.popitem(last=False)
            self._projection_digests.pop(evicted_task, None)
            self._projection_meta.pop(evicted_task, None)

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
        result = await self.redis.eval(lua, 1, key, lock_id)  # type: ignore[misc]
        return bool(result)

    async def renew_lock(
        self, resource: str, lock_id: str, ttl_ms: int = LOCK_TTL_MS,
    ) -> bool:
        """Extend a lock only while this caller still owns it."""
        key = f"bmas:locks:{resource}"
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("pexpire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = await self.redis.eval(  # type: ignore[misc]
            lua, 1, key, lock_id, ttl_ms,
        )
        return bool(result)

    async def owns_lock(self, resource: str, lock_id: str) -> bool:
        """Return true only while the supplied owner holds the lock."""
        key = f"bmas:locks:{resource}"
        return await self.redis.get(key) == lock_id

    # ── Public Namespace ─────────────────────────────────────
    async def publish_task(self, task_id: str, task_data: dict):
        """Write a task to the public task queue."""
        await self.redis.hset(  # type: ignore[misc]
            "bmas:public:tasks", task_id,
            json.dumps({**task_data, "created_at": datetime.now(UTC).isoformat()})
        )

    async def publish_result(self, task_id: str, result: dict):
        """Write a consensus result to the public results store."""
        await self.redis.hset(  # type: ignore[misc]
            "bmas:public:results", task_id,
            json.dumps({**result, "finalized_at": datetime.now(UTC).isoformat()})
        )

    async def get_state(self) -> dict:
        """Get the full public state snapshot.

        Returns the shape expected by the Mission Control frontend:
        { phase, iteration, paused, tasks: { id: Task }, agents: { role: AgentStatus } }
        """
        # Orchestrator metadata from bmas:public:state hash
        state_meta = await self.redis.hgetall("bmas:public:state")  # type: ignore[misc]

        tasks_raw = await self.redis.hgetall("bmas:public:tasks")  # type: ignore[misc]
        results = await self.redis.hgetall("bmas:public:results")  # type: ignore[misc]

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

    # ── Durable Board Snapshot (board v2 persistence) ──────────────
    #
    # The classic runtime keeps its active board in an in-process store.
    # SQLite stores the authoritative board. Redis stores a persistent
    # materialized projection for low-latency reads.
    #
    # Keys follow the v2 layout (core/protocol.py):
    #   bmas:board:{task}:entries  — Hash: entry_id → entry JSON
    #   bmas:board:{task}:meta     — Hash: phase, round, variant, …
    #
    # These keys are intentionally persistent (no TTL).  Task cleanup
    # deletes them explicitly via task_key_patterns(), never via expiry.

    @staticmethod
    def _board_entries_key(task_id: str) -> str:
        return f"bmas:board:{task_id}:entries"

    @staticmethod
    def _board_meta_key(task_id: str) -> str:
        return f"bmas:board:{task_id}:meta"

    @staticmethod
    def _board_digests_key(task_id: str) -> str:
        return f"bmas:board:{task_id}:digests"

    @staticmethod
    def _board_projection_key(task_id: str) -> str:
        return f"bmas:board:{task_id}:projection"

    async def save_board_snapshot(
        self,
        task_id: str,
        entries: dict[str, dict],
        meta: dict | None = None,
    ) -> None:
        """Persist the full board snapshot to Redis (durable, no TTL).

        ``entries`` maps entry_id → JSON-safe entry dict.  The whole
        snapshot is rewritten on every commit so removed/superseded
        statuses stay accurate (the snapshot always carries them).
        """
        entries_key = self._board_entries_key(task_id)
        meta_key = self._board_meta_key(task_id)
        digests_key = self._board_digests_key(task_id)
        projection_key = self._board_projection_key(task_id)
        serialized_entries = {
            entry_id: json.dumps(entry, separators=(",", ":"), sort_keys=True)
            for entry_id, entry in entries.items()
        }
        entry_digests = {
            entry_id: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for entry_id, value in serialized_entries.items()
        }
        serialized_meta = {
            key: value if isinstance(value, str) else json.dumps(
                value,
                separators=(",", ":"),
                sort_keys=True,
            )
            for key, value in (meta or {}).items()
        }
        projection_value = json.dumps(
            {"entries": entry_digests, "meta": serialized_meta},
            separators=(",", ":"),
            sort_keys=True,
        )
        revision = hashlib.sha256(projection_value.encode("utf-8")).hexdigest()

        if task_id not in self._projection_digests:
            read_pipe = self.redis.pipeline()
            read_pipe.hgetall(digests_key)
            read_pipe.hkeys(entries_key)
            read_pipe.hgetall(meta_key)
            read_pipe.hget(projection_key, "revision")
            stored_digests, stored_entry_ids, stored_meta, stored_revision = (
                await read_pipe.execute()
            )
            self._projection_digests[task_id] = dict(stored_digests)
            self._projection_meta[task_id] = dict(stored_meta)
            self._projection_revision[task_id] = str(stored_revision or "")
            if not stored_digests and stored_entry_ids:
                self._projection_digests[task_id] = {
                    str(entry_id): "" for entry_id in stored_entry_ids
                }

        if self._projection_revision[task_id] == revision:
            self._touch_projection_cache(task_id)
            return

        previous_digests = self._projection_digests[task_id]
        previous_meta = self._projection_meta[task_id]
        changed_entries = {
            entry_id: serialized_entries[entry_id]
            for entry_id, digest in entry_digests.items()
            if previous_digests.get(entry_id) != digest
        }
        deleted_entries = sorted(set(previous_digests) - set(entry_digests))
        changed_meta = {
            key: value
            for key, value in serialized_meta.items()
            if previous_meta.get(key) != value
        }
        deleted_meta = sorted(set(previous_meta) - set(serialized_meta))

        write_pipe = self.redis.pipeline()
        if changed_entries:
            write_pipe.hset(entries_key, mapping=changed_entries)  # type: ignore[misc,arg-type]
            write_pipe.hset(  # type: ignore[misc,arg-type]
                digests_key,
                mapping={entry_id: entry_digests[entry_id] for entry_id in changed_entries},
            )
        if deleted_entries:
            write_pipe.hdel(entries_key, *deleted_entries)
            write_pipe.hdel(digests_key, *deleted_entries)
        if changed_meta:
            write_pipe.hset(meta_key, mapping=changed_meta)  # type: ignore[misc,arg-type]
        if deleted_meta:
            write_pipe.hdel(meta_key, *deleted_meta)
        write_pipe.hset(projection_key, mapping={"revision": revision})  # type: ignore[misc]
        for key in (entries_key, meta_key, digests_key, projection_key):
            write_pipe.persist(key)
        await write_pipe.execute()

        self._projection_digests[task_id] = entry_digests
        self._projection_meta[task_id] = serialized_meta
        self._projection_revision[task_id] = revision
        self._touch_projection_cache(task_id)

    async def get_board_snapshot(self, task_id: str) -> dict:
        """Read the durable board snapshot from Redis.

        Returns {entries: [...], meta: {...}} with entries ordered by
        their sequence (parsed from the gateway-assigned ``e-<seq>`` id).
        Returns empty lists/dicts when no board exists yet.
        """
        entries_key = self._board_entries_key(task_id)
        meta_key = self._board_meta_key(task_id)

        raw_entries = await self.redis.hgetall(entries_key)  # type: ignore[misc]
        entries: list[dict] = []
        for v in raw_entries.values():
            try:
                entry = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                continue
            # Derive a numeric seq from the "e-<n>" id for stable ordering.
            entry.setdefault("seq", _entry_seq(entry.get("id", "")))
            entries.append(entry)
        entries.sort(key=lambda e: (e.get("seq", 0), str(e.get("id", ""))))

        raw_meta = await self.redis.hgetall(meta_key)  # type: ignore[misc]
        meta: dict = {}
        for k, v in raw_meta.items():
            try:
                meta[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                meta[k] = v

        return {"entries": entries, "meta": meta}

    # ── Private Namespace ──────────────────────────────────────
    async def get_debate(self, session_id: str) -> list[dict]:
        """Read all debate entries for a session."""
        raw = await self.redis.lrange(f"bmas:private:{session_id}:debate", 0, -1)  # type: ignore[misc]
        return [json.loads(r) for r in raw]

    async def clear_private(self, session_id: str):
        """Auditor cleanup: wipe private debate space to prevent context bloat.
        Uses SCAN instead of KEYS to avoid blocking the Redis event loop."""
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(  # type: ignore[misc]
                cursor, match=f"bmas:private:{session_id}:*", count=100
            )
            if keys:
                await self.redis.delete(*keys)  # type: ignore[misc]
            if cursor == 0:
                break

    # ── SSE Pub/Sub Events ───────────────────────────────────
    async def publish_event(
        self,
        task_id: str,
        event: str,
        data: dict,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """Save a task event, then send a low-latency Redis notification."""
        await record_and_publish(
            self.redis,
            task_stream(task_id),
            event,
            data,
            task_id=task_id,
            idempotency_key=idempotency_key,
        )

    async def publish_system_event(self, event: str, data: dict):
        """Save a system event, then send a low-latency Redis notification."""
        if event in {"daemon-status", "agent-health"}:
            await self.redis.publish(
                "bmas:events:system",
                json.dumps({"event": event, "data": data}),
            )
            return
        await record_and_publish(self.redis, "system", event, data)

    # ── Logging (Streams) ────────────────────────────────────
    async def publish_log(
        self,
        node_id: str,
        message: str,
        task_id: str | None = None,
        level: str = "info",
        fields: dict | None = None,
        node: str | None = None,
        turn_id: str | None = None,
    ):
        """Push a structured log entry to global stream, task stream, and Pub/Sub.

        `level` is a canonical level (info|warning|error|debug). `fields` is an
        arbitrary structured payload (agent reasoning, tool calls, routing
        rationale, usage/cost, board reads/writes, stack traces, …) carried
        end-to-end so the UI can render a lossless detail view. The full
        message and payload are transported verbatim — never truncated here.
        """
        ts = datetime.now(UTC).isoformat()
        level = _normalize_level(level)
        fields_json = json.dumps(fields) if fields else ""
        # Resolve raw HTTP endpoint URLs to the friendly node name from
        # bmas.yaml so the `node` field is always a human-readable identifier
        # (e.g. "node-1") regardless of which code path produced the log entry.
        if node and node.startswith("http"):
            node = NODE_URL_TO_NAME.get(node, node)
        # Redis Stream fields must be flat string/number values.
        stream_fields = {
            "node_id": node_id,
            "msg": message,
            "level": level,
            "ts": ts,
        }
        if node:
            stream_fields["node"] = node
        if turn_id:
            stream_fields["turn_id"] = turn_id
        if fields_json:
            stream_fields["fields"] = fields_json

        if task_id:
            log_event = {
                "agent_role": node_id,
                "level": level,
                "message": message,
                "fields": fields or None,
                "node": node,
                "turn_id": turn_id,
                "ts": ts,
            }
            await record_and_publish(
                self.redis,
                task_stream(task_id),
                "log",
                log_event,
                task_id=task_id,
            )

        # 1. Global stream (existing behavior — /api/logs global view)
        await self.redis.xadd(  # type: ignore[misc]
            f"bmas:logs:{node_id}",
            stream_fields,  # type: ignore[arg-type]
            maxlen=1000,
            approximate=True
        )

        if task_id:
            # 2. Task-scoped stream (archival to SQLite on completion)
            await self.redis.xadd(  # type: ignore[misc]
                f"bmas:logs:task:{task_id}", {**stream_fields, "task_id": task_id},  # type: ignore[dict-item]
            )
            await self.redis.expire(f"bmas:logs:task:{task_id}", 86400)  # type: ignore[misc]

    # ── Metrics ──────────────────────────────────────────────
    async def track_cost(self, model: str, tokens: int, cost_usd: float):
        """Increment cost tracking counters."""
        await self.redis.hincrbyfloat("bmas:metrics:cost", model, cost_usd)  # type: ignore[misc]
        await self.redis.hincrby("bmas:metrics:tokens", model, tokens)  # type: ignore[misc]

    # ── HITL (Human-in-the-Loop) ─────────────────────────────
    async def set_pause(self, paused: bool = True):
        """Set or clear the swarm pause flag (used by Mission Control UI)."""
        if paused:
            await self.redis.hset("bmas:public:state", "pause", "true")  # type: ignore[misc]
        else:
            await self.redis.hdel("bmas:public:state", "pause")  # type: ignore[misc]

    async def is_paused(self) -> bool:
        """Check if the swarm is paused by the operator."""
        val = await self.redis.hget("bmas:public:state", "pause")  # type: ignore[misc]
        return val == "true"

    async def push_hint(self, task_id: str, hint: str) -> None:
        """Push an operator hint for a specific task (read on resume).

        Uses RPUSH so hints are processed in FIFO order — consistent with
        the inject_directive HITL endpoint which also uses RPUSH.
        """
        await self.redis.rpush(f"bmas:public:hints:{task_id}", hint)  # type: ignore[misc]

    async def pop_hints(self, task_id: str) -> list[str]:
        """Pop all pending hints for a task (destructive read)."""
        hints = await self.redis.lrange(f"bmas:public:hints:{task_id}", 0, -1)  # type: ignore[misc]
        if hints:
            await self.redis.delete(f"bmas:public:hints:{task_id}")  # type: ignore[misc]
        return hints  # type: ignore[return-value]  # decode_responses=True → always str

    async def close(self):
        await self.redis.aclose()
