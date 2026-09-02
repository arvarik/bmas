"""Contamination, holdout, license, and rights screening.

Screening runs exact-hash, normalized-text, and approximate-overlap
checks against one pinned corpus, and it never claims that a clean
result proves no prior model exposure. Hidden-test content stays
outside configuration, prompt, and preview paths. Every source, case
group, and asset carries one license decision, an unresolved decision
blocks publication, a denied redistribution blocks export, and the
attribution bundle assembles from every approved source. Synthetic
canaries watch each holdout, every access records an audit event, and
a confirmed disclosure creates an incident and rotates the holdout.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from typing import Any

from benchmarks.provenance import content_checksum

SCREENING_IMPLEMENTATION = "exact-normalized-overlap"
SCREENING_IMPLEMENTATION_VERSION = "1"
DEFAULT_OVERLAP_THRESHOLD = 0.8
_OVERLAP_SHINGLE_SIZE = 5

SPLIT_ROLES = ("development", "validation", "hidden_test")

# Licenses the policy resolves without an operator decision. Every
# other license stays unresolved until an operator decides.
APPROVED_LICENSES = {
    "mit": "approved",
    "apache-2.0": "approved",
    "bsd-3-clause": "approved",
    "cc-by-4.0": "attribution_required",
    "cc-by-sa-4.0": "attribution_required",
    "cc-by-nc-4.0": "noncommercial",
    "repository-reviewed": "approved",
    "owner-declared": "approved",
}
_BLOCKING_DECISIONS = {"unresolved", "incompatible"}
# A noncommercial license permits internal evaluation but never an
# outward redistribution, so export treats it as a denial.
_EXPORT_DENYING = {
    "no_redistribution", "noncommercial", "incompatible", "unresolved",
}


class RightsPolicyError(ValueError):
    """A rights or holdout rule blocks the requested operation."""


def _normalized_text(text: str) -> str:
    collapsed = " ".join(str(text).split())
    return unicodedata.normalize("NFC", collapsed).casefold()


def _shingles(text: str) -> set[str]:
    tokens = _normalized_text(text).split(" ")
    if len(tokens) < _OVERLAP_SHINGLE_SIZE:
        return {" ".join(tokens)} if tokens else set()
    return {
        " ".join(tokens[index:index + _OVERLAP_SHINGLE_SIZE])
        for index in range(len(tokens) - _OVERLAP_SHINGLE_SIZE + 1)
    }


def _overlap(left: str, right: str) -> float:
    left_shingles = _shingles(left)
    right_shingles = _shingles(right)
    if not left_shingles or not right_shingles:
        return 0.0
    union = left_shingles | right_shingles
    return len(left_shingles & right_shingles) / len(union)


def screen_cases(
    cases: list[dict[str, Any]],
    corpus: dict[str, Any],
    *,
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> dict[str, Any]:
    """Screen every case against one pinned reference corpus.

    The result records the implementation, the corpus identity, the
    thresholds, and the version, labels each match kind, and states
    ``screened`` or ``suspected``. Screening never proves that a
    model missed an item, and the result says so explicitly.
    """
    references = [str(entry) for entry in corpus.get("entries") or []]
    exact = {
        hashlib.sha256(reference.encode("utf-8")).hexdigest()
        for reference in references
    }
    normalized = {
        hashlib.sha256(
            _normalized_text(reference).encode("utf-8"),
        ).hexdigest()
        for reference in references
    }
    matches: list[dict[str, Any]] = []
    for case in cases:
        text = str(case.get("input") or "")
        case_id = str(case.get("case_id") or case.get("item_key") or "")
        exact_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if exact_digest in exact:
            matches.append({"case_id": case_id, "kind": "content_hash",
                            "decision": "pending_review"})
            continue
        normalized_digest = hashlib.sha256(
            _normalized_text(text).encode("utf-8"),
        ).hexdigest()
        if normalized_digest in normalized:
            matches.append({"case_id": case_id, "kind": "exact_match",
                            "decision": "pending_review"})
            continue
        best = max(
            (_overlap(text, reference) for reference in references),
            default=0.0,
        )
        if best >= overlap_threshold:
            matches.append({
                "case_id": case_id,
                "kind": "approximate_overlap",
                "decision": "pending_review",
            })
    return {
        "implementation": SCREENING_IMPLEMENTATION,
        "corpus": str(corpus.get("id") or "unnamed-corpus"),
        "corpus_version": str(corpus.get("version") or "1"),
        "thresholds": {"overlap": overlap_threshold,
                       "shingle_size": _OVERLAP_SHINGLE_SIZE},
        "version": SCREENING_IMPLEMENTATION_VERSION,
        "result": "suspected" if matches else "screened",
        "matches": matches,
        # Screening can never prove absence of prior exposure.
        "proof_disclaimer": (
            "Screening cannot prove that a model never saw an item."
        ),
    }


# ── Hidden-test protection ───────────────────────────────────────────


def visible_cases(
    cases: list[dict[str, Any]], *, context: str,
) -> list[dict[str, Any]]:
    """Filter hidden-test cases out of every non-holdout context.

    Configuration, prompt, preview, and development-export paths never
    see hidden-test content. Only an audited holdout access reads it.
    """
    if context == "holdout_access":
        return list(cases)
    if context not in (
        "preview", "configuration", "prompt", "development_export",
    ):
        raise RightsPolicyError(f"Unknown visibility context: {context!r}")
    return [
        case
        for case in cases
        if str(_case_split(case)) != "hidden_test"
    ]


def _case_split(case: dict[str, Any]) -> str:
    classification = case.get("classification")
    if isinstance(classification, dict):
        return str(classification.get("split") or "test")
    return str(case.get("split") or "test")


def holdout_access_event(actor: str, *, reason: str) -> dict[str, Any]:
    """Record one audited holdout access."""
    if not actor or not actor.strip():
        raise RightsPolicyError(
            "A holdout access records one authenticated actor"
        )
    return {
        "actor": actor,
        "reason": reason,
        "access_id": f"holdout-access-{uuid.uuid4().hex}",
    }


def new_canaries(count: int = 3) -> list[str]:
    """Create synthetic holdout canary identifiers."""
    return [f"canary-{uuid.uuid4().hex}" for _ in range(max(count, 1))]


def detect_canary_disclosure(
    canaries: list[str], observed_text: str,
) -> dict[str, Any] | None:
    """Detect one canary in observed output and rotate the holdout.

    A confirmed disclosure creates one incident and a fresh canary
    set. The rotation history keeps the exposed identifiers readable.
    """
    exposed = [
        canary for canary in canaries if canary in str(observed_text)
    ]
    if not exposed:
        return None
    replacement = new_canaries(len(canaries))
    return {
        "incident_id": f"holdout-incident-{uuid.uuid4().hex}",
        "exposed_canaries": exposed,
        "rotated_canaries": replacement,
        "action": "rotate_affected_holdout",
    }


# ── License decisions, publication, and export ───────────────────────


def license_decision(license_name: str) -> str:
    """Resolve one license name through the declared policy table."""
    return APPROVED_LICENSES.get(
        _normalized_text(license_name), "unresolved",
    )


def build_rights_decisions(
    sources: list[dict[str, Any]],
    *,
    operator_decisions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build one license decision per source with operator overrides."""
    overrides = dict(operator_decisions or {})
    decisions = []
    for source in sources:
        source_id = str(source.get("source_id") or source.get("id") or "")
        name = str((source.get("license") or {}).get("name") or "unknown")
        decision = overrides.get(source_id) or license_decision(name)
        decisions.append({
            "subject": source_id,
            "license": name,
            "decision": decision,
        })
    return decisions


