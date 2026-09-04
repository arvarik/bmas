"""Data-class-driven redaction across envelopes, evidence, and exports.

The policy classifies by declared field name, by measurement marker,
and by value shape. Secrets redact under every persistence view,
sensitive values redact until explicitly permitted, prohibited values
never persist, token counts and budgets stay readable, credential-
shaped values redact under any key, URLs lose their user information,
and every record pins the policy digest it redacted under.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from benchmarks import data_classes, provenance
from benchmarks.data_classes import (
    REDACTED,
    RedactionPolicyError,
    RedactionReport,
    classify,
    classify_name,
    detect_secret_value,
    policy_digest,
    policy_document,
    redact,
)
from core.asset_store import DataClass


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("api_key", DataClass.SECRET),
        ("API-Key", DataClass.SECRET),
        ("openai_api_key", DataClass.SECRET),
        ("access_token", DataClass.SECRET),
        ("token", DataClass.SECRET),
        ("credentials", DataClass.SECRET),
        ("hermes_gateway_key_env", DataClass.INTERNAL),
        ("api_key_env", DataClass.INTERNAL),
        ("view_budget_tokens", DataClass.INTERNAL),
        ("cleaner_token_threshold", DataClass.INTERNAL),
        ("max_tokens", DataClass.INTERNAL),
        ("total_tokens", DataClass.INTERNAL),
        ("token_count", DataClass.INTERNAL),
        ("reviewer_email", DataClass.SENSITIVE),
        ("ip_address", DataClass.SENSITIVE),
        ("card_number", DataClass.PROHIBITED),
        ("model", DataClass.INTERNAL),
    ],
)
def test_names_classify_by_declaration_not_by_fragment(name, expected):
    assert classify_name(name) == expected


def test_measurement_names_stay_readable_after_redaction():
    envelope = {
        "effective_configuration": {
            "view_budget_tokens": 12000,
            "cleaner_token_threshold": 8000,
            "max_tokens": 5,
            "total_tokens": 42,
            "api_key_env": "FAKE_PROVIDER_KEY",
            "api_token": "abc",
            "token": "t",
            "nested": {"access_token": "x", "token_limit": 9},
        },
    }
    redacted = redact(envelope)
    configuration = redacted["effective_configuration"]
    assert configuration["view_budget_tokens"] == 12000
    assert configuration["cleaner_token_threshold"] == 8000
    assert configuration["max_tokens"] == 5
    assert configuration["total_tokens"] == 42
    assert configuration["api_key_env"] == "FAKE_PROVIDER_KEY"
    assert configuration["api_token"] == REDACTED
    assert configuration["token"] == REDACTED
    assert configuration["nested"] == {"access_token": REDACTED,
                                       "token_limit": 9}
    # The runtime envelope parses these counts after redaction.
    assert int(configuration["view_budget_tokens"]) == 12000


@pytest.mark.parametrize(
    ("value", "detector"),
    [
        ("Bearer abcdefghijklmnop", "bearer_or_basic_header"),
        ("sk-live-0123456789", "provider_secret_key"),
        ("eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4", "signed_token"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_access_key_id"),
        ("ghp_abcdefghijklmnopqrstuvwxyz", "github_token"),
        ("-----BEGIN RSA PRIVATE KEY-----\nabc", "private_key_block"),
        ("The sum is 42. #### 42", None),
        (12000, None),
        ("skeleton-key", None),
    ],
)
def test_credential_shaped_values_redact_under_any_key(value, detector):
    assert detect_secret_value(value) == detector
    report = RedactionReport()
    redacted = redact({"note": value, "items": [value]}, report=report)
    if detector is None:
        assert redacted == {"note": value, "items": [value]}
        assert report.detectors == {}
    else:
        assert redacted == {"note": REDACTED, "items": [REDACTED]}
        assert report.detectors == {"note": detector, "items[0]": detector}


def test_urls_lose_user_information_and_keep_the_host():
    value = {"gateway": "https://user:pass@gateway.example/v1?x=1",
             "plain": "https://gateway.example/v1"}
    redacted = redact(value)
    assert redacted["gateway"] == "https://[redacted]@gateway.example/v1?x=1"
    assert redacted["plain"] == "https://gateway.example/v1"
    assert redact("redis://:secret@localhost:6379/0") == (
        "redis://[redacted]@localhost:6379/0"
    )


def test_prohibited_values_never_persist_and_sensitive_values_redact():
    report = RedactionReport()
    redacted = redact(
        {"card_number": "4111", "reviewer_email": "a@b.c",
         "reviewer": "r-1", "nested": [{"cvv": "123", "ok": 1}]},
        report=report,
    )
    assert redacted == {"reviewer_email": REDACTED, "reviewer": "r-1",
                        "nested": [{"ok": 1}]}
    assert report.prohibited == ["card_number", "nested[0].cvv"]
    assert report.sensitive == ["reviewer_email"]
    assert classify({"api_key": "k", "email": "e", "cvv": "1"}) == {
        "api_key": "secret", "cvv": "prohibited", "email": "sensitive",
    }


def test_views_and_overrides_follow_the_matrix():
    value = {"api_key": "k", "email": "e", "cvv": "1", "note": "n",
             "internal_only": "i"}
    complete = redact(value, view="complete")
    assert complete == {"api_key": "k", "email": "e", "note": "n",
                        "internal_only": "i"}
    public = redact(
        value, view="public", overrides={"internal_only": "internal"},
    )
    assert public == {"api_key": REDACTED, "email": REDACTED, "note": "n"}
    overridden = redact(value, overrides={"note": DataClass.SECRET})
    assert overridden["note"] == REDACTED
    with pytest.raises(RedactionPolicyError, match="Unknown redaction view"):
        redact(value, view="secret")


def test_policy_digest_pins_the_declarative_policy():
    document = policy_document()
    assert document["schema_id"] == "bmas.redaction_policy"
    assert document["policy_version"] == 2
    assert "api_key" in document["secret_field_names"]
    assert "tokens" in document["measurement_markers"]
    assert len(policy_digest()) == 64
    assert policy_digest() == policy_digest()
    # The provenance helper delegates to the policy.
    assert provenance.redact_secrets({"password": "p", "max_tokens": 3}) == {
        "password": REDACTED, "max_tokens": 3,
    }


@pytest.mark.asyncio
async def test_runtime_envelope_pins_the_policy_and_keeps_counts():
    from benchmarks.runtime import prepare_benchmark_arm

    arm = await prepare_benchmark_arm("classic", None)
    envelope = arm["configuration"]
    assert envelope["redaction_policy_digest"] == policy_digest()
    rendered = json.dumps(envelope)
    assert "sk-" not in rendered
    assert "effective_configuration" in envelope


@pytest.mark.asyncio
async def test_evidence_bundle_and_export_pin_the_policy(tmp_path, monkeypatch):
    from test_evidence_capture import make_attempts

    import database as db
    from benchmarks import evidence_capture, replay_bundle

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "redaction.db"))
    await db.init_db()
    attempts = await make_attempts(1)
    captured = await evidence_capture.capture_attempt_evidence(
        attempt_id=attempts[0],
        run_manifest={"run_id": "run-evidence", "api_key": "sk-live-1234567"},
        runtime_specification={"runtime": "classic",
                               "gateway": "https://u:p@g.example/v1"},
        case={"case_id": "case-0"},
        trace_events=[{"kind": "action", "authorization": "Bearer abcdefghij"}],
        final_output="42",
        resources={"cost": None, "tokens": 10, "latency_ms": 5},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={"reservation_id": "reservation-a"},
    )
    record = captured["record"]
    assert record["redaction_policy_digest"] == policy_digest()
    trace = evidence_capture.read_evidence_section(record["trace_digest"])
    assert trace["value"][0]["authorization"] == REDACTED

    archive = replay_bundle.build_bundle(
        run_manifest={"run_id": "r", "api_key": "sk-live-1234567",
                      "password": "p"},
        sources=[{"source_id": "s", "token": "t"}],
        dataset_manifest={"dataset_id": "d"},
        test_revision={"revision_id": "testrev-1"},
        run_plan={"plan_id": "plan-1"},
        runtime_specifications=[{"credentials": {"secret": "x"}}],
        evidence_bundles=[record],
        score_records=[],
        snapshot={"snapshot_id": "snapshot-1"},
        frozen_input={"input_digest": "0" * 64},
        report={"results_digest": "0" * 64},
        gate_results=[],
        artifacts={},
        schemas={},
        policy="redacted",
    )
    import io

    with zipfile.ZipFile(io.BytesIO(archive["archive"])) as bundle:
        names = bundle.namelist()
        manifest_name = next(name for name in names if name.endswith("manifest.json") and "/" not in name.strip("/")) if any(name == "manifest.json" for name in names) else next(name for name in names if "manifest" in name and name.count("/") == 0)
        manifest = json.loads(bundle.read(manifest_name))
        rendered = b"".join(bundle.read(name) for name in sorted(names))
    assert manifest["redaction_policy_digest"] == provenance.content_checksum({
        "policy": "redacted", "redactor": policy_document(),
    })
    for leaked in (b"sk-live-1234567", b'"p"', b'"t"', b'"x"', b"u:p@"):
        assert leaked not in rendered
    assert data_classes.REDACTED.encode() in rendered
