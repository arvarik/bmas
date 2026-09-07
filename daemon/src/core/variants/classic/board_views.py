"""The board view policy of the Classic runtime.

The board view policy selects the entries one agent sees: the bounded,
role-specific view an activation receives, and the compact text view
the control unit reads. Both functions read the board and the token
budget from their arguments and touch no storage.
"""
from __future__ import annotations

import json
from typing import Any

from core.entry import BoardEntry, entry_to_dict

# The board entry types every view pins to the front.
PINNED_TYPES = frozenset({"objective", "directive", "ledger"})

# The entry types each constant role reads first.
RELEVANT_TYPES: dict[str, set[str]] = {
    "planner": {"objective", "directive", "ledger", "plan", "finding", "critique", "conflict"},
    "critic": {"objective", "directive", "ledger", "plan", "finding", "solution", "conflict"},
    "decider": {"objective", "directive", "ledger", "plan", "finding", "critique", "conflict", "solution"},
    "conflict_resolver": {"objective", "directive", "ledger", "finding", "critique", "conflict", "solution"},
}

CU_VIEW_TOKEN_CAP = 4000


class BoardViewPolicy:
    """Select entries for one agent context."""

    @staticmethod
    def serialize_board(
        board: dict[str, BoardEntry] | dict[str, Any],
        actor: str | None,
        *,
        view_budget_tokens: int,
    ) -> dict[str, Any]:
        """Build a bounded role-specific view over the classic board."""
        if not board:
            return {
                "mode": "bounded",
                "entries": [],
                "omitted_count": 0,
                "estimated_tokens": 0,
            }

        entries: list[dict[str, Any]] = []
        if isinstance(board, dict):
            for entry in board.values():
                if isinstance(entry, BoardEntry):
                    if entry.status != "removed":
                        entries.append(entry_to_dict(entry))
                elif (
                    isinstance(entry, dict)
                    and entry.get("status") != "removed"
                ):
                    entries.append(dict(entry))

        base_role = (actor or "").split(".", 1)[0]
        preferred = RELEVANT_TYPES.get(base_role)

        pinned_types = PINNED_TYPES
        pinned = [entry for entry in entries if entry.get("type") in pinned_types]
        candidates = [entry for entry in entries if entry not in pinned]
        referenced_ids = {
            str(ref)
            for entry in entries
            if entry.get("status", "open") == "open"
            for ref in entry.get("refs", [])
        }

        def _priority(entry: dict[str, Any]) -> tuple:
            entry_type = str(entry.get("type", ""))
            role_relevant = 1 if preferred is None or entry_type in preferred else 0
            referenced = 1 if str(entry.get("id", "")) in referenced_ids else 0
            open_status = 1 if entry.get("status", "open") == "open" else 0
            return (
                open_status,
                referenced,
                role_relevant,
                float(entry.get("salience", 0.0) or 0.0),
                float(entry.get("confidence", 0.0) or 0.0),
                int(entry.get("round", 0) or 0),
                str(entry.get("id", "")),
            )

        candidates.sort(key=_priority, reverse=True)
        pinned.sort(key=lambda entry: (
            entry.get("type") != "directive",
            int(entry.get("round", 0) or 0),
            str(entry.get("id", "")),
        ))

        selected: list[dict[str, Any]] = []
        used_tokens = 0
        budget = view_budget_tokens
        index_share = 0.40 if base_role == "decider" else 0.20
        index_budget = max(64, int(budget * index_share))
        entry_budget = max(1, budget - index_budget)
        for entry in [*pinned, *candidates]:
            remaining = entry_budget - used_tokens
            if remaining <= 32:
                break
            item = dict(entry)
            body = str(item.get("body", ""))
            overhead_chars = len(json.dumps({**item, "body": ""}, default=str))
            overhead_tokens = max(1, overhead_chars // 4)
            if overhead_tokens >= remaining:
                if item.get("type") not in pinned_types:
                    continue
                item = {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "title": str(item.get("title") or "")[:200],
                    "body": "",
                    "status": item.get("status", "open"),
                    "context_truncated": True,
                }
                item_tokens = max(
                    1, (len(json.dumps(item, default=str)) + 3) // 4,
                )
                if item_tokens > remaining:
                    continue
            else:
                body_chars = max(0, (remaining - overhead_tokens) * 4)
                if len(body) > body_chars:
                    item["body"] = body[:body_chars]
                    item["context_truncated"] = True
                item_tokens = overhead_tokens + max(
                    1, (len(str(item.get("body", ""))) + 3) // 4,
                )
                if item_tokens > remaining:
                    continue
            selected.append(item)
            used_tokens += item_tokens

        selected_ids = {str(entry.get("id", "")) for entry in selected}
        omitted_ids = [
            str(entry.get("id", ""))
            for entry in entries
            if str(entry.get("id", "")) not in selected_ids
        ]
        omitted = [
            entry
            for entry in entries
            if str(entry.get("id", "")) not in selected_ids
        ]
        omitted.sort(key=_priority, reverse=True)
        omitted_index: list[dict[str, Any]] = []
        index_tokens = 0
        excerpt_chars = 160 if base_role == "decider" else 80
        available_index_tokens = min(index_budget, budget - used_tokens)
        for entry in omitted:
            compact = {
                "id": entry.get("id"),
                "type": entry.get("type"),
                "title": str(entry.get("title") or "")[:120],
                "author": entry.get("author"),
                "round": int(entry.get("round", 0) or 0),
                "status": entry.get("status", "open"),
                "refs": list(entry.get("refs") or [])[:8],
                "salience": round(float(entry.get("salience", 0.0) or 0.0), 3),
                "body_excerpt": str(entry.get("body") or "")[:excerpt_chars],
            }
            item_tokens = max(
                1, (len(json.dumps(compact, default=str)) + 3) // 4,
            )
            remaining = available_index_tokens - index_tokens
            while item_tokens > remaining and compact["body_excerpt"]:
                compact["body_excerpt"] = compact["body_excerpt"][:
                    len(compact["body_excerpt"]) // 2
                ]
                item_tokens = max(
                    1, (len(json.dumps(compact, default=str)) + 3) // 4,
                )
            while item_tokens > remaining and compact["refs"]:
                compact["refs"] = compact["refs"][:-1]
                item_tokens = max(
                    1, (len(json.dumps(compact, default=str)) + 3) // 4,
                )
            if item_tokens > remaining:
                continue
            omitted_index.append(compact)
            index_tokens += item_tokens

        return {
            "mode": "bounded",
            "entries": selected,
            "omitted_count": len(omitted_ids),
            "omitted_ids": [item["id"] for item in omitted_index],
            "omitted_index": omitted_index,
            "omitted_index_count": len(omitted_index),
            "omitted_index_truncated": len(omitted_index) < len(omitted),
            "estimated_tokens": used_tokens + index_tokens,
            "entry_estimated_tokens": used_tokens,
            "index_estimated_tokens": index_tokens,
            "index_token_budget": index_budget,
            "token_budget": budget,
        }

    @staticmethod
    def serialize_for_cu(
        snapshot: dict[str, BoardEntry],
        *,
        view_budget_tokens: int,
    ) -> str:
        """Serialize board to a compact text format for the CU prompt."""
        if not snapshot:
            return "(empty board)"

        lines = []
        used_tokens = 0
        cu_budget = min(CU_VIEW_TOKEN_CAP, view_budget_tokens)
        for entry in sorted(
            snapshot.values(),
            key=lambda e: (
                e.type in ("objective", "directive", "ledger"),
                e.status == "open",
                e.salience,
                e.round,
                e.id,
            ),
            reverse=True,
        ):
            if entry.status == "removed":
                continue
            refs_str = f" refs=[{','.join(entry.refs)}]" if entry.refs else ""
            conf_str = f" conf={entry.confidence:.1f}" if entry.confidence else ""
            summary = (
                entry.body[:240]
                if entry.type == "directive"
                else entry.title or entry.body[:240]
            )
            line = (
                f"[{entry.id}] ({entry.type}) by {entry.author} "
                f"R{entry.round}{refs_str}{conf_str}: "
                f"{summary}"
            )
            line_tokens = max(1, len(line) // 4)
            if used_tokens + line_tokens > cu_budget:
                continue
            lines.append(line)
            used_tokens += line_tokens
        return "\n".join(lines)