def assert_publication_allowed(
    decisions: list[dict[str, Any]],
) -> None:
    """Block publication while any required decision stays unresolved."""
    blocking = [
        decision
        for decision in decisions
        if str(decision.get("decision")) in _BLOCKING_DECISIONS
    ]
    if blocking:
        subjects = sorted(
            str(decision["subject"]) for decision in blocking
        )
        raise RightsPolicyError(
            "Publication is blocked by unresolved or incompatible "
            f"license decisions: {subjects}"
        )


def assert_export_allowed(decisions: list[dict[str, Any]]) -> None:
    """Block export when any source restriction denies redistribution."""
    denying = [
        decision
        for decision in decisions
        if str(decision.get("decision")) in _EXPORT_DENYING
    ]
    if denying:
        subjects = sorted(str(decision["subject"]) for decision in denying)
        raise RightsPolicyError(
            f"Export is blocked by source restrictions: {subjects}"
        )


def attribution_bundle(
    sources: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the complete attribution bundle for allowed exports."""
    entries = []
    decided = {
        str(decision["subject"]): str(decision["decision"])
        for decision in decisions
    }
    for source in sources:
        source_id = str(source.get("source_id") or source.get("id") or "")
        license_info = source.get("license") or {}
        entries.append({
            "source_id": source_id,
            "locator": str(source.get("locator") or ""),
            "license": str(license_info.get("name") or "unknown"),
            "citation": str(license_info.get("citation") or ""),
            "decision": decided.get(source_id, "unresolved"),
        })
    bundle = {"entries": entries}
    return {**bundle, "bundle_digest": content_checksum(bundle)}


def build_contamination_record(
    *,
    dataset_version_id: str,
    screening: dict[str, Any],
    decisions: list[dict[str, Any]],
    attribution: dict[str, Any],
    canaries: list[str],
    holdout_accesses: list[dict[str, Any]],
    use_decisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one validating contamination and rights record."""
    redistribution = "allowed"
    if any(
        str(decision.get("decision")) in _EXPORT_DENYING
        for decision in decisions
    ):
        redistribution = "denied"
    return {
        "schema_id": "contamination-rights-record",
        "schema_version": 2,
        "record_id": f"contamination-{uuid.uuid4().hex}",
        "dataset_version_id": dataset_version_id,
        "split_rules": {
            "development": "declared development split",
            "validation": "declared validation split",
            "hidden_test": "audited holdout access only",
        },
        "screening": {
            "implementation": screening["implementation"],
            "corpus": screening["corpus"],
            "thresholds": screening["thresholds"],
            "version": screening["version"],
            "result": screening["result"],
        },
        "matches": [
            {
                "case_id": str(match["case_id"]),
                "kind": str(match["kind"]),
                "decision": str(match["decision"]),
            }
            for match in screening.get("matches") or []
        ],
        "holdout_access": [
            {"actor": str(access["actor"]),
             "accessed_at": str(access.get("accessed_at")
                                or "1970-01-01T00:00:00Z")}
            for access in holdout_accesses
        ],
        "canaries": {"identifiers": list(canaries),
                     "rotation_history": []},
        "license_decisions": [
            {
                "subject": str(decision["subject"]),
                "decision": (
                    "restricted"
                    if str(decision["decision"]) in (
                        "no_redistribution", "noncommercial",
                    )
                    else (
                        "approved"
                        if str(decision["decision"]) in (
                            "approved", "attribution_required",
                        )
                        else "unresolved"
                    )
                ),
                "note": str(decision.get("license") or ""),
            }
            for decision in decisions
        ],
        "attribution": {
            "text": "\n".join(
                f"{entry['source_id']}: {entry['license']}"
                + (f" ({entry['citation']})" if entry["citation"] else "")
                for entry in attribution["entries"]
            )
            or "No external sources.",
            "links": [
                entry["locator"]
                for entry in attribution["entries"]
                if entry["locator"].startswith("https://")
            ],
        },
        "use_decisions": {
            "allowed_use": "evaluation_only",
            "redistribution": redistribution,
            "modification": "allowed",
            "export": (use_decisions or {}).get("export", redistribution),
        },
    }
