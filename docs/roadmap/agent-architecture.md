# Roadmap — Agent Architecture

> Enhancements to agent generation, model diversity, and expert specialization.

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
