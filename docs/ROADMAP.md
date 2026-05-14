# bMAS — Roadmap

> Planned enhancements for the Blackboard Multi-Agent System framework, organized by priority.
> This implementation is inspired by the [bMAS paper (Han & Zhang, 2025)](https://arxiv.org/abs/2507.01701).

## 🔴 High Priority

### LLM-Driven Control Unit
**Paper reference:** §3.2 Control Unit

The current orchestrator uses a fixed pipeline (plan → execute → audit). The original bMAS paper proposes an LLM-powered control unit that dynamically selects which agents act in each round based on the current blackboard state — enabling adaptive collaboration patterns per task.

- [ ] Implement an LLM-based control unit agent that reads blackboard state and selects the next agent(s)
- [ ] Support variable round counts (current: fixed 3-phase; target: iterate until consensus or max rounds)
- [ ] Add a **Decider** agent that evaluates whether blackboard messages are sufficient for a final answer
- [ ] Implement consensus detection via cumulative similarity scoring across agent answers
- [ ] Make `max_rounds` configurable in `bmas.yaml` (default: 4, per the paper's recommendation)

### Dynamic Role Assignment (6+ Role bMAS)
**Paper reference:** §3.2 Agent Group

The current implementation uses a fixed 3-role topology (planner, executor, auditor). The paper defines 5 predefined roles plus query-specific experts, where any node can assume any role per-task:

| Role | Purpose |
|:---|:---|
| **Planner** | Decomposes complex tasks into sub-task graphs |
| **Decider** | Assesses if messages are sufficient for a final answer |
| **Critic** | Points out errors and hallucinations, forces rethinking |
| **Conflict-Resolver** | Detects contradictions, triggers private debate between conflicting agents |
| **Cleaner** | Removes redundant/useless messages to reduce token consumption |
| **Query Experts** | Dynamically generated domain-specific experts per task |

Implementation:
- [ ] Refactor `orchestrator.py` to support N agents with configurable roles
- [ ] Make the execution flow driven by the control unit (not hardcoded pipeline)
- [ ] Add Critic and Conflict-Resolver agent personas to `personas.py`
- [ ] Add Cleaner agent that prunes redundant blackboard messages between rounds
- [ ] Update `useBlackboard.ts` TypeScript types to support dynamic agent panels
- [ ] Update Mission Control UI for variable agent counts and roles

### Cloud-Based Triage
Currently, triage requires an NVIDIA GPU running a local vLLM instance. Users without GPUs should be able to use a cheap cloud model for complexity classification.
- [ ] Add `triage.provider` field to `bmas.yaml` (e.g., `local`, `cloud`)
- [ ] When `provider: cloud`, use a configured model (e.g., Gemini Flash Lite) via LiteLLM
- [ ] Maintain the same triage prompt and complexity enum
- [ ] Update documentation

### Mission Control — URL Routing & Smooth Navigation
Currently, Mission Control uses in-memory state (`activeNav`) to switch between views. There are no URL slugs, so deep-linking and browser back/forward don't work. View transitions also lack animation.

- [ ] Implement URL-based routing (e.g., `/overview`, `/dag`, `/logs`, `/operator`, `/blackboard`, `/cost`, `/infra`, `/skills`)
- [ ] Ensure browser back/forward buttons navigate between views correctly
- [ ] Add smooth page transitions (crossfade or slide) so switching feels snappy, not jarring
- [ ] Preserve scroll position when navigating back to a previously visited view
- [ ] Support deep-linking: opening `http://host:9321/infra` goes directly to Infrastructure

### Private Blackboard Space
**Paper reference:** §3.2 Blackboard

The paper distinguishes between public and private blackboard spaces. Private spaces allow specific agents to debate or self-reflect without polluting the shared context. We have the namespace schema (`bmas:private:{session}:debate`) but don't fully utilize it for structured agent-to-agent private debates.

- [ ] Implement private debate flow: conflict-resolver identifies contradictions → conflicting agents debate in private space → each writes a revised message to public space
- [ ] Add private space management to `blackboard.py` (create, read, wipe per debate)
- [ ] Expose private debate history in Mission Control's Blackboard Inspector

## 🟡 Medium Priority

### Multi-LLM Agent Diversity
**Paper reference:** §3.1 Generating Agents

The paper shows that agents using different base LLMs outperform single-model systems because "model diversity can effectively compensate for individual model limitations while amplifying their strengths."

- [ ] Support per-node `model_override` in `bmas.yaml` to assign different LLMs to different agents
- [ ] Random LLM assignment mode: each agent randomly selects from the `models` pool at task start
- [ ] Extend LiteLLM routing to support per-agent model affinity

### Dynamic Expert Generation
**Paper reference:** §3.1 Generating Agents

For each task, generate query-specific expert identities (beyond the fixed planner/executor/auditor roles). We partially do this in the complex research flow but should generalize it.

- [ ] Use an "Agent Generator" (AG) agent that produces domain-specific expert personas from the query
- [ ] Each expert gets a tuple `(identity, description)` used as role prompts
- [ ] Expose generated experts in Mission Control's agent panel

### Multi-Node Load Balancing for `local` Routing
When routing to `local`, currently maps to `edge-node-1` only. LiteLLM supports load balancing across multiple model deployments with the same name.
- [ ] Register all edge nodes under a shared `local` model group in LiteLLM
- [ ] Support round-robin, least-busy, and latency-based routing strategies
- [ ] Expose routing strategy in `bmas.yaml`

### Automated Node Provisioning (`bmas provision`)
Currently, users must manually set up Hermes agents and llama.cpp on each edge node. A provisioning CLI would automate this via SSH.
- [ ] Design `bmas provision` CLI interface
- [ ] Support SSH-based provisioning (Ansible, Fabric, or plain SSH)
- [ ] Auto-install llama.cpp with Vulkan support
- [ ] Auto-download and configure inference models
- [ ] Auto-install and configure Hermes agent with persona
- [ ] Create systemd service files on each node

### Config Hot Reload
Currently, changing `bmas.yaml` requires `docker compose restart`. Components should detect changes and reload automatically.
- [ ] File watcher in daemon (`watchdog` or inotify)
- [ ] File watcher in Mission Control (chokidar or fs.watch)
- [ ] Graceful reload without dropping active tasks

### Redis Key Prefix Configurable
Currently hardcoded to `bmas:`. Making it configurable allows multiple deployments to share a single Redis instance.
- [ ] Add `project.redis_prefix` to `bmas.yaml`
- [ ] Thread prefix through all Redis operations in `blackboard.py`
- [ ] Thread prefix through all API routes in Mission Control

### HITL (Human-in-the-Loop) Improvements
- [ ] Support approval workflows for high-cost tasks
- [ ] Configurable approval thresholds per complexity tier
- [ ] Slack/Discord notifications for pending approvals

## 🟢 Low Priority

### Majority Vote Consensus
**Paper reference:** §3.2 Control Unit

When the blackboard cycle ends, all agents present a final answer. A cumulative similarity metric selects the answer most agreed upon. This provides a more robust consensus than the current single-auditor model.

- [ ] Implement cumulative similarity scoring for agent answers
- [ ] Support both decider-based and majority-vote consensus modes
- [ ] Make consensus strategy configurable in `bmas.yaml`

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

## References

- **Han, B. & Zhang, S. (2025).** *Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture.* [arXiv:2507.01701](https://arxiv.org/abs/2507.01701) — The foundational paper for the bMAS architecture.
