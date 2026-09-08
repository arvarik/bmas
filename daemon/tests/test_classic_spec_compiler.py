"""The Classic specification compiler.

Every fidelity and effort pair compiles to one deterministic golden
specification. Every controlled object forbids extra properties. The
layers resolve in order, a fidelity-fixed field rejects a later
override with a recorded warning, and the deployment caps clamp every
bounded value after the task overrides with a recorded adjustment. The
native admission stores one immutable specification and binds its
digest, and the legacy pair keeps its envelope unchanged.

Regenerate the golden files after a deliberate contract change::

    BMAS_UPDATE_RUNTIME_FIXTURES=1 python -m pytest tests/test_classic_spec_compiler.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

import config
import database as db
import interactive_admission as admission
import run_admission
import runtime_journal as journal
import settings_store
from core import foundation_gates
from core.digest_profile import plain_json
from core.money import Money
from core.variants import RuntimeKey, VariantConfigurationError
from core.variants.classic import ClassicRuntime, ClassicVariantRuntime, compiler, profiles
from core.variants.classic import spec as spec_models
from core.variants.classic.outcomes import CLASSIC_TASK_REASONS, PAPER_ALIGNED_CONTEXT_LIMIT_REASON
from core.variants.classic.spec import (
    BoardSettings,
    ClassicSpec,
    ClassicSpecInput,
    DeploymentSnapshot,
    TaskOverrideSet,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "classic_specs"
UPDATE_FIXTURES = os.environ.get("BMAS_UPDATE_RUNTIME_FIXTURES") == "1"
NATIVE = RuntimeKey("classic", "2")
LEGACY = RuntimeKey("classic", "1")
REQUESTED_LEVELS = ("quick", "standard", "thorough", "exhaustive", "exploratory", "adversarial")

GOLDEN_CLASSIC = {
    "max_rounds": 4,
    "max_duration_s": 1800,
    "budget_ceiling_usd": 0.5,
    "max_concurrent_activations": 3,
    "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 4},
    "cleaner_entry_threshold": 12,
    "cleaner_token_threshold": 8000,
    "cleaner_retention_weights": {"salience": 2.0, "confidence": 1.0, "recency": 0.1, "size_penalty": 0.01},
    "stall_rounds": 2,
    "max_replans": 2,
    "cu_mode": "llm",
    "coordinator_narration": False,
    "sole_similarity": "auto",
    "grace_verification": True,
    "actor_context": "chained",
    "require_evidence": False,
    "round_execution": "concurrent",
    "view_budget_tokens": 12000,
}
GOLDEN_REGISTRY = {
    "planner": {"profile": "planner", "endpoints": ["http://agent-a.fixture", "http://agent-b.fixture"]},
    "critic": {"profile": "critic", "endpoints": ["http://agent-a.fixture"]},
    "decider": {"profile": "decider", "endpoints": ["http://agent-a.fixture"]},
    "cleaner": {"profile": "cleaner", "enabled": False},
}


def golden_deployment() -> DeploymentSnapshot:
    """One deterministic deployment snapshot for the golden specifications."""
    return DeploymentSnapshot(
        classic=dict(GOLDEN_CLASSIC),
        routing={"simple": "model-fast", "light": "model-fast", "medium": "model-strong", "complex": "model-strong"},
        role_registry=GOLDEN_REGISTRY,
        board=BoardSettings(
            max_entry_chars=8000, max_title_len=200,
            salience_weights={"confidence": 0.4, "recency": 0.2, "refs_in": 0.3, "penalty": 0.3},
        ),
        model_pools={"medium": ["model-strong", "model-fast"]},
        model_profiles={
            "model-fast": {"provider": "fixture", "model": "fast-model", "reasoning": "off"},
            "model-strong": {"provider": "fixture", "model": "strong-model"},
        },
        model_pricing={
            "model-fast": {"input_cost_per_token": "0.0000001", "output_cost_per_token": "0.0000004"},
            "model-strong": {"input_cost_per_token": "0.000003", "output_cost_per_token": "0.000015"},
        },
        triage_model="model-fast",
        node_endpoints=["http://agent-a.fixture"],
        endpoint_capability_digests={"http://agent-a.fixture": "fixture-capability-digest"},
    )


def compile_pair(fidelity: str, effort: str, **overrides) -> ClassicSpec:
    return compiler.compile_specification(ClassicSpecInput(
        fidelity=fidelity, effort=effort, deployment=golden_deployment(),
        task_overrides=TaskOverrideSet(**overrides),
    ))


def encode(spec: ClassicSpec) -> bytes:
    text = json.dumps(compiler.normalized_specification(spec), indent=2, sort_keys=True, ensure_ascii=True)
    return (text + "\n").encode("utf-8")


# ── Golden specifications ─────────────────────────────────────────────


@pytest.mark.parametrize("fidelity", sorted(profiles.FIDELITY_PROFILES))
@pytest.mark.parametrize("effort", REQUESTED_LEVELS)
def test_every_profile_pair_compiles_to_its_golden_specification(fidelity, effort):
    spec = compile_pair(fidelity, effort, seed=7)
    again = compile_pair(fidelity, effort, seed=7)
    assert compiler.specification_digest(spec) == compiler.specification_digest(again)
    path = FIXTURES_DIR / f"{fidelity}-{effort}.json"
    encoded = encode(spec)
    if UPDATE_FIXTURES:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
    assert path.is_file(), f"missing golden specification {path.name}"
    assert path.read_bytes() == encoded, f"the golden specification {path.name} changed"
    # The golden file round-trips through the model and keeps its digest.
    loaded = ClassicSpec.model_validate(json.loads(path.read_text()))
    assert compiler.specification_digest(loaded) == compiler.specification_digest(spec)
    assert spec.fidelity.profile_id == fidelity
    assert spec.effort.requested_level == effort
    assert spec.effort.profile_id == profiles.EFFORT_ALIASES.get(effort, effort)


def test_every_controlled_object_forbids_extra_properties():
    for model in (ClassicSpec, ClassicSpecInput, spec_models.ClassicFidelityProfile,
                  spec_models.ClassicEffortProfile, spec_models.ClassicSpecWarning,
                  spec_models.ClassicSpecEstimate):
        assert spec_models.open_objects(model.model_json_schema()) == [], model.__name__
    with pytest.raises(ValueError):
        ClassicSpecInput.model_validate({
            "fidelity": "production_safe", "effort": "standard",
            "deployment": golden_deployment().model_dump(), "surprise": True,
        })
    with pytest.raises(ValueError):
        spec_models.SpecLimits(
            max_cost={"currency": "USD", "amount_nanos": 1}, max_duration_seconds=1,
            max_input_tokens=1, max_output_tokens=1, strict_pricing=True, extra=1,
        )


def test_the_alias_table_maps_the_shipped_levels_and_keeps_the_presets():
    assert profiles.EFFORT_ALIASES == {
        "quick": "quick", "standard": "balanced", "thorough": "rigorous", "exhaustive": "long_horizon",
    }
    assert set(profiles.EFFORT_PROFILES) == {"quick", "balanced", "rigorous", "exploratory", "long_horizon", "adversarial"}
    assert profiles.resolve_effort_level("thorough") == ("thorough", "rigorous")
    assert profiles.resolve_effort_level("long_horizon") == ("long_horizon", "long_horizon")
    assert profiles.resolve_effort_level(None) == ("standard", "balanced")
    with pytest.raises(ValueError):
        profiles.resolve_effort_level("heroic")
    assert profiles.resolve_fidelity(None) == "production_safe"
    with pytest.raises(ValueError):
        profiles.resolve_fidelity("paper_exact")
    for preset in profiles.EFFORT_PROFILES.values():
        unknown = sorted(name for name in preset.values if name not in profiles.SCHEMA_DEFAULTS)
        assert unknown == [], preset.profile_id
    for profile in profiles.FIDELITY_PROFILES.values():
        unknown = sorted(name for name in profile.values if name not in profiles.SCHEMA_DEFAULTS)
        assert unknown == [], profile.profile_id
    spec = compile_pair("production_safe", "thorough")
    alias_warnings = [warning for warning in spec.warnings if warning.kind == "effort_alias"]
    assert [(warning.requested, warning.effective) for warning in alias_warnings] == [("thorough", "rigorous")]


# ── Layered resolution ────────────────────────────────────────────────


def test_the_layers_resolve_in_order_and_record_every_offer():
    spec = compile_pair("production_safe", "standard")
    # The balanced preset yields to the deployment: standard runs with
    # the session settings.
    assert spec.coordination.round_execution == "concurrent"
    rounds = spec.resolution["coordination.round_execution"]
    assert rounds.layer == "deployment"
    assert rounds.offered == {"schema_default": "concurrent", "effort": "sequential", "deployment": "concurrent"}
    assert rounds.not_applied == []
    # Every other preset keeps its intensity over the deployment default.
    rigorous = compile_pair("production_safe", "thorough")
    assert rigorous.coordination.max_rounds == 12
    record = rigorous.resolution["coordination.max_rounds"]
    assert record.layer == "effort"
    assert record.offered["deployment"] == 4
    assert record.not_applied == ["deployment"]
    # A field no layer touched records its schema default.
    assert rigorous.resolution["recovery.checkpoint_every_rounds"].layer == "schema_default"
    # The task override wins over the preset.
    overridden = compile_pair("production_safe", "thorough", classic={"max_rounds": 6})
    assert overridden.coordination.max_rounds == 6
    assert overridden.resolution["coordination.max_rounds"].layer == "task_overrides"
    # A dotted specification field is a valid override too.
    typed = compile_pair("production_safe", "standard", classic={"verification.evidence_policy": "typed_sources_strict"})
    assert typed.verification.evidence_policy == "typed_sources_strict"


def test_a_fidelity_fixed_field_rejects_a_later_override_with_a_warning():
    spec = compile_pair("paper_aligned", "standard", classic={"round_execution": "concurrent", "actor_context": "chained"})
    assert spec.coordination.round_execution == "sequential"
    record = spec.resolution["coordination.round_execution"]
    assert record.layer == "fidelity"
    assert record.not_applied == ["deployment", "task_overrides"]
    rejected = [
        (warning.field, warning.layer, warning.requested)
        for warning in spec.warnings if warning.kind == "rejected_override"
    ]
    assert ("coordination.round_execution", "task_overrides", "concurrent") in rejected
    assert ("coordination.round_execution", "deployment", "concurrent") in rejected
    assert ("board.view_strategy", "effort", "role_bounded") in rejected
    assert ("verification.independent_verifiers", "effort", 1) in rejected
    # A value that equals the fixed value is not a rejection.
    assert not any(warning.field == "memory.actor_memory" for warning in spec.warnings)
    assert spec.memory.actor_memory == "none"
    assert spec.cleaner.enabled is False
    assert spec.board.view_strategy == "full_board"
    assert spec.verification.independent_verifiers == 0
    assert spec.models.verifier is None
    assert PAPER_ALIGNED_CONTEXT_LIMIT_REASON in spec.termination.terminal_reasons
    assert PAPER_ALIGNED_CONTEXT_LIMIT_REASON not in compile_pair("production_safe", "standard").termination.terminal_reasons


def test_the_deployment_caps_apply_after_every_override_and_record_each_clamp():
    spec = compile_pair(
        "production_safe", "quick",
        classic={"max_rounds": 99, "budget_ceiling_usd": 5000, "stall_rounds": 0, "view_budget_tokens": 100},
    )
    assert spec.coordination.max_rounds == 50
    assert spec.limits.max_cost.to_money() == Money.from_decimal_string("USD", "1000")
    assert spec.recovery.stall_rounds == 1
    assert spec.board.view_budget_tokens == 512
    assert spec.deployment_caps.policy_version == profiles.DEPLOYMENT_CAPS_VERSION
    assert spec.deployment_caps.applied_after_user_overrides is True
    adjustments = {adjustment.field: adjustment for adjustment in spec.deployment_caps.adjustments}
    assert adjustments["coordination.max_rounds"].model_dump() == {
        "field": "coordination.max_rounds", "requested": 99, "effective": 50, "bound": 50, "rule": "maximum",
    }
    assert adjustments["limits.max_cost"].rule == "maximum"
    assert adjustments["recovery.stall_rounds"].rule == "minimum"
    assert spec.resolution["coordination.max_rounds"].layer == "deployment_caps"
    assert spec.resolution["coordination.max_rounds"].offered["task_overrides"] == 99
    clamped = sorted(warning.field for warning in spec.warnings if warning.kind == "clamped_value")
    assert clamped == ["board.view_budget_tokens", "coordination.max_rounds", "limits.max_cost", "recovery.stall_rounds"]
    # A value inside the range records no adjustment.
    assert compile_pair("production_safe", "quick").deployment_caps.adjustments == []
    with pytest.raises(compiler.ClassicSpecError):
        compile_pair("production_safe", "quick", classic={"rounds_max": 3})
    with pytest.raises(compiler.ClassicSpecError):
        compile_pair("production_safe", "quick", classic={"sole_similarity": "judge"})


# ── Models, prices, seeds, endpoints, prompts, lineage, termination ───


def test_the_specification_resolves_models_prices_seeds_endpoints_prompts_and_lineage():
    spec = compile_pair("production_safe", "thorough", seed=41, routing={"complex": "model-fast"})
    assert spec.models.triage.model_id == "fixture/fast-model"
    assert spec.models.control_unit.alias == "model-fast"
    assert spec.models.tier_models["complex"].alias == "model-fast"
    assert [ref.alias for ref in spec.models.expert_pool["medium"]] == ["model-strong", "model-fast"]
    assert spec.models.verifier is not None and spec.models.verifier.alias == "model-strong"
    assert spec.models.role_tier_binding["planner"] == "triage_tier"
    assert spec.models.tier_models["medium"].tokenizer_revision == compiler.TOKENIZER_REVISION
    # Prices are exact per-million Money values from the decimal source strings.
    assert spec.prices.source_amount_strings["model-strong"].input_per_million == "3"
    assert spec.prices.rates["model-strong"].input_per_million.to_money() == Money.from_decimal_string("USD", "3")
    assert spec.prices.rates["model-fast"].output_per_million.amount_nanos == 400_000_000
    assert not any(warning.kind == "missing_price" for warning in spec.warnings)
    # Seeds: the task seed and two derived streams, recorded.
    assert spec.randomness.seed_policy == "recorded"
    assert spec.randomness.task_seed == 41
    assert spec.randomness.roster_seed is not None and spec.randomness.roster_seed != spec.randomness.candidate_order_seed
    assert compile_pair("production_safe", "thorough").randomness.roster_seed is None
    # Endpoint sets: ordered, with the adapter version and the known capability digest.
    assert set(spec.routing.endpoint_sets) == {"worker_primary", "worker_secondary"}
    planner_set = spec.routing.endpoint_sets[spec.routing.role_endpoint_sets["planner"]]
    assert [endpoint.endpoint_id for endpoint in planner_set.endpoints] == ["http://agent-a.fixture", "http://agent-b.fixture"]
    assert planner_set.endpoints[0].capability_record_digest == "fixture-capability-digest"
    assert planner_set.endpoints[1].capability_record_digest is None
    assert planner_set.endpoints[0].adapter_version == "agent-protocol/2"
    assert planner_set.failover_policy.policy_version == "ordered-safe-failover/1"
    assert "cleaner" not in spec.routing.role_endpoint_sets
    assert spec.routing.role_endpoint_sets["expert"] == spec.routing.role_endpoint_sets["critic"]
    # Prompt template digests equal the digests the artifact store computes.
    templates = compiler.static_prompt_templates()
    assert set(spec.prompts.static_template_digests) == set(templates)
    assert spec.prompts.static_template_digests["planner"] == compiler.prompt_template_digest(templates["planner"])
    # Lineage: every model maps to an unverified lineage, and the independence rule warns.
    assert spec.team.independent_model_families == 2
    assert {assignment.lineage_status for assignment in spec.model_lineage.assignments.values()} == {"unverified"}
    assert any(warning.kind == "unverified_lineage" for warning in spec.warnings)
    # Termination and the estimate.
    assert spec.termination.policy_id == "verified_candidate"
    assert set(spec.termination.terminal_reasons) <= set(CLASSIC_TASK_REASONS)
    assert spec.estimate.max_in_flight_activations == spec.coordination.max_parallel_agents
    assert spec.estimate.cost_maximum == spec.limits.max_cost
    assert spec.estimate.latency_maximum_seconds == spec.limits.max_duration_seconds
    assert compile_pair("paper_aligned", "standard").estimate.max_in_flight_activations == 1


def test_a_required_role_without_an_endpoint_fails_the_compile():
    registry = dict(GOLDEN_REGISTRY)
    registry["planner"] = {"profile": "planner", "enabled": False}
    deployment = DeploymentSnapshot.model_validate({
        **golden_deployment().model_dump(), "node_endpoints": [], "role_registry": registry,
    })
    with pytest.raises(compiler.ClassicSpecError, match="required role"):
        compiler.compile_specification(ClassicSpecInput(fidelity="production_safe", effort="standard", deployment=deployment))
    with pytest.raises(compiler.ClassicSpecError, match="Unknown role"):
        compile_pair("production_safe", "standard", role_registry={"auditor": {"enabled": True}})


def test_money_never_parses_from_a_float_and_the_projection_feeds_the_engine():
    assert compiler.money_text(0.5) == "0.5"
    assert compiler.money_text(10.0) == "10"
    assert compiler.money_text("2.00") == "2"
    with pytest.raises(compiler.ClassicSpecError):
        compiler.money_text(0)
    spec = compile_pair("production_safe", "exhaustive", classic={"budget_ceiling_usd": 0.1})
    assert spec.limits.max_cost.amount_nanos == 100_000_000
    settings = compiler.legacy_settings_from_spec(spec)
    assert settings["max_rounds"] == 32
    assert settings["budget_ceiling_usd"] == 0.1
    assert settings["actor_context"] == "fresh"
    assert settings["require_evidence"] is True
    assert settings["grace_verification"] is True
    assert settings["sole_similarity"] == "token_similarity"
    from settings_store import validate_classic_settings
    validate_classic_settings(settings)


# ── The native capture and admission ─────────────────────────────────


@pytest_asyncio.fixture
async def native_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "native.db"))
    monkeypatch.setattr(config, "FOUNDATION_GATES", {name: True for name in foundation_gates.PLANNED_WRITER_GATES}, raising=False)
    monkeypatch.setattr(config, "STORAGE_OPERATOR_CONFIRMED", True, raising=False)
    monkeypatch.setattr(config, "REQUIRE_PROVIDER_QUALIFICATION", False, raising=False)
    monkeypatch.setattr(config, "ADMIT_TEST_ONLY_RUNTIMES", True, raising=False)
    monkeypatch.setattr(config, "ROLE_REGISTRY", {
        "planner": {"profile": "planner", "endpoints": ["http://agent.test"]},
        "critic": {"profile": "critic", "endpoints": ["http://agent.test"]},
        "decider": {"profile": "decider", "endpoints": ["http://agent.test"]},
    }, raising=False)
    monkeypatch.setattr(config, "MODEL_PRICING", {
        "test-light": {"input_cost_per_token": 1e-07, "output_cost_per_token": 4e-07, "source": "test"},
    }, raising=False)
    monkeypatch.setattr(settings_store, "_store", None)
    admission.reset_for_tests()
    await db.init_db()
    yield tmp_path
    monkeypatch.setattr(settings_store, "_store", None)


@pytest.mark.asyncio
async def test_the_native_capture_compiles_the_submission_and_projects_the_engine_settings(native_db):
    envelope = await ClassicRuntime.capture_configuration({"effort": "thorough", "fidelity": "paper_aligned", "seed": 3})
    assert envelope["variant_contract_version"] == "2"
    assert envelope["configuration_schema_version"] == "2"
    assert envelope["fidelity"] == "paper_aligned"
    assert envelope["effort"] == "thorough"
    assert envelope["settings"]["classic"]["max_rounds"] == 12
    assert envelope["settings"]["classic"]["round_execution"] == "sequential"
    assert envelope["specification_input"]["task_overrides"]["seed"] == 3
    assert ClassicRuntime.configuration_from_metadata({"effective_configuration": envelope}) == envelope
    # The native pair accepts the preset classes and rejects unknown values.
    assert (await ClassicRuntime.capture_configuration({"effort": "long_horizon"}))["effort"] == "long_horizon"
    with pytest.raises(VariantConfigurationError):
        await ClassicRuntime.capture_configuration({"effort": "heroic"})
    with pytest.raises(VariantConfigurationError):
        await ClassicRuntime.capture_configuration({"fidelity": "paper_exact"})
    with pytest.raises(VariantConfigurationError):
        await ClassicRuntime.capture_configuration({"classic": {"rounds_max": 1}})
    with pytest.raises(VariantConfigurationError):
        ClassicRuntime.configuration_from_metadata({"effective_configuration": {"configuration_schema_version": "1", "variant": "classic"}})


@pytest.mark.asyncio
async def test_the_legacy_pair_keeps_its_envelope_and_rejects_a_fidelity_profile(native_db):
    envelope = await ClassicVariantRuntime.capture_configuration({"effort": "thorough"})
    assert envelope["configuration_schema_version"] == "1"
    assert "fidelity" not in envelope and "specification_input" not in envelope
    with pytest.raises(VariantConfigurationError, match="fidelity"):
        await ClassicVariantRuntime.capture_configuration({"fidelity": "paper_aligned"})
    with pytest.raises(VariantConfigurationError):
        await ClassicVariantRuntime.capture_configuration({"effort": "long_horizon"})


@pytest.mark.asyncio
async def test_the_native_admission_stores_one_immutable_specification_and_binds_its_digest(native_db):
    envelope = await ClassicRuntime.capture_configuration({"effort": "quick", "fidelity": "production_safe"})
    await db.create_task_with_meta("task-native", "interactive", "Add 20 and 22.", "classic",
                                   {"effective_configuration": envelope}, runtime_contract_version="2")
    admitted = await admission.admit_task_run(task_id="task-native", runtime_key=NATIVE, effective_configuration=envelope)
    assert admitted is not None and admitted["new"] is True
    row = await db.get_classic_specification(admitted["run_id"])
    assert row is not None
    assert (row["fidelity_profile_id"], row["effort_profile_id"], row["requested_effort_level"]) == ("production_safe", "quick", "quick")
    assert row["schema_version"] == "classic-spec/1"
    chain = await journal.read_journal(run_id=admitted["run_id"])
    assert chain[0].payload["specification_digest"] == row["specification_digest"]
    assert row["journal_cursor"] == chain[0].journal_cursor
    # The promoted artifact holds the complete specification with the same digest.
    stored = compiler.load_specification(compiler.specification_store(), row["artifact_digest"])
    assert compiler.specification_digest(stored) == row["specification_digest"]
    assert stored.inputs.asset_manifest_digest is not None
    assert stored.limits.max_cost.amount_nanos == 250_000_000
    async with db._connect() as connection:  # noqa: SLF001
        limit = await (await connection.execute(
            "SELECT limit_amount FROM budget_limits WHERE budget_id = ?", (admitted["budget_id"],),
        )).fetchone()
        assert int(limit[0]) == 250_000
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await connection.execute("UPDATE classic_specifications SET effort_profile_id = 'balanced' WHERE run_id = ?", (admitted["run_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await connection.execute("DELETE FROM classic_specifications WHERE run_id = ?", (admitted["run_id"],))
    # A second admission returns the stored identity and keeps one row.
    again = await admission.admit_task_run(task_id="task-native", runtime_key=NATIVE, effective_configuration=envelope)
    assert again is not None and again["new"] is False
    async with db._connect() as connection:  # noqa: SLF001
        count = await (await connection.execute("SELECT COUNT(*) FROM classic_specifications")).fetchone()
    assert int(count[0]) == 1


@pytest.mark.asyncio
async def test_a_native_task_without_a_specification_input_fails_closed(native_db):
    await db.create_task_with_meta("task-bare", "interactive", "Add 1 and 2.", "classic",
                                   {"effective_configuration": {}}, runtime_contract_version="2")
    with pytest.raises(run_admission.AdmissionPrerequisiteError, match="specification"):
        await admission.admit_task_run(task_id="task-bare", runtime_key=NATIVE, effective_configuration={"variant": "classic"})
    assert await db.get_classic_specification("run-task-bare") is None
    assert await journal.read_journal(run_id="run-task-bare") == []


@pytest.mark.asyncio
async def test_the_legacy_admission_keeps_the_envelope_digest_and_no_specification_row(native_db):
    envelope = await ClassicVariantRuntime.capture_configuration({"effort": "quick"})
    await db.create_task_with_meta("task-legacy", "interactive", "Add 2 and 3.", "classic",
                                   {"effective_configuration": envelope}, runtime_contract_version="1")
    admitted = await admission.admit_task_run(task_id="task-legacy", runtime_key=LEGACY, effective_configuration=envelope)
    assert admitted is not None and admitted["new"] is True
    assert await db.get_classic_specification(admitted["run_id"]) is None
    chain = await journal.read_journal(run_id=admitted["run_id"])
    expected = admission.digest_hex(admission.SPECIFICATION_DIGEST_DOMAIN, plain_json({
        "runtime_key": LEGACY.to_dict(), "effective_configuration": envelope,
    }))
    assert chain[0].payload["specification_digest"] == expected
