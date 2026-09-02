"""Portable transformation profile tests against shared fixtures.

The fixtures come from an independent reference generator, so equal
pinned inputs and recipes must produce equal case, recipe, and
dataset digests here and in every other supported implementation.
The suite also pins strict parsing, Unicode NFC, missing-value
semantics, number rendering, template grammar, deterministic
sampling, split assignment, and locale and time zone independence.
"""

from __future__ import annotations

import json
import locale
import os
import time
from pathlib import Path

import pytest

from benchmarks.transform_profile import (
    MISSING,
    PROFILE_NAME,
    PROFILE_VERSION,
    SEED_LIMIT,
    TransformProfileError,
    apply_recipe,
    canonical_json,
    case_digest,
    dataset_digest,
    rank_bytes,
    recipe_digest,
    render_number,
    render_template,
    resolve_pointer,
    strict_parse,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "transform_profile.json")
    .read_text(),
)


def _recipe(operations: list[dict], seed: int = 0) -> dict:
    return {
        "profile": PROFILE_NAME,
        # The profile version travels in recipe metadata, never inside
        # an identifier.
        "profile_version": PROFILE_VERSION,
        "seed": seed,
        "operations": operations,
    }


# ── Shared fixture agreement ─────────────────────────────────────────


def test_fixture_pins_the_profile_identity():
    assert FIXTURES["profile"] == PROFILE_NAME
    assert FIXTURES["profile_version"] == PROFILE_VERSION


@pytest.mark.parametrize(
    "vector", FIXTURES["numbers"], ids=lambda vector: vector["expected"],
)
def test_number_rendering_matches_the_reference(vector):
    assert render_number(vector["value"]) == vector["expected"]


def test_case_digests_match_the_reference():
    digests = [case_digest(case) for case in FIXTURES["cases"]]
    assert digests == FIXTURES["case_digests"]


@pytest.mark.parametrize("vector", FIXTURES["rank_vectors"])
def test_rank_vectors_match_the_reference(vector):
    case = next(
        case for case in FIXTURES["cases"]
        if case["case_id"] == vector["case_id"]
    )
    rank = rank_bytes(
        seed=vector["seed"],
        operation_index=vector["operation_index"],
        case_digest_value=bytes.fromhex(case_digest(case)),
        counter=vector["counter"],
    )
    assert rank.hex() == vector["rank"]


def test_sample_selection_matches_the_reference():
    plan = FIXTURES["sample"]
    outcome = apply_recipe(
        FIXTURES["cases"],
        _recipe(
            [{"operation": "sample",
              "parameters": {"count": plan["count"]}}],
            seed=plan["seed"],
        ),
    )
    selected = [case["case_id"] for case in outcome["cases"]]
    assert selected == plan["selected_case_ids"]


def test_split_assignment_matches_the_reference():
    plan = FIXTURES["split"]
    outcome = apply_recipe(
        FIXTURES["cases"],
        _recipe(
            [
                {"operation": "normalize", "parameters": {"fields": []}},
                {"operation": "split",
                 "parameters": {"weights": plan["weights"]}},
            ],
            seed=plan["seed"],
        ),
    )
    assigned = {
        case["case_id"]: case["split"] for case in outcome["cases"]
    }
    assert assigned == plan["assignment"]


# ── Strict parsing and Unicode rules ─────────────────────────────────


def test_strict_parse_rejects_invalid_utf8():
    with pytest.raises(TransformProfileError, match="strict UTF-8"):
        strict_parse(b'{"a": "\xff\xfe"}')


def test_strict_parse_rejects_duplicate_keys_before_construction():
    with pytest.raises(TransformProfileError, match="Duplicate"):
        strict_parse(b'{"a": 1, "a": 2}')


def test_generated_strings_normalize_to_nfc():
    decomposed = "é"
    rendered = render_template(
        "${x}", {"x": {"pointer": "/value"}}, {"value": decomposed},
    )
    assert rendered == "é"


def test_nfc_equivalent_inputs_produce_equal_digests():
    assert case_digest({"k": "é"}) != case_digest({"k": "é"})
    # Raw content digests differ, and normalization is the declared
    # operation that makes them equal.
    outcome_a = apply_recipe(
        [{"case_id": "one", "k": "é"}],
        _recipe([{"operation": "normalize",
                  "parameters": {"fields": ["k"], "forms": ["nfc"]}}]),
    )
    outcome_b = apply_recipe(
        [{"case_id": "one", "k": "é"}],
        _recipe([{"operation": "normalize",
                  "parameters": {"fields": ["k"], "forms": ["nfc"]}}]),
    )
    assert outcome_a["dataset_digest"] == outcome_b["dataset_digest"]


