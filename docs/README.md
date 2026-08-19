# Documentation

[Return to the project README](../README.md).

This documentation describes the current classic implementation.

## Getting started

1. Use the [Quick Start](QUICKSTART.md) to run the single-host starter.
2. Read [Concepts](CONCEPTS.md) to learn the service and task flow.
3. Use [Configuration](CONFIGURATION.md) when you change the starter.

## Operator guides

| Guide | Purpose |
|:---|:---|
| [Deployment](DEPLOYMENT.md) | Select a local, LAN, or internet deployment. |
| [Operations](OPERATIONS.md) | Check readiness, read logs, back up data, and recover services. |
| [Node Setup](NODE_SETUP.md) | Add advanced Hermes execution nodes. |
| [Classic Harness](CLASSIC_HARNESS.md) | Verify lifecycle and fault behavior. |

## Developer guides

| Guide | Purpose |
|:---|:---|
| [Development](DEVELOPMENT.md) | Install tools and run local checks. |
| [Architecture](architecture/README.md) | Study the internal runtime design. |
| [Platform Foundations](PLATFORM_FOUNDATIONS.md) | Read the shared data and delivery contracts. |
| [Design System](design/DESIGN.md) | Change the Mission Control interface. |

## Reference

| Reference | Purpose |
|:---|:---|
| [Generated configuration schema](reference/config.schema.json) | Validate `bmas.yaml` in an editor or a tool. |
| [Hermes API](HERMES_API.md) | Integrate an advanced Hermes node. |

The generated schema follows `daemon/src/config_schema.py`. Run `make docs-check` after each documentation or configuration change.
