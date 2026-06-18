# /opt/bmas/daemon/tests/test_response_parser.py
"""Unit tests for core.response_parser.

All failure cases observed in task-caccf02b and general LLM output patterns:

1.  Structured entries_v1 JSON (happy path)
2.  Bundled planner response → split into N plan entries
3.  Bundled critic response → split into N critique entries, each with refs
4.  Prose ref extraction: **Refs**: [e-3, e-4]
5.  Prose ref extraction: refs=[e-3, e-4]
6.  Prose ref extraction: bare e-N mention in prefix (heuristic)
7.  Decider JSON code-fence wrapping → clean body + refs
8.  Decider nested entries-in-entries → unwrapped body
9.  Rebuttal promotion from finding (title signal)
10. Rebuttal promotion from finding (body signal: "I concede")
11. Rebuttal NOT promoted when no signals present
12. Confidence: explicit float preserved
13. Confidence: string parsed
14. Confidence: hedging words reduce default
15. Empty response → []
16. Decline action passthrough
17. Cleaner action passthrough
18. Known-ids validation: unknown refs dropped
19. Unknown-ids=None: any e-N ref accepted
20. Type normalisation: invalid type → actor default
21. Single JSON object (no entries array) treated as one entry
22. Prose with --- delimiter splits into multiple entries
23. Prose with markdown section headers splits into multiple entries
24. Title synthesised from body first line when missing / empty
25. JSON embedded mid-text
"""
from __future__ import annotations

import json

from core.response_parser import parse_entries

# ── Helpers ──────────────────────────────────────────────────────────

KNOWN = {"e-1", "e-2", "e-3", "e-4", "e-5"}


def _one(raw, actor="expert.test", known=None):
    """Parse and assert exactly one entry returned."""
    results = parse_entries(raw, actor, known_ids=known)
    assert len(results) == 1, f"Expected 1 entry, got {len(results)}: {results}"
    return results[0]


# ── 1. Structured entries_v1 (happy path) ────────────────────────────

def test_structured_entries_v1():
    raw = {
        "entries": [
            {
                "type": "finding",
                "title": "IAM failure",
                "body": "The support rep had excess permissions.",
                "refs": ["e-1", "e-2"],
                "confidence": 0.85,
            }
        ]
    }
    entry = _one(raw, known=KNOWN)
    assert entry["type"] == "finding"
    assert entry["refs"] == ["e-1", "e-2"]
    assert entry["confidence"] == 0.85


# ── 2. Bundled planner response ──────────────────────────────────────

def test_planner_bundled_entries():
    raw = {
        "entries": [
            {
                "type": "plan",
                "title": "Analyse IAM",
                "body": "Look at privilege scope.",
                "refs": ["e-1"],
                "confidence": 0.9,
            },
            {
                "type": "plan",
                "title": "Analyse billing integration",
                "body": "Audit third-party access.",
                "refs": ["e-1"],
                "confidence": 0.8,
            },
        ]
    }
    results = parse_entries(raw, "planner", known_ids=KNOWN)
    assert len(results) == 2
    assert all(e["type"] == "plan" for e in results)
    assert results[0]["title"] == "Analyse IAM"
    assert results[1]["title"] == "Analyse billing integration"


# ── 3. Bundled critic with prose refs ────────────────────────────────

def test_critic_bundled_entries_with_prose_refs():
    # Critic returned a single dict with entries array, but the entries
    # have their refs embedded in body prose — as observed in task-caccf02b.
    raw = {
        "entries": [
            {
                "type": "critique",
                "title": "Data layer blind spot",
                "body": "**Refs**: [e-3, e-4]\nBoth findings miss the data-layer isolation issue.",
                "refs": [],        # ← empty structured field (the bug)
                "confidence": 0.95,
            },
            {
                "type": "critique",
                "title": "Admin interface security",
                "body": "**Refs**: [e-3]\nThe support tool itself is over-privileged.",
                "refs": [],
                "confidence": 0.9,
            },
        ]
    }
    results = parse_entries(raw, "critic", known_ids=KNOWN)
    assert len(results) == 2
    # Refs should be extracted from prose
    assert "e-3" in results[0]["refs"]
    assert "e-4" in results[0]["refs"]
    assert "e-3" in results[1]["refs"]


# ── 4. Prose ref extraction: **Refs**: [e-3, e-4] ────────────────────