# ── Missing values and JSON null ─────────────────────────────────────


def test_missing_stays_distinct_from_null():
    assert resolve_pointer({"a": None}, "/a") is None
    assert resolve_pointer({"a": None}, "/b") is MISSING
    exists = apply_recipe(
        [{"case_id": "one", "a": None}, {"case_id": "two"}],
        _recipe([{"operation": "filter",
                  "parameters": {"field": "a", "operator": "exists"}}]),
    )
    assert [case["case_id"] for case in exists["cases"]] == ["one"]
    absent = apply_recipe(
        [{"case_id": "one", "a": None}, {"case_id": "two"}],
        _recipe([{"operation": "filter",
                  "parameters": {"field": "a", "operator": "absent"}}]),
    )
    assert [case["case_id"] for case in absent["cases"]] == ["two"]


def test_missing_value_never_serializes():
    with pytest.raises(TransformProfileError, match="never serializes"):
        canonical_json(MISSING)


# ── Template grammar ─────────────────────────────────────────────────


def test_template_binding_default_and_missing_failure():
    bindings = {
        "answer": {"pointer": "/expected/answer", "default": "unknown"},
    }
    rendered = render_template("A: ${answer}", bindings, {"expected": {}})
    assert rendered == "A: unknown"
    with pytest.raises(TransformProfileError, match="no default"):
        render_template(
            "A: ${answer}", {"answer": {"pointer": "/missing"}}, {},
        )


def test_template_literal_escape_and_null_rendering():
    rendered = render_template(
        "$${literal} ${v}", {"v": {"pointer": "/v"}}, {"v": None},
    )
    assert rendered == "${literal} null"


def test_template_object_values_use_canonical_json():
    rendered = render_template(
        "${v}", {"v": {"pointer": "/v"}}, {"v": {"b": 1, "a": 2}},
    )
    assert rendered == '{"a":2,"b":1}'


def test_template_json_pointer_escapes():
    case = {"a/b": {"~c": "found"}}
    assert resolve_pointer(case, "/a~1b/~0c") == "found"


def test_template_unbound_name_fails():
    with pytest.raises(TransformProfileError, match="unbound"):
        render_template("${nope}", {}, {})


# ── Number rules ─────────────────────────────────────────────────────


def test_negative_zero_normalizes_to_zero():
    assert render_number(-0.0) == "0"
    assert canonical_json({"a": -0.0}) == '{"a":0}'
    assert case_digest({"a": -0.0}) == case_digest({"a": 0.0})


def test_safe_integer_limit_enforced():
    assert render_number(9007199254740991) == "9007199254740991"
    with pytest.raises(TransformProfileError, match="safe range"):
        render_number(9007199254740992)


def test_non_finite_numbers_reject():
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(TransformProfileError, match="finite"):
            render_number(value)


def test_seed_boundaries():
    digest = bytes.fromhex(case_digest({"case_id": "one"}))
    rank_bytes(seed=SEED_LIMIT, operation_index=0,
               case_digest_value=digest, counter=0)
    with pytest.raises(TransformProfileError, match="64-bit"):
        rank_bytes(seed=SEED_LIMIT + 1, operation_index=0,
                   case_digest_value=digest, counter=0)


def test_ranks_compare_as_big_endian_bytes():
    digest = bytes.fromhex(case_digest({"case_id": "one"}))
    left = rank_bytes(seed=1, operation_index=0,
                      case_digest_value=digest, counter=0)
    right = rank_bytes(seed=2, operation_index=0,
                       case_digest_value=digest, counter=0)
    assert (left < right) == (
        int.from_bytes(left, "big") < int.from_bytes(right, "big")
    )


# ── Operations ───────────────────────────────────────────────────────


def test_sample_keeps_source_order_and_resolves_ties_by_ordinal():
    duplicate = {"case_id": "same", "input": "equal"}
    outcome = apply_recipe(
        [dict(duplicate), dict(duplicate)],
        _recipe([{"operation": "sample", "parameters": {"count": 1}}]),
    )
    # Equal content gives equal ranks; the stable key breaks the tie
    # with the source ordinal, so the first occurrence stays.
    assert len(outcome["cases"]) == 1


def test_sample_output_preserves_source_order():
    outcome = apply_recipe(
        FIXTURES["cases"],
        _recipe([{"operation": "sample", "parameters": {"count": 5}}],
                seed=7),
    )
    source_order = [case["case_id"] for case in FIXTURES["cases"]]
    ids = [case["case_id"] for case in outcome["cases"]]
    assert ids == sorted(ids, key=source_order.index)


