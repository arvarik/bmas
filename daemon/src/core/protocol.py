"""Redis v2 key patterns and SSE event names for the blackboard protocol.

Phase 0: definitions only — no emitters, no consumers.
These constants are the canonical source of truth for key layout
and event vocabulary (doc 04 §8–9).

Key functions resolve patterns to concrete keys at runtime.
Event constants are used by the gateway (Phase 2) and the SSE
endpoint (routes/events.py) to register / filter / validate.
"""
from __future__ import annotations

# ── Board Entry Types (doc 04 §1) ────────────────────────────────────
#
# Fixed vocabulary — the gateway validates proposed entries against this
# set.  Agents that omit the type get a default from their role mapping.

ENTRY_TYPE_OBJECTIVE = "objective"       # Control Unit (genesis) — the task goal
ENTRY_TYPE_ATTACHMENT = "attachment"     # Daemon (genesis) — an uploaded file
ENTRY_TYPE_PLAN = "plan"                # Planner — decomposition / strategy
ENTRY_TYPE_FINDING = "finding"          # Experts — assertion + evidence + reasoning
ENTRY_TYPE_CRITIQUE = "critique"        # Critic — identifies error/hallucination
ENTRY_TYPE_REBUTTAL = "rebuttal"        # Any — responds to a critique
ENTRY_TYPE_CONFLICT = "conflict"        # Conflict-Resolver — two entries contradict
ENTRY_TYPE_DIRECTIVE = "directive"      # Control Unit / operator — focuses next round
ENTRY_TYPE_SOLUTION = "solution"        # Decider — proposed/final answer
ENTRY_TYPE_ARTIFACT = "artifact"        # Daemon — a file the swarm produced

ENTRY_TYPES: frozenset[str] = frozenset({
    ENTRY_TYPE_OBJECTIVE,
    ENTRY_TYPE_ATTACHMENT,
    ENTRY_TYPE_PLAN,
    ENTRY_TYPE_FINDING,
    ENTRY_TYPE_CRITIQUE,
    ENTRY_TYPE_REBUTTAL,
    ENTRY_TYPE_CONFLICT,
    ENTRY_TYPE_DIRECTIVE,
    ENTRY_TYPE_SOLUTION,
    ENTRY_TYPE_ARTIFACT,
})

# ── Board Entry Statuses (doc 04 §1) ─────────────────────────────────

ENTRY_STATUS_OPEN = "open"              # Active entry (default)
ENTRY_STATUS_SUPERSEDED = "superseded"  # Replaced by a newer entry
ENTRY_STATUS_REMOVED = "removed"        # Cleaned (Cleaner) — stays in event log + SQLite

ENTRY_STATUSES: frozenset[str] = frozenset({
    ENTRY_STATUS_OPEN,
    ENTRY_STATUS_SUPERSEDED,
    ENTRY_STATUS_REMOVED,
})


# ── Redis Key Patterns (doc 04 §8) ───────────────────────────────────
#
# Additive to the existing namespace.  Old keys remain for backward
# compatibility during migration (doc 10).
#
# Every v2 key lives under the `bmas:board:`, `bmas:traces:`, or
# `bmas:files:` prefix.  The existing `bmas:events:{task}` Pub/Sub
# channel is retained and extended with new event names.

# Board state — one set of keys per task
BOARD_ENTRIES_KEY = "bmas:board:{task}:entries"        # Hash: entry_id → entry JSON (live, non-removed)
BOARD_EVENTS_KEY = "bmas:board:{task}:events"          # Stream: append-only committed event log
BOARD_META_KEY = "bmas:board:{task}:meta"              # Hash: phase, round, budget_spent, variant, decider_state
BOARD_PRIVATE_KEY = "bmas:board:{task}:private:{topic}"  # Hash: transient private sub-board
BOARD_SALIENCE_KEY = "bmas:board:{task}:salience"      # ZSet: entry_id scored by salience

# Agent traces — one stream per turn
TRACES_KEY = "bmas:traces:{task}:{turn}"               # Stream: agent trace events for a turn (doc 06)

# File metadata
FILES_KEY = "bmas:files:{task}"                        # Hash: uploaded file metadata (doc 17)

# Pub/Sub channels — existing, extended with new event names (§9)
EVENTS_CHANNEL = "bmas:events:{task}"                  # Channel: SSE bridge (existing)
EVENTS_SYSTEM_CHANNEL = "bmas:events:system"           # Channel: system-wide events (existing)


# ── Key Registry ─────────────────────────────────────────────────────
# Complete list of v2 key patterns for documentation, tests, and
# future key-space enumeration (e.g. task cleanup, TTL scans).

V2_KEY_PATTERNS: dict[str, dict[str, str]] = {
    BOARD_ENTRIES_KEY: {
        "type": "Hash",
        "purpose": "Snapshot: entry_id → entry JSON (live, non-removed)",
    },
    BOARD_EVENTS_KEY: {
        "type": "Stream",
        "purpose": "Append-only committed event log (live transport; SQLite is durable truth)",
    },
    BOARD_META_KEY: {
        "type": "Hash",
        "purpose": "phase, round, budget_spent, variant, decider_state",
    },
    BOARD_PRIVATE_KEY: {
        "type": "Hash",
        "purpose": "Transient private sub-board (conflict debates)",
    },
    BOARD_SALIENCE_KEY: {
        "type": "ZSet",
        "purpose": "entry_id scored by salience (fast top-N for budgeted views)",
    },
    TRACES_KEY: {
        "type": "Stream",
        "purpose": "Agent trace events for a turn (doc 06)",
    },
    FILES_KEY: {
        "type": "Hash",
        "purpose": "Uploaded file metadata (doc 17)",
    },
    EVENTS_CHANNEL: {
        "type": "Channel",
        "purpose": "SSE bridge — extended with v2 event names",
    },
}


