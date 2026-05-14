# Redis — Blackboard State Store

The shared state backbone for Stigmergic. Redis serves as the "blackboard" in the Blackboard Multi-Agent System architecture — the central knowledge store through which all agents coordinate without direct communication.

> Runs as Docker container `bmas-redis` on the HP OMEN at `192.168.4.240:6379`.

## Why Redis as the Blackboard?

| Alternative | Why Not |
|:---|:---|
| Filesystem | No atomic operations, no pub/sub, no distributed locks |
| PostgreSQL | Overkill for ephemeral swarm state; adds latency |
| etcd | Designed for config, not streaming data |
| **Redis** | ✅ Atomic ops (Redlock), Streams for durable logs, sub-ms latency, namespace separation |

## Namespace Schema

All keys use the `bmas:` prefix with a hierarchical namespace structure:

| Namespace | Key Pattern | Data Type | Purpose |
|:---|:---|:---|:---|
| **Public State** | `bmas:public:state` | Hash | Orchestrator phase, iteration count, pause flag |
| **Public Tasks** | `bmas:public:tasks` | Hash | Task registry — maps task IDs to JSON task objects |
| **Public Results** | `bmas:public:results` | Hash | Consensus results — written only by the Auditor |
| **Private Debate** | `bmas:private:{session}:debate` | List | Per-session agent debate entries (wiped after consensus) |
| **Locks** | `bmas:locks:{resource}` | String | Redlock distributed locks with SET NX PX |
| **Logs** | `bmas:logs:{node_id}` | Stream | Durable agent log streams (capped at 1000 entries) |
| **Metrics** | `bmas:metrics:cost` | Hash | Per-model USD cost counters |
| **Metrics** | `bmas:metrics:tokens` | Hash | Per-model token counters |
| **HITL Hints** | `bmas:public:hints:{task_id}` | List | Operator hints injected during pause |

## Files

| File | Purpose |
|:---|:---|
| `docker-compose.yml` | Container definition with health check (`redis-cli ping`), resource limits, and volume mounts |
| `redis.conf` | Redis server configuration — memory limits, persistence, security, logging |
| `data/` | Persistent data directory (RDB snapshots, logs) — **not committed to git** |

## Configuration Highlights

| Setting | Value | Rationale |
|:---|:---|:---|
| `maxmemory` | 1 GB | Sufficient for swarm state; prevents OOM on control plane |
| `maxmemory-policy` | `volatile-lru` | Evicts least-recently-used keys with TTL first |
| `save 60 100` | RDB snapshot every 60s if ≥100 keys changed | Balances durability vs. disk I/O |
| `requirepass` | Set | Prevents unauthorized access from other LAN devices |
| `bind 0.0.0.0` | All interfaces | Required for agent LXCs and Mission Control to connect |

## Deployment

```bash
# Start the container
docker compose up -d

# Check health
docker exec bmas-redis redis-cli -a bmas-redis-secret-2026 ping

# Monitor commands in real-time
docker exec bmas-redis redis-cli -a bmas-redis-secret-2026 MONITOR

# Inspect blackboard state
docker exec bmas-redis redis-cli -a bmas-redis-secret-2026 HGETALL bmas:public:state
docker exec bmas-redis redis-cli -a bmas-redis-secret-2026 HGETALL bmas:public:tasks

# View log streams
docker exec bmas-redis redis-cli -a bmas-redis-secret-2026 XRANGE bmas:logs:daemon - + COUNT 10

# Check memory usage
docker exec bmas-redis redis-cli -a bmas-redis-secret-2026 INFO memory
```

## Resource Limits

- **Memory**: 2 GB (container limit; Redis `maxmemory` is 1 GB)
- **CPUs**: 2 cores
- **Network**: Bound to `192.168.4.240:6379` (LAN only)
- **Persistence**: RDB snapshots to `/data/bmas-blackboard.rdb`
