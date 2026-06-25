# /opt/bmas/daemon/src/core/response_parser.py
"""Robust agent response parser (doc 04 §3, extended).

Converts the raw string or dict returned by an agent turn into a list of
clean proposed-entry dicts ready for gateway.append().

Design goals
------------
* Never raises — logs warnings and returns whatever is parseable.
* Handles every LLM output pattern observed in production:
    - Structured JSON with an ``entries`` array      (happy path)
    - Single JSON object (one entry)                 (common)
    - Multiple entries bundled in one JSON blob      (planner/critic bug)
    - Prose delimited by ``---`` with JSON blocks    (markdown bundling)
    - Refs embedded in body prose, not in JSON field (most common bug)
    - Decider wrapping output in a ```json code fence (observed in task-caccf02b)
    - ``rebuttal`` mislabelled as ``finding``        (observed in task-caccf02b)
    - Plain free-text fallback                       (legacy)

Entry IDs
---------
Pass ``known_ids`` (the set of entry IDs currently on the board, e.g.
{\"e-1\", \"e-2\", \"e-3\"}) so the parser can validate ref mentions and only
promote heuristic refs that look like real board IDs.  If ``known_ids`` is
None, the parser accepts any ``e-\\d+`` pattern without validation.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.entry import DEFAULT_CONFIDENCE, role_default_type

logger = logging.getLogger("bmas.response_parser")

# ── Constants ────────────────────────────────────────────────────────

# Valid entry type vocabulary (mirrors protocol.ENTRY_TYPES without importing
# the module to keep this module dependency-free and easily unit-testable).
_VALID_TYPES: frozenset[str] = frozenset({
    "objective", "attachment", "plan", "finding",
    "critique", "rebuttal", "conflict", "directive",
    "solution", "artifact",
})

# Regex: board entry IDs (e-1, e-12, …)
_ENTRY_ID_RE = re.compile(r"\be-(\d+)\b")

# Prose ref patterns the LLM commonly uses instead of the JSON field:
#   **Refs**: [e-3, e-4]
#   refs=[e-3, e-4]
#   (refs: e-3, e-5)
#   Refs: e-3
_PROSE_REF_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\*{0,2}[Rr]efs?\*{0,2}\s*[:=]\s*\[([^\]]+)\]"),
    re.compile(r"\*{0,2}[Rr]efs?\*{0,2}\s*[:=]\s*(e-\d+(?:\s*,\s*e-\d+)*)"),
    re.compile(r"\(\s*[Rr]efs?\s*:\s*(e-\d+(?:\s*,\s*e-\d+)*)\s*\)"),
]

# Words that signal hedging/low-confidence
_HEDGING_WORDS = frozenset({
    "likely", "possibly", "possible", "unclear", "uncertain",
    "may", "might", "perhaps", "could", "speculative", "hypothetical",
    "unconfirmed", "inconclusive", "suspect",
})

# Markdown section headers that indicate a new bundled entry
_SECTION_HEADER_RE = re.compile(
    r"^#{1,3}\s+(?:Plan|Finding|Critique|Rebuttal|Conflict|Directive|Solution)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Delimiter between bundled entries in prose output
_PROSE_DELIMITER_RE = re.compile(r"\n---+\n")


# ── Public API ───────────────────────────────────────────────────────

def parse_entries(
    raw: Any,
    actor: str,
    known_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse an agent turn response into proposed board entry dicts.

    Parameters
    ----------
    raw:
        The raw agent response — may be a dict (structured JSON), a string
        (free-text or JSON-in-string), or anything else.
    actor:
        The opaque actor id (e.g. \"planner\", \"expert.valuation\").
        Used to infer default entry type and detect rebuttal promotions.
    known_ids:
        Set of board entry IDs currently in play (e.g. {\"e-1\", \"e-2\"}).
        Refs extracted from prose are validated against this set.
        Pass None to skip validation (accepts any ``e-N`` pattern).

    Returns
    -------
    list[dict]
        Each dict is a proposed entry with at minimum: type, title, body,
        refs (list), confidence (float).  Ready for gateway.append().
    """
    # Step 1: normalise raw to a list of candidate entry dicts
    candidates = _extract_candidates(raw, actor)

    if not candidates:
        logger.debug("parse_entries(%s): no candidates extracted", actor)
        return []

    # Step 2: clean and enrich each candidate
    results: list[dict[str, Any]] = []
    for raw_entry in candidates:
        try:
            entry = _clean_entry(raw_entry, actor, known_ids)
            if entry:
                results.append(entry)
        except Exception as exc:
            logger.warning("parse_entries: failed to clean entry for %s: %s", actor, exc)

    logger.debug(
        "parse_entries(%s): %d raw candidates → %d clean entries",
        actor, len(candidates), len(results),
    )
    return results


