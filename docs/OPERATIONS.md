# Operations

Use this guide after the classic stack has a valid `bmas.yaml` and `.env`.

## Daily checks

Run one command before you investigate a service.

```bash
./scripts/bmas doctor
```

The command checks local files and live readiness. Add a wait period during startup.

```bash
./scripts/bmas doctor --wait 180
```

Run a smoke task after an upgrade, a provider change, or a runtime-limit change.

```bash
./scripts/bmas smoke
```

## Health endpoints

The daemon provides two different endpoints.

| Endpoint | Meaning |
|:---|:---|
| `GET /health` | Reports daemon and dependency state for monitoring. |
| `GET /readiness` | Reports whether the complete classic stack can accept a task. |

Read readiness directly:

```bash
curl -fsS http://127.0.0.1:9000/readiness
```

The response checks Redis, SQLite, LiteLLM, execution agents, the classic runtime, and event delivery.

Mission Control uses the same response. It disables submission when one check fails.

## Container state

```bash
docker compose ps
```

The required services are `redis`, `litellm`, `agent`, `daemon`, and `dashboard`.

The optional `triage` service appears only when you start the `gpu` profile.

## Logs

Read all recent logs:

```bash
docker compose logs --tail 200
```

Follow the task path:

```bash
docker compose logs -f daemon agent litellm
```

Read one failed readiness service:

```bash
docker compose logs --tail 200 redis
docker compose logs --tail 200 litellm
docker compose logs --tail 200 agent
docker compose logs --tail 200 daemon
docker compose logs --tail 200 dashboard
```

Do not paste `.env` or authorization headers into an issue or shared chat.

## Restart a service

Restart one service when its configuration did not change.

```bash
docker compose restart daemon
```

Recreate the stack after a `bmas.yaml` or `.env` change.

```bash
docker compose up -d
./scripts/bmas doctor --wait 180
```

Rebuild the changed image after a source change.

```bash
docker compose up -d --build daemon agent dashboard litellm
./scripts/bmas doctor --wait 180
```

## Task inspection

List recent tasks:

```bash
curl -fsS http://127.0.0.1:9000/tasks
```

Read one task:

```bash
curl -fsS http://127.0.0.1:9000/tasks/TASK_ID
```

When `BMAS_API_KEY` protects a mutation request, send `Authorization: Bearer VALUE`. Read-only task routes do not require this key.

## Backup

Run this command while the required containers exist:

```bash
./scripts/bmas backup
```

The command performs these steps:

1. It stops Mission Control and the daemon when they run.
2. It archives SQLite, uploads, and artifacts.
3. It starts the stopped services.
4. It waits for readiness.

The default destination is `backups/`. Git ignores this directory.

Select another destination when needed:

```bash
./scripts/bmas backup /path/to/backup-directory
```

Move the final archive to another host or an object store.

## Restore

The restore command replaces current SQLite, upload, and artifact data. It creates a safety backup first.

```bash
./scripts/bmas restore backups/bmas-data-YYYYMMDDTHHMMSSZ.tar.gz --yes
```

The command validates the archive before it stops services. It restarts services and checks readiness after extraction.

Run a smoke task after a restore.

## Common failures

### The provider key is empty

1. The startup check stops before Compose starts.
2. The selected `models.*.api_key_env` value has no value in `.env`.
3. Set that exact variable in `.env`, then run `./scripts/bmas doctor`.

### LiteLLM is not ready

1. The readiness response marks `LiteLLM` as failed.
2. LiteLLM can fail when a provider variable is missing or its generated configuration is invalid.
3. Run `docker compose logs litellm`, correct the first startup error, then recreate LiteLLM.

```bash
docker compose up -d --build litellm
./scripts/bmas doctor --wait 60
```

### The execution agent is not ready

1. The readiness response marks `Execution agents` as failed.
2. The agent can fail when LiteLLM is unavailable or an execution key differs.
3. Compare the daemon and agent service logs, then recreate the agent.

```bash
docker compose logs agent daemon
docker compose up -d --build agent daemon
```

### Mission Control cannot reach the daemon

1. Mission Control shows a daemon connection error.
2. The daemon can still be starting, or `BMAS_DAEMON_URL` can point to the wrong address outside Compose.
3. Run the doctor command and check the dashboard plus daemon logs.

```bash
./scripts/bmas doctor --wait 60
docker compose logs dashboard daemon
```

### A task remains queued

1. The task status does not change from `pending`.
2. The active-task limit, endpoint capacity, or an open circuit can block dispatch.
3. Read `/health`, then inspect `task_queue` and `runtime.endpoint_requests`.

Reduce request load or correct the failed endpoint. Do not increase capacity before you identify the failed dependency.

### A task reaches the cost ceiling

1. The task stops before the expected final answer.
2. `coordination.classic.budget_ceiling_usd` stopped further model calls.
3. Review the task cost view and model pricing before you increase the ceiling.

### Events stop updating

1. Mission Control stops showing new task events.
2. Redis or the durable event outbox can be unavailable or overloaded.
3. Read the `event_delivery` object from `/health`, then inspect Redis and daemon logs.

SQLite still owns the durable task record. Restarting Mission Control does not repair a daemon delivery failure.

## Configuration warnings

Published example files must load without a warning.

Run this check:

```bash
python3 scripts/validate_configs.py
```

Replace the old `traditional` name with `classic`. Replace `triage.backend: gemini` with `triage.backend: cloud`.

## Capacity changes

Capture a baseline before you change admission or endpoint limits.

Use the [Classic Harness](CLASSIC_HARNESS.md) for lifecycle and soak checks. Change one limit, repeat the test, and compare queue plus cost data.

## Data deletion warning

`docker compose down` keeps named volumes.

`docker compose down -v` deletes all named volumes for this Compose project. This includes the SQLite database, uploads, artifacts, Redis data, and agent retry state.

Create and verify a backup before you use the `-v` option.
