# Redis Runtime Service

Redis supplies live projections, locks, notifications, short-lived controls, and bounded streams.

SQLite remains the authoritative durable task store. Redis is not the only copy of a task.

## Runtime uses

| Use | Example key |
|:---|:---|
| Live task projection | `bmas:public:tasks` |
| Live board projection | `bmas:board:{task}:entries` |
| Task and system notifications | `bmas:events:{task}` and `bmas:events:system` |
| Resource locks | `bmas:locks:{resource}` |
| Operator pause and hints | `bmas:public:pause:{task}` and `bmas:public:hints:{task}` |
| Bounded logs and traces | `bmas:logs:*` and `bmas:traces:*` |
| Live cost counters | `bmas:metrics:*` |

The daemon writes durable events to SQLite before it publishes live notifications. It retries failed notifications through the event outbox.

## Files

| File | Purpose |
|:---|:---|
| `redis.conf.template` | Defines memory, persistence, network, and password settings. |
| `entrypoint.sh` | Inserts `REDIS_PASSWORD` into a temporary runtime configuration. |

The image stores Redis files in the `redis-data` named volume.

## Default settings

| Setting | Value |
|:---|:---|
| Image | `redis:8.10.0-alpine3.23` |
| Maximum Redis memory | `1gb` |
| Eviction policy | `volatile-lru` |
| Snapshot rule | After 100 changes in 60 seconds |
| Data file | `/data/bmas-blackboard.rdb` |
| Authentication | Required through `REDIS_PASSWORD` |

Redis listens on all container interfaces. Docker publishes the port on `127.0.0.1` by default.

Do not publish Redis to an untrusted network.

## Operations

```bash
docker compose up -d redis
docker compose logs --tail 100 redis
docker compose exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" ping'
docker compose exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" INFO memory'
```

Avoid the Redis `MONITOR` command on a busy deployment. It can add significant load and can reveal request data.

Use the daemon `/readiness` endpoint for the normal operator check.

## Recovery

If Redis loses its live projections, restart Redis and the daemon. The daemon keeps durable task state in SQLite.

```bash
docker compose restart redis daemon
./scripts/bmas doctor --wait 180
```

Read [Operations](../docs/OPERATIONS.md) before you delete the `redis-data` volume.