# ── Step 1: candidate extraction ─────────────────────────────────────

def _extract_candidates(raw: Any, actor: str) -> list[dict[str, Any]]:
    """Normalise the raw response to a flat list of candidate dicts."""

    # ── Dict input ───────────────────────────────────────────────────
    if isinstance(raw, dict):
        # Cleaner / decline pass-throughs (never sent to gateway as entries)
        action = raw.get("action")
        if action == "decline":
            return []
        if action in ("clean", "condense"):
            results = []
            removals = raw.get("removals", [])
            if removals:
                results.append({"_action": "clean", "removals": removals})
            if action == "condense":
                entries = raw.get("entries")
                if isinstance(entries, list) and entries:
                    results.extend(_flatten_entries(entries, actor))
            return results

        # entries_v1 array
        entries = raw.get("entries")
        if isinstance(entries, list) and entries:
            return _flatten_entries(entries, actor)

        # Single entry dict (agent returned one entry without wrapping)
        if "body" in raw or "type" in raw:
            return [raw]

        # Result field (legacy api_server format)
        result = raw.get("result", "")
        if result:
            return _extract_candidates(result, actor)

        return []

    # ── String input ─────────────────────────────────────────────────
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []

        # Try to parse JSON from the string
        json_candidates = _try_parse_json(text, actor)
        if json_candidates is not None:
            return json_candidates

        # No parseable JSON — treat as prose and split
        return _split_prose(text, actor)

    return []


def _try_parse_json(text: str, actor: str) -> list[dict[str, Any]] | None:
    """Try to extract and parse JSON from text. Returns None if no JSON found."""

    # Strategy 1: bare JSON (entire text is a JSON object or array)
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        parsed = _safe_json(stripped)
        if parsed is not None:
            return _extract_candidates(parsed, actor)

    # Strategy 2: ```json ... ``` fenced block
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    if fence_match:
        inner = fence_match.group(1).strip()
        parsed = _safe_json(inner)
        if parsed is not None:
            # Decider anti-pattern: the agent wrapped its solution in a json
            # code fence.  The parsed dict may itself contain an entries array.
            result = _extract_candidates(parsed, actor)
            if result:
                return result

    # Strategy 3: JSON object embedded mid-text
    # Find the first { ... } spanning the most content
    for m in re.finditer(r"\{", text):
        start = m.start()
        # Walk forward to find matching close brace
        depth = 0
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start: i + 1]
                    parsed = _safe_json(candidate)
                    if parsed is not None and isinstance(parsed, dict):
                        result = _extract_candidates(parsed, actor)
                        if result:
                            return result
                    break

    return None


def _split_prose(text: str, actor: str) -> list[dict[str, Any]]:
    """Split free-text response into one or more entry dicts."""
    default_type = role_default_type(actor)

    # Try splitting on ``---`` delimiters
    sections = _PROSE_DELIMITER_RE.split(text)

    if len(sections) > 1:
        # Multiple sections — try to parse each as an entry
        entries = []
        for sec in sections:
            sec = sec.strip()
            if not sec:
                continue
            # Each section may itself contain a JSON block
            json_result = _try_parse_json(sec, actor)
            if json_result:
                entries.extend(json_result)
            else:
                entries.append(_prose_to_entry(sec, default_type))
        return [e for e in entries if e]

    # Single block — try markdown section headers
    header_matches = list(_SECTION_HEADER_RE.finditer(text))
    if len(header_matches) > 1:
        # Agent bundled multiple entries under headers
        entries = []
        for i, m in enumerate(header_matches):
            start = m.start()
            end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(text)
            section = text[start:end].strip()
            json_result = _try_parse_json(section, actor)
            if json_result:
                entries.extend(json_result)
            else:
                entries.append(_prose_to_entry(section, default_type))
        return [e for e in entries if e]

    # Single entry
    return [_prose_to_entry(text, default_type)]


def _flatten_entries(entries: list[Any], actor: str) -> list[dict[str, Any]]:
    """Flatten an entries array — each element should be a dict."""
    result = []
    for item in entries:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            # Unusual: string inside entries array — treat as body
            result.append({"body": item})
    return result


def _prose_to_entry(text: str, default_type: str) -> dict[str, Any]:
    """Convert a prose block to a minimal entry dict."""
    lines = text.strip().split("\n")
    # First non-empty line becomes the title (strip markdown heading markers)
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            title = stripped[:200]
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip() or text.strip()
    return {
        "type": default_type,
        "title": title,
        "body": body,
        "refs": [],
        "confidence": None,
    }


