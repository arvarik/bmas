# bMAS — Roadmap

> Planned enhancements for the Blackboard Multi-Agent System framework, organized by category.
> This implementation is inspired by the [bMAS paper (Han & Zhang, 2025)](https://arxiv.org/abs/2507.01701).

## Categories

| Category | Description |
|:---|:---|
| [Control Unit & Orchestration](control-unit.md) | LLM-driven control unit, dynamic role assignment, consensus strategies |
| [Agent Architecture](agent-architecture.md) | Multi-LLM diversity, dynamic expert generation |
| [Agent Integration](agent-integration.md) | Hermes tool calling, procedural memory, agent-initiated observation |
| [Blackboard](blackboard.md) | Private blackboard spaces and structured debates |
| [Mission Control](mission-control.md) | UI overhaul, task history management |
| [Infrastructure](infrastructure.md) | Triage, load balancing, provisioning, config, Redis |
| [Operations](operations.md) | HITL, backups, multi-tenancy, observability, plugins |

## Priority Legend

- 🔴 **High** — Core to the bMAS paper's architecture or blocking user workflows
- 🟡 **Medium** — Significant improvements to usability or scalability
- 🟢 **Low** — Nice-to-have enhancements and ecosystem integrations

## References

- **Han, B. & Zhang, S. (2025).** *Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture.* [arXiv:2507.01701](https://arxiv.org/abs/2507.01701) — The foundational paper for the bMAS architecture.
