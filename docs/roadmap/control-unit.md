# Roadmap — Control Unit & Orchestration

> Enhancements to the bMAS control unit, round management, and consensus strategies.

## 🔴 High Priority

### LLM-Driven Control Unit & Cyclic Execution
**Paper reference:** §3.2 Control Unit

The current orchestrator uses a fixed, acyclic pipeline (plan → execute → audit → done). This has two fundamental problems:

1. **Static agent selection** — The paper proposes an LLM-powered control unit that dynamically selects which agents act each round based on blackboard state, rather than a hardcoded sequence.
2. **No cycles** — A DAG cannot loop by definition. This conflicts with the blackboard pattern, where agents should continuously observe shared state and react until a goal is met. Long-horizon tasks (e.g. multi-hour investment research with ongoing debate) are impossible under a one-shot pipeline.

The fix is a single architectural change: replace the DAG with an **OODA loop** (Observe → Orient → Decide → Act) driven by an LLM control unit. The control unit reads the blackboard, decides which agents act next, and cycles until a consensus threshold is reached or a timeout expires.

**Execution model:**
1. **Genesis** — Planner writes the objective, consensus threshold, and initial phase to the blackboard
2. **Independent assessment** — Agents autonomously search, analyze, and write findings to the blackboard without direct communication
3. **Cross-examination** — Agents read each other's work and write critiques, rebuttals, or requests for additional research back to the board
4. **Consensus check** — The Decider evaluates agreement level across agents
   - Below threshold → write directive focusing the next cycle on unresolved disagreements
   - Above threshold → mark task as `Verified_Complete` and suspend

Implementation:
- [ ] Implement an LLM-based control unit that reads blackboard state and selects the next agent(s)
- [ ] Replace the DAG execution model in `orchestrator.py` with a cyclic directed graph (allow back-edges)
- [ ] Implement OODA-loop control: observe blackboard → orient on disagreements → decide next agents → act
- [ ] Add a **Decider** agent that evaluates whether blackboard state is sufficient for a final answer
- [ ] Add configurable `consensus_threshold` to `bmas.yaml` (e.g., `0.8` = 80% agent agreement)
- [ ] Make `max_rounds` configurable in `bmas.yaml` (default: 4, per the paper's recommendation)
- [ ] Add `max_duration` timeout for long-horizon tasks (hours/days) alongside `max_rounds`
- [ ] Implement periodic consensus scoring (cumulative similarity / sentiment / vote-based)
- [ ] Support phased task state on the blackboard (`Discovery` → `Debate` → `Convergence` → `Verified_Complete`)
- [ ] Update Mission Control to render cyclic graphs (rename "DAG" view to "Task Graph")
- [ ] Persist cross-examination and debate history for replay

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
- [ ] Add Critic and Conflict-Resolver agent personas to `personas.py`
- [ ] Add Cleaner agent that prunes redundant blackboard messages between rounds
- [ ] Update `useBlackboard.ts` TypeScript types to support dynamic agent panels
- [ ] Update Mission Control UI for variable agent counts and roles

**References:**
- [LLM-Consensus: Multi-Agent Debate](https://arxiv.org/abs/2410.20140v2) — Agents debate with freedom of opinion until consensus threshold, reducing hallucinations
- [CAST: PAT Blackboard Architecture](https://www.craft.ai/pat) — OODA-loop specification for modern LLM blackboards
- [Advanced LangGraph Orchestration (2026)](https://blog.langchain.dev/advanced-langgraph/) — How the graph ecosystem introduced cyclic edges for agent reflection

## 🟢 Low Priority

### Majority Vote Consensus
**Paper reference:** §3.2 Control Unit

When the blackboard cycle ends, all agents present a final answer. A cumulative similarity metric selects the answer most agreed upon. This provides a more robust consensus than the current single-auditor model.

- [ ] Implement cumulative similarity scoring for agent answers
- [ ] Support both decider-based and majority-vote consensus modes
- [ ] Make consensus strategy configurable in `bmas.yaml`
