"""Contamination, holdout, license, and rights screening tests.

Screening fixtures cover exact duplicates, normalized duplicates,
paraphrases, and unrelated controls. The suite asserts the recorded
corpus, thresholds, implementation, and version, the explicit
no-proof disclaimer, hidden-test exclusion from every non-holdout
context, canary rotation on a confirmed disclosure, license decisions
that block publication and export, and the complete attribution
bundle on allowed exports.
"""

from __future__ import annotations

import pytest
from test_evaluation_contracts import valid_benchmark_source

from benchmarks.evaluation_contracts import validate_record
from benchmarks.rights_screening import (
    RightsPolicyError,
    assert_export_allowed,
    assert_publication_allowed,
    attribution_bundle,
    build_contamination_record,
    build_rights_decisions,
    detect_canary_disclosure,
    holdout_access_event,
    license_decision,
    new_canaries,
    screen_cases,
    visible_cases,
)

REFERENCE = (
    "The quick brown fox jumps over the lazy dog while the calm cat "
    "watches from the tall green fence and the small bird sings a "
    "morning song"
)


def _corpus() -> dict:
    return {"id": "corpus-pinned", "version": "3", "entries": [REFERENCE]}


# ── Screening kinds ──────────────────────────────────────────────────


def test_exact_duplicate_matches_by_content_hash():
    result = screen_cases(
        [{"case_id": "case-exact", "input": REFERENCE}], _corpus(),
    )
    assert result["result"] == "suspected"
    assert result["matches"] == [{
        "case_id": "case-exact",
        "kind": "content_hash",
        "decision": "pending_review",
    }]


def test_normalized_duplicate_matches_after_normalization():
    variant = "  " + REFERENCE.upper().replace(" ", "   ") + "  "
    result = screen_cases(
        [{"case_id": "case-normalized", "input": variant}], _corpus(),
    )
    assert result["matches"][0]["kind"] == "exact_match"


def test_paraphrase_matches_by_approximate_overlap():
    paraphrase = REFERENCE.rsplit(" ", 1)[0] + " tune"
    result = screen_cases(
        [{"case_id": "case-paraphrase", "input": paraphrase}], _corpus(),
    )
    assert result["matches"][0]["kind"] == "approximate_overlap"


def test_unrelated_control_screens_clean():
    result = screen_cases(
        [{"case_id": "case-control",
          "input": "Compute the integral of x squared."}],
        _corpus(),
    )
    assert result["result"] == "screened"
    assert result["matches"] == []


def test_screening_records_corpus_threshold_and_version():
    result = screen_cases([], _corpus(), overlap_threshold=0.9)
    assert result["implementation"] == "exact-normalized-overlap"
    assert result["corpus"] == "corpus-pinned"
    assert result["corpus_version"] == "3"
    assert result["thresholds"]["overlap"] == 0.9
    assert result["version"] == "1"


def test_screening_never_claims_proof_of_no_exposure():
    result = screen_cases(
        [{"case_id": "case-control", "input": "Unrelated."}], _corpus(),
    )
    assert "cannot prove" in result["proof_disclaimer"]
    assert "prove" not in result["result"]


# ── Hidden-test protection and holdout audit ─────────────────────────


def test_hidden_test_stays_outside_every_non_holdout_context():
    cases = [
        {"case_id": "case-hidden",
         "classification": {"split": "hidden_test"}},
        {"case_id": "case-open", "classification": {"split": "test"}},
    ]
    for context in (
        "preview", "configuration", "prompt", "development_export",
    ):
        visible = visible_cases(cases, context=context)
        assert [case["case_id"] for case in visible] == ["case-open"]
    audited = visible_cases(cases, context="holdout_access")
    assert len(audited) == 2
    with pytest.raises(RightsPolicyError, match="Unknown visibility"):
        visible_cases(cases, context="anywhere")


def test_holdout_access_records_one_authenticated_actor():
    event = holdout_access_event("operator-a", reason="score audit")
    assert event["actor"] == "operator-a"
    assert event["access_id"].startswith("holdout-access-")
    with pytest.raises(RightsPolicyError, match="authenticated actor"):
        holdout_access_event("  ", reason="score audit")


