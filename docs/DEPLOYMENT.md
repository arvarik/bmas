# Deployment

Choose one deployment boundary before you change any network setting.

## Deployment modes

| Mode | Published address | Required controls |
|:---|:---|:---|
| Local computer | `127.0.0.1` | Generated keys and local host access |
| Trusted LAN | One private LAN address | Host firewall, generated keys, and restricted source addresses |
| Internet | `127.0.0.1` behind a reverse proxy | TLS, firewall, access policy, backups, and monitoring |

The starter defaults to `127.0.0.1`. Keep this default unless another computer must connect.

## Local computer

Use the normal starter commands.

```bash
./scripts/bmas init --provider gemini
./scripts/bmas up
./scripts/bmas smoke
```

Docker publishes each port on the local loopback interface. Another computer cannot connect to these ports.

## Trusted LAN

Set one specific private address in `.env`.

```env
BMAS_BIND_ADDRESS=192.168.1.10
```

Do not use `0.0.0.0` when one specific address works.

Apply host firewall rules before you restart the stack. Allow only the required source addresses.

Expose Mission Control port `9321` to operators. Expose daemon port `9000` only to approved API clients.

Do not expose Redis, LiteLLM, or the starter agent to general LAN clients.

Use HTTPS on a LAN when the browser crosses an untrusted wireless or shared network. Mission Control uses a reusable password.

## Internet deployment

Keep every Compose port bound to `127.0.0.1`. Put a reverse proxy on the same host.

The reverse proxy must provide these controls:

1. Terminate TLS with a valid certificate.
2. Forward operator traffic only to `127.0.0.1:9321`.
3. Set request and idle timeouts for event streams.
4. Limit request rates and request body sizes.
5. Record access logs without secrets.
6. Apply an identity-aware access policy when possible.

Do not proxy Redis, LiteLLM, the starter agent, or the daemon unless a specific integration requires that API.

If an integration requires the daemon API, proxy only required routes and require `BMAS_API_KEY`.

## Authentication keys

The `init` command creates separate values for each trust boundary.

| Key | Client | Server |
|:---|:---|:---|
| `BMAS_DASHBOARD_KEY` | Browser | Mission Control proxy |
| `BMAS_API_KEY` | Mission Control and approved API clients | Daemon |
| `BMAS_EXECUTE_KEY` | Daemon | Execution agent |
| `BMAS_NODE_KEY` | Execution agent | Daemon ingest API |
| `LITELLM_MASTER_KEY` | Daemon and execution agent | LiteLLM |

Keep each value different. Store `.env` with `0600` permissions.

Rotate one key at a time. Restart every client and server that uses that key.

## Provider keys

Give each deployment its own provider key when the provider supports separate keys. Set a provider-side budget and alert.

The model section stores only the provider variable name. The `.env` file stores the value.

## Persistent data

The Compose stack creates these named volumes:

| Volume | Data |
|:---|:---|
| `daemon-data` | SQLite task database |
| `uploads-data` | User uploads |
| `artifacts-data` | Task artifacts |
| `redis-data` | Redis snapshots and append-only files |
| `agent-data` | Agent activation cache and trace retry files |

SQLite is the authoritative task store. Redis can rebuild live projections from durable task events.

Benchmark scheduler workers use SQLite transactions and fenced leases. Multiple daemon processes can coordinate only when they use the same local SQLite file and host filesystem locks.

Do not place the SQLite file on a network filesystem. SQLite documents this restriction in its [network filesystem guidance](https://www.sqlite.org/useovernet.html).

Use one daemon host for the current production design. A later shared database control plane must replace SQLite before a multi-host daemon deployment.

Do not use `docker compose down -v` unless you intend to delete all named-volume data.

## Backups

Run the repository backup command.

```bash
./scripts/bmas backup
```

The command pauses the daemon, archives its durable data, restarts the daemon, and checks readiness. It stores the archive under `backups/` by default.

Copy each archive to storage outside the Docker host. Test a restore on a separate host before you rely on the backup.

The [Operations guide](OPERATIONS.md) gives the restore procedure.

## Image versions

The Compose file and Dockerfiles use fixed image versions. This prevents an unrelated upstream release from changing a normal rebuild.

Review each version change in a separate pull request. Run the complete test command and smoke test before deployment.

## Upgrade procedure

1. Read the incoming changes.
2. Run `./scripts/bmas test` on the new source.
3. Run `./scripts/bmas backup` on the deployed source.
4. Build with `docker compose build`.
5. Start with `docker compose up -d`.
6. Run `./scripts/bmas doctor --wait 180`.
7. Run `./scripts/bmas smoke`.
8. Review daemon, agent, and LiteLLM logs.

## Rollback procedure

1. Keep the failed logs and the backup archive.
2. Return the source to the previous reviewed commit.
3. Rebuild the previous fixed image versions.
4. Start the previous stack.
5. Restore the backup if the previous code cannot read the upgraded database.
6. Run the doctor and smoke commands.

Perform a rollback on a maintenance window when other users can submit tasks.

## Production checklist

- The published address matches the selected deployment mode.
- A firewall blocks unnecessary ports.
- Internet traffic uses TLS.
- Every authentication key has a unique value.
- Provider budgets and alerts are active.
- Backup archives leave the Docker host.
- An operator has tested restore steps.
- Monitoring checks `/health` and `/readiness`.
- The operator can read container logs.
- The deployment records the source commit and image versions.
- All benchmark scheduler workers share one supported local SQLite file.
