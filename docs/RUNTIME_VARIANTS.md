# Runtime Variants

[Return to the documentation index](README.md).

Stigmergic registers three versioned coordination runtimes. The daemon publishes their exact capabilities through `GET /capabilities`.

## Runtime comparison

| Runtime identifier | Coordination | Recovery checkpoint | Benchmark seed behavior |
|:---|:---|:---|:---|
| `classic` | A control unit selects roles and reads a shared blackboard. | Classic saves its board and runtime lifecycle state. | The runtime applies the saved seed. |
| `patchboard` | Independent contributors produce patches in parallel. One integrator creates the result. | The runtime saves the complete patch set before integration. | The attempt records the seed. The runtime does not alter agent sampling. |
| `stigmergic` | Ordered workers revise one shared artifact for one or more rounds. One integrator creates the result. | The runtime saves each artifact revision and its next step. | The attempt records the seed. The runtime does not alter agent sampling. |

The `traditional` identifier remains an input alias for Classic. Do not use it in new requests.

## Patchboard configuration

Patchboard accepts these test-arm fields:

| Field | Type | Default | Constraint |
|:---|:---|:---|:---|
| `contributor_roles` | string list | `planner`, `critic` | One through six unique, enabled role-registry names. |
| `integrator_role` | string | `decider` | One enabled role-registry name. |
| `rounds` | integer | `1` | Exactly one. Patchboard always uses one independent contribution stage. |
| `submission_overrides` | object | empty | Per-task routing and role-registry overrides. |

Patchboard creates stable activation identifiers from the task, runtime, and step. A retry returns a saved activation result when the agent supports idempotency.

## Stigmergic workspace configuration

Stigmergic workspace accepts these test-arm fields:

| Field | Type | Default | Constraint |
|:---|:---|:---|:---|
| `worker_roles` | string list | `planner`, `critic` | One through six unique, enabled role-registry names. |
| `integrator_role` | string | `decider` | One enabled role-registry name. |
| `rounds` | integer | `2` | One through six ordered revision rounds. |
| `submission_overrides` | object | empty | Per-task routing and role-registry overrides. |

The first worker receives the objective as the initial artifact. Each later worker receives the latest saved revision.

## Capability rules

Mission Control selects a runtime only when all conditions pass:

1. The daemon reports the runtime as available.
2. Mission Control has an adapter for the reported contract version.
3. The runtime reports the required interface features.

An unknown runtime fails closed. The daemon does not change it to Classic.

## Qualification

A static qualification checks the runtime descriptor and preflight checksum. It returns a provisional result because it has no task evidence.

A run qualification needs one completed benchmark run. It checks each latest attempt snapshot against the runtime contract.

Use distinct evidence runs when you change a runtime contract. Saved qualification records include the contract version and report checksum.

## Extension contract

Add a new runtime through the shared registry. Do not add runtime-specific columns to benchmark tables.

The runtime must capture all effective settings, validate saved schema versions, use stable activation identifiers, save recovery state, and return a typed result.

Add a Mission Control adapter for each advertised feature. Keep the runtime unavailable until lifecycle, restart, replay, benchmark, and browser contract tests pass.