def test_canary_disclosure_creates_incident_and_rotates():
    canaries = new_canaries(3)
    clean = detect_canary_disclosure(canaries, "Nothing leaked here.")
    assert clean is None
    incident = detect_canary_disclosure(
        canaries, f"model output quoting {canaries[1]} verbatim",
    )
    assert incident["exposed_canaries"] == [canaries[1]]
    assert incident["action"] == "rotate_affected_holdout"
    assert len(incident["rotated_canaries"]) == 3
    assert not set(incident["rotated_canaries"]) & set(canaries)


# ── License decisions ────────────────────────────────────────────────


def test_license_decision_table():
    assert license_decision("MIT") == "approved"
    assert license_decision("Apache-2.0") == "approved"
    assert license_decision("CC-BY-4.0") == "attribution_required"
    assert license_decision("CC-BY-NC-4.0") == "noncommercial"
    assert license_decision("mystery-terms") == "unresolved"


def test_unresolved_license_blocks_publication():
    source = valid_benchmark_source()
    source["license"]["name"] = "mystery-terms"
    decisions = build_rights_decisions([source])
    assert decisions[0]["decision"] == "unresolved"
    with pytest.raises(RightsPolicyError, match="Publication is blocked"):
        assert_publication_allowed(decisions)


def test_operator_decision_resolves_publication():
    source = valid_benchmark_source()
    source["license"]["name"] = "mystery-terms"
    decisions = build_rights_decisions(
        [source],
        operator_decisions={source["source_id"]: "approved"},
    )
    assert_publication_allowed(decisions)


def test_redistribution_denial_blocks_export():
    source = valid_benchmark_source()
    decisions = build_rights_decisions(
        [source],
        operator_decisions={source["source_id"]: "no_redistribution"},
    )
    # Publication proceeds, export blocks.
    assert_publication_allowed(decisions)
    with pytest.raises(RightsPolicyError, match="Export is blocked"):
        assert_export_allowed(decisions)


def test_incompatible_license_blocks_publication_and_export():
    source = valid_benchmark_source()
    decisions = build_rights_decisions(
        [source],
        operator_decisions={source["source_id"]: "incompatible"},
    )
    with pytest.raises(RightsPolicyError):
        assert_publication_allowed(decisions)
    with pytest.raises(RightsPolicyError):
        assert_export_allowed(decisions)


def test_allowed_export_includes_the_complete_attribution_bundle():
    source = valid_benchmark_source()
    decisions = build_rights_decisions([source])
    assert_export_allowed(decisions)
    bundle = attribution_bundle([source], decisions)
    assert bundle["entries"][0]["source_id"] == source["source_id"]
    assert bundle["entries"][0]["license"] == "MIT"
    assert bundle["entries"][0]["citation"] == "Cobbe et al."
    assert len(bundle["bundle_digest"]) == 64


# ── The immutable contamination and rights record ────────────────────


def test_contamination_record_validates_against_its_contract():
    source = valid_benchmark_source()
    decisions = build_rights_decisions([source])
    screening = screen_cases(
        [{"case_id": "case-exact", "input": REFERENCE}], _corpus(),
    )
    canaries = new_canaries(2)
    record = build_contamination_record(
        dataset_version_id="version-one",
        screening=screening,
        decisions=decisions,
        attribution=attribution_bundle([source], decisions),
        canaries=canaries,
        holdout_accesses=[
            holdout_access_event("operator-a", reason="audit"),
        ],
    )
    summary = validate_record(record)
    assert summary["schema_id"] == "contamination-rights-record"
    assert record["screening"]["result"] == "suspected"
    assert record["canaries"]["identifiers"] == canaries
    assert record["use_decisions"]["redistribution"] == "allowed"
    assert record["license_decisions"][0]["decision"] == "approved"


def test_noncommercial_and_denied_licenses_map_to_restricted():
    source = valid_benchmark_source()
    source["license"]["name"] = "CC-BY-NC-4.0"
    decisions = build_rights_decisions([source])
    record = build_contamination_record(
        dataset_version_id="version-one",
        screening=screen_cases([], _corpus()),
        decisions=decisions,
        attribution=attribution_bundle([source], decisions),
        canaries=new_canaries(1),
        holdout_accesses=[],
    )
    validate_record(record)
    assert record["license_decisions"][0]["decision"] == "restricted"
    assert record["use_decisions"]["redistribution"] == "denied"
