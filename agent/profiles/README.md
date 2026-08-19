# Classic Hermes Profiles

These profiles define the six Hermes roles used by the classic runtime.

Read the [architecture guide](../../docs/architecture/README.md) for the complete task flow.

## Profile set

| Profile | Purpose | Suggested tool scope |
|:---|:---|:---|
| `planner` | Splits the task and identifies needed evidence. | Read, research, and planning tools |
| `expert` | Supplies task-specific domain work. | Tools required by the task |
| `critic` | Finds unsupported claims, gaps, and defects. | Read-only evidence tools |
| `conflict_resolver` | Resolves incompatible contributions. | Read and research tools |
| `cleaner` | Condenses or removes low-value board content. | Board-content tools only |
| `decider` | Selects and verifies the final answer. | Read and verification tools |

The table gives a starting scope. Review every tool against the node trust boundary.

## Files

Each profile directory contains two reviewed files.

- `SOUL.md` defines the durable role identity.
- `config.yaml` defines the Hermes tool and model settings.

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

The Hermes Runs API receives the profile name in the run request. The CLI fallback passes `--profile PROFILE` to Hermes.

## Installation

Copy only the required profiles to the service user's Hermes profile directory.

The [Hermes Node Setup](../../docs/NODE_SETUP.md) uses `/var/lib/bmas-agent/.hermes/profiles`.

Do not copy provider keys into a profile directory. Keep node secrets in the protected service environment file.