# ── Step 2: entry cleaning ────────────────────────────────────────────

def _clean_entry(
    raw: dict[str, Any],
    actor: str,
    known_ids: set[str] | None,
) -> dict[str, Any] | None:
    """Normalise, enrich, and validate a single candidate entry dict."""

    # Pass-through for cleaner / decline actions
    if raw.get("action") in ("clean", "decline"):
        return raw
    if raw.get("_action") == "clean":
        return raw

    body = _extract_body(raw)
    if not body or not body.strip():
        logger.debug("_clean_entry: skipping entry with empty body")
        return None

    title = _extract_title(raw, body)
    entry_type = _resolve_type(raw, actor, body, title)
    refs = _resolve_refs(raw, body, title, known_ids)
    confidence = _resolve_confidence(raw, body)

    return {
        "type": entry_type,
        "title": title,
        "body": body,
        "refs": refs,
        "confidence": confidence,
        # Preserve any extra fields the agent may have set
        **{k: v for k, v in raw.items()
           if k not in ("type", "title", "body", "refs", "confidence",
                        "id", "status", "salience", "author", "author_node",
                        "created_at", "updated_at", "task_id", "round", "space")},
    }


def _extract_body(raw: dict[str, Any]) -> str:
    """Extract body text, unwrapping decider JSON-in-JSON patterns.

    When a JSON code fence or nested entries structure is found inside the body,
    we replace the body with the unwrapped text AND merge any refs found in the
    inner entry back into the raw dict so _resolve_refs can find them.
    """
    body = raw.get("body", "")

    # Decider anti-pattern: body is a JSON string containing another entries array
    if isinstance(body, str) and body.strip().startswith("{"):
        inner = _safe_json(body.strip())
        if isinstance(inner, dict):
            inner_entries = inner.get("entries", [])
            if inner_entries and isinstance(inner_entries, list):
                first = inner_entries[0]
                if isinstance(first, dict) and "body" in first:
                    logger.debug("_extract_body: unwrapped nested entries JSON from body")
                    # Merge inner refs back so _resolve_refs can find them
                    if not raw.get("refs") and first.get("refs"):
                        raw["refs"] = first["refs"]
                    return str(first["body"])
            if "body" in inner:
                return str(inner["body"])

    # Decider anti-pattern: the title is ```json and the body is a JSON fence
    if isinstance(body, str):
        fence_match = re.match(r"^```(?:json)?\s*\n([\s\S]*?)\n```\s*$", body.strip())
        if fence_match:
            inner_text = fence_match.group(1).strip()
            inner = _safe_json(inner_text)
            if isinstance(inner, dict):
                # Try to extract solution body from nested structure
                inner_entries = inner.get("entries", [])
                if inner_entries and isinstance(inner_entries, list):
                    first = inner_entries[0]
                    if isinstance(first, dict) and "body" in first:
                        logger.debug("_extract_body: unwrapped JSON-fenced body")
                        # Merge inner refs back
                        if not raw.get("refs") and first.get("refs"):
                            raw["refs"] = first["refs"]
                        return str(first["body"])
                if "body" in inner:
                    return str(inner["body"])

    return str(body) if body else ""


def _extract_title(raw: dict[str, Any], body: str) -> str:
    """Extract or synthesise a title."""
    title = raw.get("title", "")

    # Decider anti-pattern: title is literally "```json"
    if isinstance(title, str) and title.strip().startswith("```"):
        title = ""

    if not title or not str(title).strip():
        # Synthesise from first line of body
        first_line = body.split("\n", 1)[0].strip()
        # Strip markdown heading markers and type labels
        first_line = re.sub(r"^#{1,4}\s*", "", first_line)
        first_line = re.sub(r"^\*+\s*", "", first_line)
        title = first_line[:200]

    return str(title).strip()[:200]


def _resolve_type(
    raw: dict[str, Any],
    actor: str,
    body: str,
    title: str,
) -> str:
    """Resolve the entry type, including rebuttal promotion."""
    raw_type = raw.get("type", "")

    if raw_type and raw_type in _VALID_TYPES:
        # Already a valid type — but check for rebuttal promotion
        if raw_type == "finding" and _should_promote_to_rebuttal(body, title):
            logger.debug(
                "_resolve_type: promoting finding → rebuttal for %s (rebuttal signals in body/title)",
                actor,
            )
            return "rebuttal"
        return raw_type

    # Invalid or missing type — infer from actor
    inferred = role_default_type(actor)

    # Apply rebuttal promotion for experts
    if inferred == "finding" and _should_promote_to_rebuttal(body, title):
        logger.debug(
            "_resolve_type: inferred rebuttal for %s (rebuttal signals in body/title)", actor,
        )
        return "rebuttal"

    if raw_type and raw_type not in _VALID_TYPES:
        logger.warning(
            "_resolve_type: unknown type '%s' from %s, using '%s'",
            raw_type, actor, inferred,
        )

    return inferred