def resolve_key(pattern: str, **kwargs: str) -> str:
    """Resolve a key pattern to a concrete Redis key.

    Example:
        resolve_key(BOARD_ENTRIES_KEY, task="task-abc")
        → "bmas:board:task-abc:entries"
    """
    return pattern.format(**kwargs)


def task_key_patterns() -> list[str]:
    """Return all v2 key patterns that are per-task (contain {task}).

    Useful for task cleanup: resolve each pattern for a given task_id
    and DELETE.
    """
    return [p for p in V2_KEY_PATTERNS if "{task}" in p]


# ── SSE Event Names (doc 04 §9) ──────────────────────────────────────
#
# New event types are additive.  The existing routes/events.py loop
# forwards any {event, data} published to bmas:events:{task_id} — we
# add new event names without touching the loop.
#
# Legacy events (debate, subtask, phase, log, cost, complete) continue
# to fire during migration (doc 10) so the current UI keeps working.

# Board events (gateway → UI)
EVENT_BOARD_ENTRY = "board_entry"                      # committed entry (full)
EVENT_ENTRY_REMOVED = "entry_removed"                  # {entry_id, by, reason}
EVENT_ENTRY_STATUS_CHANGED = "entry_status_changed"    # {entry_id, by, reason}
EVENT_ENTRY_REJECTED = "entry_rejected"                # {entry, actor, reason}

# Coordination events (CU → UI)
EVENT_CONSENSUS = "consensus"                          # {decider_state, open_critiques, phase, round}
EVENT_COORDINATOR_NARRATION = "coordinator_narration"   # {round, selected, rationale, source} — doc 05 §1.2

# Trace events (agent → UI)
EVENT_TRACE = "trace"                                  # trace event (doc 06)

# Turn lifecycle events (orchestrator → UI)
EVENT_TURN_START = "turn_start"                        # {turn_id, actor, node, round}
EVENT_TURN_END = "turn_end"                            # {turn_id, actor, node, round}

# File/artifact events (daemon → UI)
EVENT_FILE_ADDED = "file_added"                        # file metadata (doc 17)
EVENT_ARTIFACT_CREATED = "artifact_created"            # artifact metadata (doc 17)

# Phase 5: HITL events (doc 05 §6)
EVENT_PAUSED = "paused"                                # task paused by operator
EVENT_RESUMED = "resumed"                              # task resumed
EVENT_BUDGET = "budget"                                # {spent, ceiling, percentage} — budget gauge
EVENT_APPROVAL_REQUEST = "approval_request"            # Hermes run approval (doc 12 §5.1)


# ── Event Registry ───────────────────────────────────────────────────

# V2 event names — the new vocabulary registered by Phase 0.
V2_EVENT_NAMES: dict[str, str] = {
    EVENT_BOARD_ENTRY: "Committed entry (full) — live graph, debate list",
    EVENT_ENTRY_REMOVED: "{entry_id, by, reason} — graph fade-out / strikethrough",
    EVENT_ENTRY_STATUS_CHANGED: "{entry_id, by, reason} — graph status update",
    EVENT_ENTRY_REJECTED: "{entry, actor, reason} — rejection overlay, debug",
    EVENT_CONSENSUS: "{decider_state, open_critiques, phase, round} — convergence meter",
    EVENT_COORDINATOR_NARRATION: "{round, selected, rationale, source} — Coordinator lane (doc 05 §1.2, doc 13 §3)",
    EVENT_TRACE: "Trace event (doc 06) — trace inspector, log terminal",
    EVENT_TURN_START: "{turn_id, actor, node, round} — worker activity lane",
    EVENT_TURN_END: "{turn_id, actor, node, round} — worker activity lane",
    EVENT_FILE_ADDED: "File metadata (doc 17) — attachments rail",
    EVENT_ARTIFACT_CREATED: "Artifact metadata (doc 17) — artifact browser",
    EVENT_PAUSED: "Task paused by operator (doc 05 §6)",
    EVENT_RESUMED: "Task resumed (doc 05 §6)",
    EVENT_BUDGET: "{spent, ceiling, percentage} — budget gauge (doc 09 §5)",
    EVENT_APPROVAL_REQUEST: "Hermes run approval request (doc 12 §5.1)",
}

# Legacy events that continue to fire during migration (doc 10).
# These are NOT defined here — they're emitted by the existing
# blackboard.py and orchestrator.py.  Listed for documentation.
LEGACY_EVENT_NAMES: frozenset[str] = frozenset({
    "debate",
    "subtask",
    "phase",
    "log",
    "cost",
    "complete",
})


def all_v2_event_names() -> list[str]:
    """Return all registered v2 event names (sorted)."""
    return sorted(V2_EVENT_NAMES.keys())


def is_v2_event(name: str) -> bool:
    """Check if an event name is a registered v2 event."""
    return name in V2_EVENT_NAMES


def is_legacy_event(name: str) -> bool:
    """Check if an event name is a legacy event."""
    return name in LEGACY_EVENT_NAMES
