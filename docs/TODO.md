# bMAS — Future Roadmap

> This document tracks planned enhancements beyond v1. Items are organized by priority.

## High Priority

### Dynamic Role Assignment (6-Role bMAS)
The current implementation uses a fixed 3-role topology (planner, executor, auditor).
The original bMAS paper calls for 6 roles where any node can assume any role per-task:
- **Coordinator** — Manages task lifecycle and DAG construction
- **Planner** — Decomposes tasks into sub-task graphs
- **Executor** — Implements sub-tasks (code, research, data processing)
- **Auditor** — Reviews work products and validates outputs
- **Critic** — Provides adversarial review and identifies edge cases
- **Synthesizer** — Merges outputs from multiple agents into coherent deliverables

This requires:
- [ ] Refactor `orchestrator.py` to support N agents with configurable roles
- [ ] Make the DAG execution flow configurable (not hardcoded planner→executor→auditor)
- [ ] Update `useBlackboard.ts` TypeScript types to support dynamic roles
- [ ] Update Mission Control UI components for dynamic agent panels

### Cloud-Based Triage
Currently, triage requires an NVIDIA GPU running a local vLLM instance.
Users without GPUs should be able to use a cheap cloud model for complexity classification.
- [ ] Add `triage.provider` field to `bmas.yaml` (e.g., `local`, `cloud`)
- [ ] When `provider: cloud`, use a configured model (e.g., Gemini Flash Lite) for classification
- [ ] Maintain the same triage prompt and complexity enum
- [ ] Update documentation

### Automated Node Provisioning (`bmas provision`)
Currently, users must manually set up Hermes agents and llama.cpp on each edge node.
A provisioning CLI would automate this via SSH.
- [ ] Design `bmas provision` CLI interface
- [ ] Support SSH-based provisioning (Ansible, Fabric, or plain SSH)
- [ ] Auto-install llama.cpp with Vulkan support
- [ ] Auto-download and configure inference models
- [ ] Auto-install and configure Hermes agent with persona
- [ ] Create systemd service files on each node

## Medium Priority

### Multi-Node Load Balancing for `local` Routing
When routing to `local`, currently maps to `edge-node-1` only.
LiteLLM supports load balancing across multiple model deployments with the same name.
- [ ] Register all edge nodes under a shared `local` model group in LiteLLM
- [ ] Support round-robin, least-busy, and latency-based routing strategies
- [ ] Expose routing strategy in `bmas.yaml`

### Config Hot Reload
Currently, changing `bmas.yaml` requires `docker compose restart`.
Components should detect changes and reload automatically.
- [ ] File watcher in daemon (`watchdog` or inotify)
- [ ] File watcher in Mission Control (chokidar or fs.watch)
- [ ] Graceful reload without dropping active tasks

### Redis Key Prefix Configurable
Currently hardcoded to `bmas:`. Making it configurable allows multiple deployments
to share a single Redis instance.
- [ ] Add `project.redis_prefix` to `bmas.yaml`
- [ ] Thread prefix through all Redis operations in `blackboard.py`
- [ ] Thread prefix through all API routes in Mission Control

### HITL (Human-in-the-Loop) Improvements
- [ ] Support approval workflows for high-cost tasks
- [ ] Configurable approval thresholds per complexity tier
- [ ] Slack/Discord notifications for pending approvals

## Low Priority

### Kubernetes Support
- [ ] Helm chart for bMAS control plane
- [ ] Kubernetes operator for managing agent nodes
- [ ] Horizontal pod autoscaling for inference nodes

### Multi-Tenant Support
- [ ] API authentication for Mission Control
- [ ] Per-user task isolation on the blackboard
- [ ] Role-based access control (RBAC)

### Observability
- [ ] OpenTelemetry integration for distributed tracing
- [ ] Prometheus metrics endpoint on the daemon
- [ ] Grafana dashboard templates
- [ ] Cost alerting (budget limits per complexity tier)

### Plugin System
- [ ] Custom agent types via plugin interface
- [ ] Custom triage classifiers
- [ ] Webhook integrations (on task complete, on error, etc.)