def test_prose_ref_bold_bracket():
    raw = {
        "type": "critique",
        "title": "Missing audit trail",
        "body": "**Refs**: [e-2, e-3]\nThe plan ignores configuration change detection.",
        "refs": [],
        "confidence": 1.0,
    }
    entry = _one(raw, actor="critic", known=KNOWN)
    assert "e-2" in entry["refs"]
    assert "e-3" in entry["refs"]


def test_prose_ref_equals_bracket():
    raw = {
        "type": "critique",
        "title": "Alert fatigue",
        "body": "refs=[e-4]\nThe on-call staffing analysis is insufficient.",
        "refs": [],
        "confidence": 0.8,
    }
    entry = _one(raw, actor="critic", known=KNOWN)
    assert "e-4" in entry["refs"]


def test_prose_ref_paren():
    raw = {
        "type": "finding",
        "title": "IAM gaps",
        "body": "(refs: e-1, e-2) The identity perimeter was breached.",
        "refs": [],
        "confidence": 0.7,
    }
    entry = _one(raw, actor="expert.analyst", known=KNOWN)
    assert "e-1" in entry["refs"]
    assert "e-2" in entry["refs"]


# ── 6. Heuristic: bare e-N mention in body prefix ────────────────────

def test_prose_ref_heuristic_bare_mention():
    raw = {
        "type": "rebuttal",
        "title": "Addressing e-5",
        "body": "The critique in e-5 raised valid points about admin tooling. I concede the point regarding step-up auth.",
        "refs": [],
        "confidence": 0.75,
    }
    entry = _one(raw, actor="expert.sec", known={"e-1", "e-5"})
    assert "e-5" in entry["refs"]


# ── 7. Decider JSON code-fence wrapping ──────────────────────────────

def test_decider_json_code_fence_unwrap():
    # The decider returned ```json { "entries": [...] } ``` as the body.
    inner_body = "This breach was a perfect storm cascade."
    inner = {
        "entries": [
            {
                "id": "solution-1",
                "type": "solution",
                "title": "Hardening Strategy",
                "body": inner_body,
                "refs": ["e-3", "e-4", "e-5"],
                "confidence": 0.98,
            }
        ]
    }
    raw_response = f"```json\n{json.dumps(inner)}\n```"
    entry = _one(raw_response, actor="decider", known={"e-3", "e-4", "e-5"})
    assert entry["type"] == "solution"
    assert entry["body"] == inner_body
    assert "e-3" in entry["refs"]


def test_decider_json_code_fence_in_body_field():
    # The decider put the JSON fence inside the body field of a dict response.
    inner_body = "Comprehensive post-mortem and hardening strategy."
    inner = {
        "entries": [
            {
                "type": "solution",
                "title": "Multi-vector Hardening",
                "body": inner_body,
                "refs": ["e-3", "e-4"],
                "confidence": 0.95,
            }
        ]
    }
    raw = {
        "type": "solution",
        "title": "```json",   # ← the broken title we observed
        "body": f"```json\n{json.dumps(inner)}\n```",
        "refs": [],
        "confidence": 0.5,
    }
    entry = _one(raw, actor="decider", known={"e-3", "e-4"})
    assert entry["body"] == inner_body
    assert entry["title"] != "```json"
    assert "e-3" in entry["refs"]


# ── 8. Decider nested entries-in-entries ─────────────────────────────

def test_decider_nested_entries_in_body():
    # The body itself is a JSON string containing an entries array.
    inner_body = "The root cause was a privilege escalation via feature flags."
    inner_json = json.dumps({
        "entries": [
            {
                "type": "solution",
                "body": inner_body,
                "refs": ["e-2"],
            }
        ]
    })
    raw = {
        "type": "solution",
        "title": "Solution",
        "body": inner_json,
        "refs": [],
        "confidence": 0.9,
    }
    entry = _one(raw, actor="decider", known={"e-2"})
    assert entry["body"] == inner_body


# ── 9. Rebuttal promotion: title signal ──────────────────────────────

def test_rebuttal_promotion_title_signal():
    raw = {
        "type": "finding",
        "title": "rebuttal: Addressing the data layer critique",
        "body": "I concede the points raised. The billing integration needs row-level security.",
        "refs": ["e-5"],
        "confidence": 0.75,
    }
    entry = _one(raw, actor="expert.domain_analyst", known=KNOWN)
    assert entry["type"] == "rebuttal"


# ── 10. Rebuttal promotion: body signal ──────────────────────────────

def test_rebuttal_promotion_body_signal_concede():
    raw = {
        "type": "finding",
        "title": "Administrative Interface Security",
        "body": "I concede the point regarding admin tool over-privilege. Step-up auth is necessary.",
        "refs": ["e-5"],
        "confidence": 0.8,
    }
    entry = _one(raw, actor="expert.sec", known=KNOWN)
    assert entry["type"] == "rebuttal"


