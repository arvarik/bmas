# bMAS Agent Profiles

> **Reference**: See the [Architecture docs](../docs/architecture/README.md) and [Han & Zhang (2025)](https://arxiv.org/abs/2507.01701).

Hermes **profiles** implement the persona library from the [LbMAS paper](https://arxiv.org/abs/2507.01701). Each profile is a fully isolated Hermes instance with its own `SOUL.md` (role identity), `config.yaml` (toolset scoping), memory, skills, and sessions.

## Profile Set (7 profiles)

| Profile | Paper role | Toolset | Home node |
|:--|:--|:--|:--|
| `planner` | Planner | web, browser, terminal, file | node-1 (.103) |
| `expert` | Dynamic expert (AG-generated) | **full** (web, browser, terminal, code_exec, file) | any (load-balanced) |
| `critic` | Critic | web, browser, file (read-only analysis) | node-1 (.103) |
| `conflict_resolver` | Conflict Resolver | web, browser, file | node-3 (.122) |
| `cleaner` | Cleaner | file only (board content) | node-1 (.103) |
| `decider` | Decider (sufficiency judgment) | web, file | node-2 (.112) |
| `universal` | V2 roleless (stigmergic) | **full** | any |

### What is NOT a profile

- **The CU scheduler** — it's the `TraditionalVariant` in the daemon (Python), not a Hermes agent.
- **Expert specializations** — experts share ONE `expert` profile. Domain identity is injected per-task via `AGENTS.md`, not by creating `expert_security`, `expert_perf`, etc.

## File Structure

```
agent/profiles/
├── README.md                    ← this file
├── planner/
│   ├── SOUL.md                  ← durable role identity
│   └── config.yaml              ← toolset scoping + model config
├── expert/
│   ├── SOUL.md
│   └── config.yaml
├── critic/
│   ├── SOUL.md
│   └── config.yaml
├── conflict_resolver/
│   ├── SOUL.md
│   └── config.yaml
├── cleaner/
│   ├── SOUL.md
│   └── config.yaml
├── decider/
│   ├── SOUL.md
│   └── config.yaml
└── universal/
    ├── SOUL.md
    └── config.yaml
```

## Three-Layer Identity Model

| Layer | File | Scope | Example |
|:--|:--|:--|:--|
| **Identity** (durable) | profile `SOUL.md` | who this agent *is* | "You are the Critic. You find errors." |
| **Operation** (per task) | per-turn `AGENTS.md` | what to do *now* | objective, phase, board snapshot, entry contract |
| **Capability** | profile `config.yaml` | what tools/model | toolsets, model affinity |

## Deployment

Profiles are replicated to **all 3 nodes** — any node can assume any role. The `role → (preferred_host, profile, dispatch_endpoint)` registry in `bmas.yaml` encodes home assignments with any-host fallback.

```bash
# Deploy profiles to all nodes
bash scripts/deploy_profiles.sh

# Verify on each node
ssh root@<node_ip> 'hermes profile list'
```

## Dispatch Mechanism

Phase 3a uses the **profile-aware bMAS bridge**: `hermes --profile <role> -z ...`.
The existing `api_server.py` on each node invokes `hermes --profile <role>` when
the daemon sends a `profile` field in the `TaskRequest` payload.

This is the documented fallback path (doc 06 §8, doc 12 §7). Per-profile
Runs API gateways are a Phase 3b upgrade.
