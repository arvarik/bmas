# Roadmap — Agent Integration

> Leveraging Hermes Agent runtime capabilities (tools, memory, autonomy) within the bMAS blackboard architecture. Currently, the daemon treats Hermes agents as stateless text-in/text-out endpoints — these items wire the agent's native features into the blackboard data flow.

## 🔴 High Priority

### Tool-Augmented Blackboard Writes

The daemon's `_dispatch_agent()` sends a prompt and receives a flat string. Hermes agents have native tool-calling capabilities (web search, web scraping, shell execution, code execution) but the dispatch payload never enables them, and the response format never captures tool call artifacts. This means agents can't bring external evidence into the debate, and other agents can't challenge the *sources* — only the conclusions.

**What changes:**
- The dispatch payload declares which tools the agent may use for this task
- The agent returns structured results: analysis + raw tool call artifacts (search queries, URLs scraped, excerpts, source links)
- The daemon writes all artifacts to the blackboard so other agents can read, verify, and challenge the evidence directly

**Example flow (investment research):**
1. Executor Agent A searches "NVIDIA 2026 earnings" → finds 3 articles → writes structured findings **with source URLs** to the blackboard
2. Executor Agent B reads Agent A's sources on the blackboard → disagrees on valuation → searches "NVIDIA supply chain tariff exposure" → writes counter-evidence with sources
3. Decider reads both positions and their supporting evidence → evaluates consensus

Implementation:
- [ ] Extend `_dispatch_agent()` payload to include a `tools` field (e.g., `["web_search", "web_scrape", "code_exec"]`)
- [ ] Define a structured agent response schema that separates `result`, `tool_calls`, `sources`, and `confidence`
- [ ] Write tool call artifacts to the blackboard alongside agent analysis (new Redis key pattern: `bmas:public:{task}:evidence:{agent}`)
- [ ] Pass existing blackboard evidence into each dispatch call so agents can see (and challenge) each other's sources
- [ ] Add a `tools` configuration per-role in `bmas.yaml` (e.g., executors get search + scrape, auditors get read-only)
- [ ] Update Mission Control Blackboard Inspector to display tool call artifacts and source links
- [ ] Add rate limiting / budget controls for tool calls to prevent runaway search costs

## 🟡 Medium Priority

### Procedural Memory as Persistent Blackboard Layer

Hermes agents create "skill documents" — reusable markdown files written after solving complex tasks (5+ tool calls, error recovery, or user corrections). These encode *how* to do something, not just what was done. Currently, skills are completely siloed per-agent and never fed back into bMAS task execution.

Connecting skills to the blackboard gives agents institutional memory across tasks. The planner remembers that a particular decomposition strategy worked well for financial analysis. The executor knows which search patterns yielded high-quality sources last time. The system gets better at recurring task types instead of starting from zero every time.

**What changes:**
- After task completion, agents persist learned procedures as Hermes skills
- Before dispatching a new task, the daemon queries each agent's skill index for relevant prior art
- Relevant skills are injected into the blackboard context so all agents benefit from any single agent's past experience

Implementation:
- [ ] After task completion, include a `persist_skills: true` flag in the dispatch payload to trigger Hermes skill creation
- [ ] Add a skill relevance query step to the orchestrator's task setup phase (query `GET /skills` on each agent, match against task description)
- [ ] Inject matched skills into the dispatch `context` field so agents receive relevant prior art
- [ ] Add a shared skills namespace to the blackboard (`bmas:public:skills:{domain}`) for cross-agent skill sharing
- [ ] Expose skill lineage in Mission Control — show which skills influenced a task's execution
- [ ] Add skill pruning: periodically evaluate skill utility and retire low-value skills to prevent context bloat

### Agent-Initiated Blackboard Observation (Pull Model)

The current architecture is push-based: the daemon tells each agent exactly what to do and when. The blackboard pattern implies the opposite — agents should *observe* shared state and decide to act autonomously. Hermes's "Crons" feature enables exactly this: background processes that run on a schedule without being prompted.

This is the architectural shift from "orchestrator as puppeteer" to "orchestrator as referee." The daemon's control unit manages consensus, enforces timeouts, and resolves deadlocks — but agents drive their own participation by watching the blackboard for changes relevant to their role.

**What changes:**
- Each agent runs a background cron that watches its relevant blackboard namespace via Redis pub/sub or polling
- When new evidence appears (another agent posted findings, a critique was written), the agent autonomously decides whether to respond
- The control unit becomes a referee: managing consensus thresholds, enforcing `max_rounds` / `max_duration`, and breaking deadlocks

Implementation:
- [ ] Define a blackboard observation protocol: agents subscribe to relevant Redis key patterns (e.g., executor watches `bmas:public:{task}:evidence:*`)
- [ ] Configure Hermes Crons per-agent to poll the blackboard at a configurable interval (e.g., every 10s during active tasks)
- [ ] Add an agent-side decision function: "given current blackboard state, should I act?" (prevents unnecessary cycles)
- [ ] Refactor the daemon's dispatch model to support both push (explicit dispatch) and pull (agent-initiated) modes
- [ ] Add a `participation_mode` config to `bmas.yaml` per-role: `push` (current behavior), `pull` (agent-driven), or `hybrid`
- [ ] Implement deadlock detection in the control unit: if no agent acts for N intervals, force a dispatch or escalate
- [ ] Update Mission Control to show agent activity source (dispatched vs. self-initiated) in the task timeline