def test_rebuttal_promotion_body_signal_critique():
    raw = {
        "type": "finding",
        "title": "Addressing Administrative Interface Security and Decoy Dynamics",
        "body": "The critique regarding the over-privileged Internal Administrative Tooling is well-taken.",
        "refs": ["e-5"],
        "confidence": 0.8,
    }
    entry = _one(raw, actor="expert.systems_thinker", known=KNOWN)
    assert entry["type"] == "rebuttal"


# ── 11. Rebuttal NOT promoted (no signals) ───────────────────────────

def test_rebuttal_not_promoted_when_no_signals():
    raw = {
        "type": "finding",
        "title": "Cross-Domain Identity Contagion",
        "body": "The incident reveals a structural vulnerability in Security Domain Isolation.",
        "refs": ["e-1"],
        "confidence": 0.8,
    }
    entry = _one(raw, actor="expert.systems_thinker", known=KNOWN)
    assert entry["type"] == "finding"


# ── 12–14. Confidence resolution ─────────────────────────────────────

def test_confidence_explicit_float():
    raw = {"type": "finding", "body": "Clear analysis.", "refs": [], "confidence": 0.92}
    entry = _one(raw, actor="expert.test", known=KNOWN)
    assert entry["confidence"] == 0.92


def test_confidence_string_parsed():
    raw = {"type": "finding", "body": "Clear analysis.", "refs": [], "confidence": "0.92"}
    entry = _one(raw, actor="expert.test", known=KNOWN)
    assert entry["confidence"] == 0.92


def test_confidence_hedging_words_reduce_default():
    body = "This is likely a possible misconfiguration. It might be unclear without more data."
    raw = {"type": "finding", "body": body, "refs": []}
    entry = _one(raw, actor="expert.test", known=KNOWN)
    # Multiple hedging words → below default 0.5
    assert entry["confidence"] < 0.5


def test_confidence_single_hedging_word():
    body = "The attacker possibly exploited a secondary flaw in the billing integration."
    raw = {"type": "finding", "body": body, "refs": []}
    entry = _one(raw, actor="expert.test", known=KNOWN)
    assert entry["confidence"] < 0.5


# ── 15. Empty response ────────────────────────────────────────────────

def test_empty_string_returns_empty():
    assert parse_entries("", "expert.test") == []


def test_empty_dict_returns_empty():
    assert parse_entries({}, "expert.test") == []


def test_none_returns_empty():
    assert parse_entries(None, "expert.test") == []


# ── 16. Decline action ───────────────────────────────────────────────

def test_decline_action_passthrough():
    raw = {"action": "decline"}
    results = parse_entries(raw, "decider")
    assert results == []


# ── 17. Cleaner action ───────────────────────────────────────────────

def test_cleaner_action_passthrough():
    raw = {
        "action": "clean",
        "removals": [
            {"entry_id": "e-3", "reason": "Duplicates e-7"},
        ],
    }
    results = parse_entries(raw, "cleaner")
    assert len(results) == 1
    assert results[0]["_action"] == "clean"
    assert results[0]["removals"][0]["entry_id"] == "e-3"


# ── 18. Known-ids validation ─────────────────────────────────────────

def test_known_ids_validation_drops_unknown_refs():
    # e-99 is not in known_ids — should be dropped
    raw = {
        "type": "critique",
        "body": "**Refs**: [e-3, e-99]\nThe analysis misses the data layer.",
        "refs": [],
        "confidence": 0.9,
    }
    entry = _one(raw, actor="critic", known={"e-1", "e-3"})
    assert "e-3" in entry["refs"]
    assert "e-99" not in entry["refs"]


def test_known_ids_none_accepts_any_e_n():
    # With known_ids=None, any e-N is accepted
    raw = {
        "type": "critique",
        "body": "**Refs**: [e-42, e-99]\nSome critique.",
        "refs": [],
        "confidence": 0.9,
    }
    entry = _one(raw, actor="critic", known=None)
    assert "e-42" in entry["refs"]
    assert "e-99" in entry["refs"]


# ── 20. Type normalisation ───────────────────────────────────────────

def test_invalid_type_falls_back_to_actor_default():
    raw = {
        "type": "totally_invalid_type",
        "body": "Some content.",
        "refs": [],
    }
    entry = _one(raw, actor="planner", known=KNOWN)
    assert entry["type"] == "plan"   # planner's default