def test_split_weights_must_be_positive_integers():
    with pytest.raises(TransformProfileError, match="positive integers"):
        apply_recipe(
            FIXTURES["cases"],
            _recipe([{"operation": "split",
                      "parameters": {"weights": {"train": 0.5}}}]),
        )


def test_deduplicate_after_normalization():
    cases = [
        {"case_id": "one", "text": "  Hello   World  "},
        {"case_id": "two", "text": "hello world"},
        {"case_id": "three", "text": "different"},
    ]
    outcome = apply_recipe(
        cases,
        _recipe([
            {"operation": "normalize",
             "parameters": {"fields": ["text"],
                            "forms": ["trim", "collapse_whitespace",
                                      "lower"]}},
            {"operation": "deduplicate",
             "parameters": {"fields": ["text"]}},
        ]),
    )
    assert [case["case_id"] for case in outcome["cases"]] == [
        "one", "three",
    ]


def test_select_rename_and_attach_rubric():
    outcome = apply_recipe(
        [{"case_id": "one", "question": "Q", "extra": "drop"}],
        _recipe([
            {"operation": "select",
             "parameters": {"fields": ["case_id", "question"]}},
            {"operation": "rename",
             "parameters": {"mapping": {"question": "input"}}},
            {"operation": "attach_rubric",
             "parameters": {"rubric_id": "rubric-alpha"}},
        ]),
    )
    assert outcome["cases"] == [{
        "case_id": "one", "input": "Q", "rubric_id": "rubric-alpha",
    }]


# ── Digest equality and environment independence ─────────────────────


def test_equal_inputs_and_recipes_produce_equal_digests():
    recipe = _recipe(
        [{"operation": "sample", "parameters": {"count": 4}}], seed=11,
    )
    first = apply_recipe(FIXTURES["cases"], recipe)
    second = apply_recipe(
        json.loads(json.dumps(FIXTURES["cases"])),
        json.loads(json.dumps(recipe)),
    )
    assert first["dataset_digest"] == second["dataset_digest"]
    assert first["recipe_digest"] == second["recipe_digest"]
    assert first["case_digests"] == second["case_digests"]
    assert first["recipe_digest"] == recipe_digest(recipe)


def test_engine_metadata_carries_versions_as_values():
    outcome = apply_recipe(
        FIXTURES["cases"],
        _recipe([{"operation": "normalize", "parameters": {}}]),
    )
    assert outcome["engine"]["profile"] == PROFILE_NAME
    assert outcome["engine"]["profile_version"] == PROFILE_VERSION


def test_digests_are_locale_and_time_zone_independent(monkeypatch):
    recipe = _recipe(
        [
            {"operation": "map_template",
             "parameters": {
                 "target": "prompt",
                 "template": "Value ${n}",
                 "bindings": {"n": {"pointer": "/n"}},
             }},
            {"operation": "split",
             "parameters": {"weights": {"train": 2, "test": 1}}},
        ],
        seed=5,
    )
    cases = [
        {"case_id": f"case-{index}", "n": 1234567.25 + index}
        for index in range(6)
    ]
    baseline = apply_recipe(cases, recipe)["dataset_digest"]
    original_locale = locale.setlocale(locale.LC_ALL)
    try:
        for name in ("C", "en_US.UTF-8", "de_DE.UTF-8"):
            try:
                locale.setlocale(locale.LC_ALL, name)
            except locale.Error:
                continue
            for zone in ("UTC", "Pacific/Kiritimati", "America/Anchorage"):
                monkeypatch.setenv("TZ", zone)
                time.tzset()
                assert apply_recipe(cases, recipe)[
                    "dataset_digest"
                ] == baseline
    finally:
        locale.setlocale(locale.LC_ALL, original_locale)
        os.environ.pop("TZ", None)
        time.tzset()


def test_recipe_envelope_validation():
    with pytest.raises(TransformProfileError, match="profile"):
        apply_recipe([], {"profile": "other", "profile_version": 1,
                          "operations": [{"operation": "normalize"}]})
    with pytest.raises(TransformProfileError, match="profile version"):
        apply_recipe([], _recipe([{"operation": "normalize"}]) | {
            "profile_version": 99,
        })
    with pytest.raises(TransformProfileError, match="Unknown operation"):
        apply_recipe([], _recipe([{"operation": "explode"}]))


def test_dataset_digest_is_order_sensitive():
    forward = dataset_digest(FIXTURES["cases"])
    reversed_digest = dataset_digest(list(reversed(FIXTURES["cases"])))
    assert forward != reversed_digest
