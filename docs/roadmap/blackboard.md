# Roadmap — Blackboard

> Enhancements to the blackboard communication layer, including private spaces and structured debates.

## 🔴 High Priority

### Private Blackboard Space
**Paper reference:** §3.2 Blackboard

The paper distinguishes between public and private blackboard spaces. Private spaces allow specific agents to debate or self-reflect without polluting the shared context. We have the namespace schema (`bmas:private:{session}:debate`) but don't fully utilize it for structured agent-to-agent private debates.

- [ ] Implement private debate flow: conflict-resolver identifies contradictions → conflicting agents debate in private space → each writes a revised message to public space
- [ ] Add private space management to `blackboard.py` (create, read, wipe per debate)
- [ ] Expose private debate history in Mission Control's Blackboard Inspector