def _should_promote_to_rebuttal(body: str, title: str) -> bool:
    """Heuristic: should this finding be promoted to a rebuttal?

    True when the content clearly signals a response to a critique:
    - Title or body starts with "rebuttal", "addressing", "concede"
    - Body contains "I concede", "the critic is correct", "you are right"
    - Body references a critique by saying "the critique", "the critic raised"
    """
    combined = (title + " " + body[:600]).lower()
    rebuttal_signals = (
        "rebuttal",
        "i concede",
        "the critic",
        "critique raised",
        "critique regarding",
        "addressing the critique",
        "addressing critique",
        "concede the point",
        "well-taken",
        "i agree with the critique",
        "the critique is correct",
        "responding to",
    )
    return any(sig in combined for sig in rebuttal_signals)


def _resolve_refs(
    raw: dict[str, Any],
    body: str,
    title: str,
    known_ids: set[str] | None,
) -> list[str]:
    """Resolve refs: structured field first, then prose extraction fallback.

    Tracking the source of refs is important for debugging:
    - ``structured``: agent populated the JSON refs field correctly
    - ``prose_explicit``: extracted from a recognisable **Refs**: [...] pattern
    - ``prose_heuristic``: extracted from bare e-N mentions in body prefix
    """
    # ── Structured refs (JSON field) ─────────────────────────────────
    raw_refs = raw.get("refs", [])
    if isinstance(raw_refs, str):
        # Sometimes agents set refs as a string: "e-3, e-4"
        raw_refs = [r.strip() for r in raw_refs.replace("[", "").replace("]", "").split(",")]

    structured: list[str] = []
    if isinstance(raw_refs, list):
        for r in raw_refs:
            r_str = str(r).strip()
            if _is_valid_ref(r_str, known_ids):
                structured.append(r_str)

    if structured:
        return _dedupe(structured)

    # ── Prose extraction ──────────────────────────────────────────────
    search_text = (title or "") + "\n" + body

    # Strategy 1: explicit ref patterns
    explicit: list[str] = []
    for pat in _PROSE_REF_PATTERNS:
        for m in pat.finditer(search_text):
            ref_text = m.group(1)
            for ref in _ENTRY_ID_RE.findall(ref_text):
                candidate = f"e-{ref}"
                if _is_valid_ref(candidate, known_ids):
                    explicit.append(candidate)

    if explicit:
        logger.debug("_resolve_refs: extracted refs via prose_explicit: %s", explicit)
        return _dedupe(explicit)

    # Strategy 2: heuristic — bare e-N mentions in first 600 chars
    # Only apply when no explicit refs found, to avoid false positives.
    prefix = search_text[:600]
    heuristic: list[str] = []
    for ref in _ENTRY_ID_RE.findall(prefix):
        candidate = f"e-{ref}"
        if _is_valid_ref(candidate, known_ids):
            heuristic.append(candidate)

    if heuristic:
        logger.debug(
            "_resolve_refs: extracted refs via prose_heuristic: %s "
            "(known_ids validation: %s)",
            heuristic, known_ids is not None,
        )
        return _dedupe(heuristic)

    return []


def _resolve_confidence(raw: dict[str, Any], body: str) -> float:
    """Parse confidence, apply hedging heuristic if not explicitly set."""
    raw_conf = raw.get("confidence")

    if raw_conf is not None:
        try:
            val = float(raw_conf)
            if 0.0 <= val <= 1.0:
                return val
        except (ValueError, TypeError):
            pass

    # Apply hedging heuristic
    body_lower = body.lower()
    hedge_count = sum(1 for w in _HEDGING_WORDS if w in body_lower)
    if hedge_count >= 2:
        return 0.4   # Visibly uncertain
    if hedge_count == 1:
        return 0.45

    return DEFAULT_CONFIDENCE  # 0.5


# ── Helpers ──────────────────────────────────────────────────────────

def _safe_json(text: str) -> Any:
    """Parse JSON without raising."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _is_valid_ref(ref: str, known_ids: set[str] | None) -> bool:
    """Check if a ref string looks like a valid board entry ID."""
    if not _ENTRY_ID_RE.fullmatch(ref):
        return False
    if known_ids is not None:
        return ref in known_ids
    return True


def _dedupe(refs: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    seen: set[str] = set()
    result = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            result.append(r)
    return result
