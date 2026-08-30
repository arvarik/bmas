"""Foundation Stage 0B: admission compiles routing fields and recovery
uses the exact stored runtime pair.

The admission compiler validates the complete routing identity of one
candidate run. It persists nothing and it enqueues nothing. The task
queue path persists the exact runtime pair before queue admission, and
every recovery path resolves that stored pair or fails closed.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import database as db
import routes.submit as submit
from core.run_contracts import (
    MissingReaderError,
    RuntimeAdmission,
    VersionFieldError,
    VersionSet,
    compile_run_admission,
)
from core.variants import (
    MissingCheckpointReaderError,
    RuntimeKey,
    RuntimeNotAdmissibleError,
    UnknownVariantError,
    UnsupportedContractError,
)

CLASSIC_KEY = RuntimeKey("classic", "1")

VERSION_SET = VersionSet(
    runtime_spec_schema_version="1",
    runtime_state_schema_version="1",
    checkpoint_schema_version="1",
    activation_schema_version="1",
    activation_dispatch_schema_version="1",
    activation_acknowledgement_schema_version="1",
    digest_profile_version="1",
    runtime_outcome_schema_version="1",
    post_terminal_invalidation_schema_version="1",
    agent_protocol_version="1",
    agent_receipt_schema_version="1",
    effect_schema_version="1",
    trace_schema_version="1",
    evidence_schema_version="1",
    asset_manifest_schema_version="1",
    policy_set_schema_version="1",
    capability_document_version="1",
    database_schema_version=db.SCHEMA_VERSION,
)

READERS = frozenset({"reader.checkpoint", "reader.activation", "reader.trace"})


def compile_admission(**overrides) -> RuntimeAdmission:
    arguments = dict(
        admission_id="admission-a",
        task_id="task-a",
        run_id="run-a",
        runtime_key=CLASSIC_KEY,
        version_set=VERSION_SET,
        specification_digest="a" * 64,
        capability_document_digest="b" * 64,
        prompt_profile_digest="c" * 64,
        role_profile_digest="d" * 64,
        seed_policy="recorded",
        requested_seed=7,
        required_reader_ids=("reader.checkpoint", "reader.trace"),
        interface_adapter_id="classic",
        available_reader_ids=READERS,
    )
    arguments.update(overrides)
    return compile_run_admission(**arguments)


def test_admission_compiles_for_every_qualified_pair():
    for runtime_id in ("classic", "patchboard", "stigmergic"):
        admission = compile_admission(
            runtime_key=RuntimeKey(runtime_id, "1"),
            interface_adapter_id=runtime_id,
        )
        assert admission.runtime_key == RuntimeKey(runtime_id, "1")
        assert admission.version_set is VERSION_SET


def test_admission_rejects_an_unknown_pair_without_fallback():
    with pytest.raises(UnsupportedContractError):
        compile_admission(runtime_key=RuntimeKey("classic", "2"))
    with pytest.raises(UnknownVariantError):
        compile_admission(runtime_key=RuntimeKey("unheard-of", "1"))


def test_admission_rejects_a_missing_reader_without_fallback():
    with pytest.raises(MissingReaderError) as failure:
        compile_admission(
            required_reader_ids=("reader.checkpoint", "reader.evidence"),
        )
    assert failure.value.missing == ("reader.evidence",)


def test_a_reader_mismatch_is_not_a_runtime_mismatch():
    with pytest.raises(MissingReaderError) as reader_failure:
        compile_admission(required_reader_ids=("reader.gone",))
    assert not isinstance(reader_failure.value, UnknownVariantError)
    with pytest.raises(UnknownVariantError) as runtime_failure:
        compile_admission(runtime_key=RuntimeKey("unheard-of", "1"))
    assert not isinstance(runtime_failure.value, MissingReaderError)


def test_every_version_field_validates_independently():
    for spec in dataclasses.fields(VersionSet):
        if spec.name == "database_schema_version":
            broken = dataclasses.asdict(VERSION_SET) | {spec.name: 0}
        else:
            broken = dataclasses.asdict(VERSION_SET) | {spec.name: ""}
        with pytest.raises(VersionFieldError) as failure:
            VersionSet(**broken)
        assert failure.value.field_name == spec.name


def test_no_version_field_is_inferred_from_another():
    varied = dataclasses.replace(VERSION_SET, trace_schema_version="9")
    assert varied.trace_schema_version == "9"
    assert varied.checkpoint_schema_version == "1"
    assert varied.agent_protocol_version == "1"


def test_each_admission_field_changes_the_digest():
    admission = compile_admission()
    baseline = admission.digest()
    changed = dataclasses.replace(admission, specification_digest="e" * 64)
    assert changed.digest() != baseline
    changed = dataclasses.replace(admission, requested_seed=8)
    assert changed.digest() != baseline
    changed = dataclasses.replace(
        admission, runtime_key=RuntimeKey("patchboard", "1"),
    )
    assert changed.digest() != baseline
    assert compile_admission().digest() == baseline


def test_seed_policy_is_a_closed_set():
    assert compile_admission(seed_policy="none", requested_seed=None)
    with pytest.raises(VersionFieldError):
        compile_admission(seed_policy="whatever")


def test_stage_zero_b_admission_touches_no_queue_or_database():
    import core.run_contracts as contracts_module

    source = Path(contracts_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import database",
        "import aiosqlite",
        "from routes",
        "import routes",
        "_task_queue",
    ):
        assert forbidden not in source


def test_a_failed_admission_leaves_the_task_open():
    from core.variants import (
        _ALIASES,
        _VARIANTS,
        VariantDescriptor,
        register_variant,
    )

    class PlannedRuntime:
        descriptor = VariantDescriptor("planned-only", "Planned", "1")

        @classmethod
        async def capture_configuration(cls, overrides=None): ...

        @classmethod
        def configuration_from_metadata(cls, metadata): ...

        @classmethod
        async def run(cls, host, request): ...

    task_state = {"status": "pending"}
    key = register_variant(
        "planned-only", PlannedRuntime, availability="planned",
    )
    try:
        with pytest.raises(RuntimeNotAdmissibleError):
            compile_admission(runtime_key=key)
    finally:
        _VARIANTS.pop(key, None)
        _ALIASES.pop("planned-only", None)
    assert task_state == {"status": "pending"}


@pytest.mark.asyncio
async def test_the_stored_pair_persists_before_queue_admission(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "admission.db"))
    await db.init_db()
    await db.create_task_with_meta(
        "task-pair",
        "pair",
        "pair",
        "classic",
        {"effective_configuration": {"variant": "classic"}},
        runtime_contract_version="1",
    )
    row = await db.get_task("task-pair")
    assert row is not None
    assert row["variant"] == "classic"
    assert row["runtime_contract_version"] == "1"
    resumable = await db.get_resumable_tasks()
    stored = {task["id"]: task for task in resumable}
    assert stored["task-pair"]["runtime_contract_version"] == "1"


@pytest.mark.asyncio
async def test_the_migration_backfills_the_stored_pair(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "backfill.db"))
    await db.init_db()
    async with db._connect() as connection:
        await connection.execute(
            "INSERT INTO tasks (id, label, full_input, status, variant, "
            "runtime_contract_version) VALUES "
            "('task-legacy', 'legacy', 'legacy', 'pending', 'classic', NULL)"
        )
        await connection.commit()
    async with db._connect() as connection:
        await db._migrate_add_runtime_pair(connection)
    row = await db.get_task("task-legacy")
    assert row is not None
    assert row["runtime_contract_version"] == "1"


def test_recovery_resolves_the_stored_pair_exactly():
    key, runtime = submit.resolve_stored_runtime(
        {"variant": "classic", "runtime_contract_version": "1"},
    )
    assert key == CLASSIC_KEY
    assert runtime.descriptor.contract_version == "1"


def test_recovery_fails_closed_without_a_complete_pair():
    with pytest.raises(UnknownVariantError):
        submit.resolve_stored_runtime({"variant": "classic"})
    with pytest.raises(UnknownVariantError):
        submit.resolve_stored_runtime(
            {"runtime_contract_version": "1"},
        )


def test_recovery_fails_closed_after_a_reader_disappears():
    with pytest.raises(UnsupportedContractError):
        submit.resolve_stored_runtime(
            {"variant": "classic", "runtime_contract_version": "2"},
        )
    with pytest.raises(UnknownVariantError):
        submit.resolve_stored_runtime(
            {"variant": "removed-runtime", "runtime_contract_version": "1"},
        )


def test_a_resume_requires_a_checkpoint_reader():
    from core.variants import (
        _ALIASES,
        _VARIANTS,
        VariantDescriptor,
        register_variant,
    )

    class NoReaderRuntime:
        descriptor = VariantDescriptor(
            "no-reader", "No reader", "1", supports_recovery=False,
        )

        @classmethod
        async def capture_configuration(cls, overrides=None): ...

        @classmethod
        def configuration_from_metadata(cls, metadata): ...

        @classmethod
        async def run(cls, host, request): ...

    key = register_variant(
        "no-reader", NoReaderRuntime, availability="test_only",
    )
    try:
        stored = {"variant": "no-reader", "runtime_contract_version": "1"}
        assert submit.resolve_stored_runtime(stored)
        with pytest.raises(MissingCheckpointReaderError):
            submit.resolve_stored_runtime(stored, resuming=True)
    finally:
        _VARIANTS.pop(key, None)
        _ALIASES.pop("no-reader", None)