def test_missing_type_inferred_from_expert_actor():
    raw = {"body": "Some content.", "refs": []}
    entry = _one(raw, actor="expert.valuation", known=KNOWN)
    assert entry["type"] == "finding"


def test_missing_type_inferred_from_critic_actor():
    raw = {"body": "**Refs**: [e-1]\nThis finding has a flaw.", "refs": []}
    entry = _one(raw, actor="critic", known=KNOWN)
    assert entry["type"] == "critique"


# ── 21. Single JSON object treated as one entry ──────────────────────

def test_single_json_object_without_entries_array():
    raw = {
        "type": "finding",
        "title": "IAM failure",
        "body": "The support rep had excess permissions.",
        "refs": ["e-1"],
        "confidence": 0.8,
    }
    entry = _one(raw, actor="expert.iam", known=KNOWN)
    assert entry["type"] == "finding"
    assert entry["refs"] == ["e-1"]


# ── 22. Prose with --- delimiter ─────────────────────────────────────

def test_prose_delimiter_splits_entries():
    text = """## Plan: IAM Analysis
Investigate credential scope and privilege escalation.
**Refs**: [e-1]

---

## Plan: Billing Integration Audit
Analyse third-party data access patterns.
**Refs**: [e-1]
"""
    results = parse_entries(text, "planner", known_ids={"e-1"})
    assert len(results) == 2
    # Both should reference e-1
    for r in results:
        assert "e-1" in r["refs"]


# ── 23. Markdown section headers split ───────────────────────────────

def test_markdown_section_headers_split_entries():
    text = """## Plan: Technical Root Cause Analysis
Identify how feature flags were misused.

## Plan: Operational Resilience Analysis
Investigate alert fatigue during the DB outage.

## Plan: Third-Party Audit
Analyse billing integration trust model.
"""
    results = parse_entries(text, "planner", known_ids=None)
    assert len(results) == 3


# ── 24. Title synthesised from body ──────────────────────────────────

def test_title_synthesised_from_body_first_line():
    raw = {
        "type": "finding",
        "title": "",
        "body": "Critical Identity Perimeter Breach\nThe breach exploited IAM gaps.",
        "refs": [],
        "confidence": 0.8,
    }
    entry = _one(raw, actor="expert.iam", known=KNOWN)
    assert "Identity" in entry["title"] or "Critical" in entry["title"]


def test_title_stripped_of_json_fence():
    raw = {
        "type": "solution",
        "title": "```json",
        "body": "The solution is comprehensive.",
        "refs": [],
        "confidence": 0.9,
    }
    entry = _one(raw, actor="decider", known=KNOWN)
    assert entry["title"] != "```json"
    assert "solution" in entry["title"].lower() or "comprehensive" in entry["title"].lower()


# ── 25. JSON embedded mid-text ───────────────────────────────────────

def test_json_embedded_mid_text():
    text = (
        "Here is my analysis:\n\n"
        '{"type": "finding", "title": "IAM failure", '
        '"body": "The support rep had excess permissions.", '
        '"refs": ["e-1"], "confidence": 0.8}\n\n'
        "Please consider this carefully."
    )
    results = parse_entries(text, "expert.iam", known_ids={"e-1"})
    assert len(results) == 1
    assert results[0]["type"] == "finding"
    assert results[0]["refs"] == ["e-1"]


# ── Extra: string input (free-text) ──────────────────────────────────

def test_plain_string_fallback():
    text = "The IAM configuration had critical flaws that allowed privilege escalation."
    results = parse_entries(text, "expert.iam", known_ids=None)
    assert len(results) == 1
    assert results[0]["type"] == "finding"
    assert len(results[0]["body"]) > 0


def test_result_field_in_dict():
    """Legacy api_server format: dict with 'result' string field."""
    raw = {
        "result": '{"type": "finding", "body": "The breach exploited IAM gaps.", "refs": ["e-1"], "confidence": 0.8}',
    }
    results = parse_entries(raw, "expert.iam", known_ids={"e-1"})
    assert len(results) == 1
    assert results[0]["type"] == "finding"


def test_string_json_response():
    """Agent returned raw JSON as a string."""
    raw = '{"entries": [{"type": "plan", "title": "Analyse IAM", "body": "Look at privilege scope.", "refs": ["e-1"], "confidence": 0.9}]}'
    results = parse_entries(raw, "planner", known_ids={"e-1"})
    assert len(results) == 1
    assert results[0]["type"] == "plan"
    assert results[0]["refs"] == ["e-1"]
