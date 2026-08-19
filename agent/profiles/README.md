# Classic Hermes Profiles

These profiles define the six Hermes roles used by the classic runtime. The configuration files target Hermes Agent v0.20.4.

Read the [architecture guide](../../docs/architecture/README.md) for the complete task flow.

## Profile set

| Profile | Purpose | Suggested tool scope |
|:---|:---|:---|
| `planner` | Splits the task and identifies needed evidence. | Web, browser, terminal, file, and memory tools |
| `expert` | Supplies task-specific domain work. | Web, browser, terminal, code, file, and memory tools |
| `critic` | Finds unsupported claims, gaps, and defects. | Web, browser, file, and memory tools |
| `conflict_resolver` | Resolves incompatible contributions. | Web, browser, file, and memory tools |
| `cleaner` | Condenses or removes low-value board content. | No tools |
| `decider` | Selects and verifies the final answer. | Web and memory tools |

The table gives a starting scope. Review every tool against the node trust boundary.

The Hermes `file` toolset includes write and patch tools. The Hermes `terminal` toolset executes commands.

A profile separates Hermes state, but it does not create a security boundary. Use operating-system permissions or a sandbox backend for enforced restrictions.

## Files

Each profile directory contains two reviewed files.

- `SOUL.md` defines the durable role identity.
- `config.yaml` defines the Hermes model and per-platform toolsets.

The daemon sends the current objective, role prompt, and blackboard view with each activation. Do not put task-specific instructions in `SOUL.md`.

## Dispatch

The `coordination.role_registry` section maps a classic role to a profile and an agent endpoint.

```yaml
coordination:
  role_registry:
    critic:
      enabled: true
      preferred_host: 192.168.1.21
      profile: critic
      dispatch_port: 8000
```

The Hermes Runs API does not receive a profile field. A profile-scoped gateway process selects its configured profile.

A shared multi-profile gateway selects a profile through the `/p/<profile>/` URL prefix. Configure `HERMES_GATEWAY_URL` with that prefix.

The CLI fallback passes `-p PROFILE` to Hermes.

The profile templates configure both the `cli` and `api_server` platforms. The CLI fallback uses `cli`, and the Runs API uses `api_server`.

## Installation

Copy only the required profiles to the service user's Hermes profile directory.

The [Hermes Node Setup](../../docs/NODE_SETUP.md) uses `/var/lib/bmas-agent/.hermes/profiles`.

Do not copy provider keys into a profile directory. Keep node secrets in the protected service environment file.

Run `hermes -p PROFILE config check` after each copy. Then confirm the active tools through `GET /v1/toolsets` for the selected profile.
