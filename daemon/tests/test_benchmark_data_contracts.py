"""Version, scorer, and outcome-mapping data contracts.

Every scorer declares its direction, scale, and evidence
requirements, publication validates scorer configuration against the
declared schema, every stored score carries its effective
configuration checksum, dataset distributions describe the admitted
immutable revision, and every run plan pins one sorted outcome-mapping
set with one exact member per arm. Analysis rejects an unknown
terminal reason or a stale mapping before any number computes.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import outcome_mappings, repository
from benchmarks.analysis import build_run_report
from benchmarks.gates import check_compatibility, invariant_digest
from benchmarks.provenance import content_checksum
from benchmarks.scoring import score_output

# ── Outcome mappings and the sorted per-experiment set ───────────────


def test_each_qualified_runtime_pair_registers_one_mapping():
    classic = outcome_mappings.registered_mapping("classic", "1")
    patchboard = outcome_mappings.registered_mapping("patchboard", "1")
    assert classic.runtime_id == "classic"
    assert patchboard.runtime_id == "patchboard"
    # Equal reason tables, different pair identity: the digests differ
    # because the runtime pair is part of the hashed payload.
    assert classic.digest != patchboard.digest
    assert classic.contract_version == (
        outcome_mappings.OUTCOME_MAPPING_CONTRACT_VERSION
    )


def test_every_reason_maps_to_complete_rules():
    mapping = outcome_mappings.registered_mapping("classic", "1")
    for reason in ("completed", "execution", "timeout", "budget_stop",
                   "configuration", "infrastructure", "cancelled"):
        rules = mapping.resolve(reason)
        assert set(rules) == {
            "benchmark_class", "retry_rule", "missingness", "denominator",
        }
    with pytest.raises(outcome_mappings.OutcomeMappingError):
        mapping.resolve("victory")


def test_the_sorted_set_is_order_independent_and_unique():
    classic = outcome_mappings.registered_mapping("classic", "1").member()
    patchboard = outcome_mappings.registered_mapping(
        "patchboard", "1",
    ).member()
    forward = outcome_mappings.build_outcome_mapping_set(
        [classic, patchboard],
    )
    reversed_input = outcome_mappings.build_outcome_mapping_set(
        [patchboard, classic],
    )
    # Reversed member input builds equal canonical bytes and an equal
    # set digest.
    assert forward["members"] == reversed_input["members"]
    assert forward["digest"] == reversed_input["digest"]
    assert forward["identifier"] == "bmas/outcome-mapping-set"
    with pytest.raises(outcome_mappings.OutcomeMappingError, match="pair"):
        outcome_mappings.build_outcome_mapping_set([classic, classic])
    duplicate_id = {**patchboard, "mapping_id": classic["mapping_id"]}
    with pytest.raises(
        outcome_mappings.OutcomeMappingError, match="identifier",
    ):
        outcome_mappings.build_outcome_mapping_set([classic, duplicate_id])
    with pytest.raises(outcome_mappings.OutcomeMappingError):
        outcome_mappings.build_outcome_mapping_set([])


def test_each_arm_resolves_only_its_exact_runtime_pair_member():
    mapping_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
        outcome_mappings.registered_mapping("patchboard", "1").member(),
    ])
    classic_member = outcome_mappings.member_for_arm(mapping_set, "classic")
    patchboard_member = outcome_mappings.member_for_arm(
        mapping_set, "patchboard",
    )
    assert classic_member["runtime_id"] == "classic"
    assert patchboard_member["runtime_id"] == "patchboard"
    assert classic_member["mapping_digest"] != (
        patchboard_member["mapping_digest"]
    )
    removed = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    with pytest.raises(outcome_mappings.OutcomeMappingError, match="member"):
        outcome_mappings.member_for_arm(removed, "patchboard")


def test_a_changed_mapping_needs_a_new_set_and_run_plan():
    changed_reasons = dict(outcome_mappings.SHARED_TASK_REASONS)
    changed_reasons["completed"] = {
        "benchmark_class": "success",
        "retry_rule": "allowed",
        "missingness": "observed",
        "denominator": "unconditional",
    }
    changed = outcome_mappings.build_outcome_mapping(
        runtime_id="classic",
        runtime_contract_version="1",
        reasons=changed_reasons,
    )
    with pytest.raises(
        outcome_mappings.OutcomeMappingError,
        match="new mapping set and run plan",
    ):
        outcome_mappings.register_outcome_mapping(changed)


def _plan_run(mapping_set: dict, arms: list[dict]) -> dict:
    return {
        "id": "run-contract",
        "status": "completed",
        "test_id": "test-contract",
        "test_revision_id": "revision-contract",
        "test_configuration": {"repetitions": 1},
        "execution_plan": {
            "outcome_mapping_set": mapping_set,
            "arms": arms,
        },
        "arms": [
            {"id": arm["id"], "runtime_id": arm["runtime_id"]}
            for arm in arms
        ],
        "attempts": [],
    }


def test_analysis_rejects_a_replaced_member_digest():
    mapping_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    arms = [{
        "id": "arm-classic",
        "runtime_id": "classic",
        "outcome_mapping": {
            **outcome_mappings.member_for_arm(mapping_set, "classic"),
            "mapping_digest": "f" * 64,
        },
    }]
    with pytest.raises(outcome_mappings.OutcomeMappingError, match="match"):
        outcome_mappings.validate_run_outcome_contract(
            _plan_run(mapping_set, arms),
        )


def test_analysis_rejects_a_stale_mapping_and_a_forged_set():
    member = outcome_mappings.registered_mapping("classic", "1").member()
    stale_member = {**member, "mapping_digest": "e" * 64}
    stale_set = outcome_mappings.build_outcome_mapping_set([stale_member])
    arms = [{
        "id": "arm-classic",
        "runtime_id": "classic",
        "outcome_mapping": dict(stale_member),
    }]
    with pytest.raises(outcome_mappings.OutcomeMappingError, match="stale"):
        outcome_mappings.validate_run_outcome_contract(
            _plan_run(stale_set, arms),
        )
    forged = outcome_mappings.build_outcome_mapping_set([member])
    forged["digest"] = "d" * 64
    with pytest.raises(outcome_mappings.OutcomeMappingError, match="digest"):
        outcome_mappings.validate_run_outcome_contract(
            _plan_run(forged, [{
                "id": "arm-classic",
                "runtime_id": "classic",
                "outcome_mapping": dict(member),
            }]),
        )


def test_analysis_rejects_an_unknown_terminal_reason():
    mapping_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    run = _plan_run(mapping_set, [{
        "id": "arm-classic",
        "runtime_id": "classic",
        "outcome_mapping": outcome_mappings.member_for_arm(
            mapping_set, "classic",
        ),
    }])
    run["attempts"] = [{
        "id": "attempt-a",
        "arm_id": "arm-classic",
        "status": "failed",
        "failure_category": "victory",
    }]
    with pytest.raises(
        outcome_mappings.OutcomeMappingError, match="victory",
    ):
        outcome_mappings.validate_run_outcome_contract(run)


def test_declared_exclusions_must_be_excludable_under_the_mapping():
    mapping_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    run = _plan_run(mapping_set, [{
        "id": "arm-classic",
        "runtime_id": "classic",
        "outcome_mapping": outcome_mappings.member_for_arm(
            mapping_set, "classic",
        ),
    }])
    run["test_configuration"]["infrastructure_exclusions"] = {
        "categories": ["execution"],
        "reason": "not allowed",
    }
    # A substantive failure can never leave the unconditional
    # denominator through the exclusion policy.
    with pytest.raises(
        outcome_mappings.OutcomeMappingError, match="excludable",
    ):
        outcome_mappings.validate_run_outcome_contract(run)


def _gate_run(identifier: str, mapping_set: dict) -> dict:
    return {
        "id": identifier,
        "status": "completed",
        "test_id": "test-contract",
        "test_revision_id": "revision-contract",
        "test_configuration_checksum": "checksum",
        "test_configuration": {"repetitions": 1,
                               "practical_difference": 0.01},
        "dataset_id": "dataset-contract",
        "dataset_checksum": "dataset-checksum",
        "execution_plan": {"outcome_mapping_set": mapping_set},
        "execution_plan_checksum": "plan-checksum",
        "revision_scorers": [{"id": "exact", "required": True,
                              "sort_order": 0,
                              "configuration_checksum": ""}],
        "arms": [],
        "attempts": [],
        "scores": [],
        "human_reviews": [],
    }


def test_the_invariant_digest_carries_only_the_set_digest():
    two_arm_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
        outcome_mappings.registered_mapping("patchboard", "1").member(),
    ])
    reversed_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("patchboard", "1").member(),
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    baseline = _gate_run("baseline", two_arm_set)
    candidate = _gate_run("candidate", reversed_set)
    # Classic and PatchBoard carry different mapping digests inside
    # one sorted set, and the two runs stay gate-compatible because
    # only the complete set digest enters the invariant.
    assert invariant_digest(baseline) == invariant_digest(candidate)
    compatibility = check_compatibility(baseline, candidate, [])
    assert compatibility["baseline_invariant_digest"] == (
        compatibility["candidate_invariant_digest"]
    )
    other_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    changed = _gate_run("changed", other_set)
    assert invariant_digest(baseline) != invariant_digest(changed)


def test_every_report_names_its_unit_estimand_and_mapping_set():
    mapping_set = outcome_mappings.build_outcome_mapping_set([
        outcome_mappings.registered_mapping("classic", "1").member(),
    ])
    run = _gate_run("run-report", mapping_set)
    report = build_run_report(run)
    analysis = report["analysis"]
    assert analysis["statistical_unit"] == "case"
    assert analysis["estimand"]["primary_estimand"] == (
        "paired-difference-in-weighted-case-means"
    )
    assert analysis["outcome_mapping_set"]["digest"] == (
        mapping_set["digest"]
    )
    assert analysis["outcome_mapping_set"]["contract_version"] == "1"


# ── Scorer configuration contracts ───────────────────────────────────


def _scorer(kind: str, configuration: dict) -> dict:
    return {
        "id": f"scorer-{kind}",
        "kind": kind,
        "version": "1",
        "configuration": configuration,
        "configuration_checksum": content_checksum(configuration),
    }


def test_scorer_configuration_changes_scorer_behavior():
    strict = score_output(
        scorer=_scorer("exact_match", {}),
        expected_output="Paris",
        actual_output="paris",
    )
    relaxed = score_output(
        scorer=_scorer("exact_match", {"case_sensitive": False}),
        expected_output="Paris",
        actual_output="paris",
    )
    assert strict["passed"] is False
    assert relaxed["passed"] is True
    exact = score_output(
        scorer=_scorer("numeric_match", {}),
        expected_output="10",
        actual_output="The answer is 10.4",
    )
    tolerant = score_output(
        scorer=_scorer("numeric_match", {"tolerance": "0.5"}),
        expected_output="10",
        actual_output="The answer is 10.4",
    )
    assert exact["passed"] is False
    assert tolerant["passed"] is True
    assert tolerant["explanation"] == "numeric_match_within_tolerance"
    narrow = score_output(
        scorer=_scorer("letter_match", {"choices": "AB"}),
        expected_output="C",
        actual_output="The answer is C",
    )
    # A letter outside the configured choice set never extracts.
    assert narrow["passed"] is False
    assert narrow["extracted_output"] is None


def test_every_score_result_carries_the_configuration_checksum():
    configuration = {"case_sensitive": False}
    result = score_output(
        scorer=_scorer("exact_match", configuration),
        expected_output="Paris",
        actual_output="PARIS",
    )
    assert result["configuration_checksum"] == content_checksum(
        configuration,
    )
    assert result["evidence"]["effective_configuration"] == configuration


# ── Database-backed publication, scoring, and distributions ──────────


@pytest_asyncio.fixture
async def contracts_db(tmp_path, monkeypatch):
    path = str(tmp_path / "contracts.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    for version, items in (
        ("version-contract-early", [
            {"id": "item-alpha", "item_key": "alpha", "input": "Q1",
             "expected_output": "42", "subject": "math", "split": "test",
             "tags": [], "metadata": {}},
            {"id": "item-beta", "item_key": "beta", "input": "Q2",
             "expected_output": "42", "subject": "math", "split": "test",
             "tags": [], "metadata": {}},
        ]),
        ("version-contract-late", [
            {"id": "item-gamma", "item_key": "gamma", "input": "Q3",
             "expected_output": "42", "subject": "physics",
             "split": "validation", "tags": [], "metadata": {}},
        ]),
    ):
        await db.create_dataset_version(
            dataset_id="dataset-contract",
            version_id=version,
            name="Contract data",
            description="",
            source_uri=None,
            license_name=None,
            author=None,
            dataset_metadata={},
            checksum=f"checksum-{version}",
            schema={"version": "1"},
            source_filename=f"{version}.jsonl",
            source_mime="application/x-ndjson",
            source_checksum=f"source-{version}",
            source_path=f"/tmp/{version}.jsonl",
            version_metadata={},
            items=items,
        )
    return path


def _arm(identifier: str) -> dict:
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    return {
        "id": identifier,
        "name": "Classic",
        "slug": "classic",
        "runtime_id": "classic",
        "configuration": envelope,
        "configuration_checksum": content_checksum(envelope),
    }


@pytest.mark.asyncio
async def test_registered_scorers_declare_their_complete_contract(
    contracts_db,
):
    scorers = {
        scorer["id"]: scorer for scorer in await repository.list_scorers()
    }
    for scorer_id in (
        "scorer-exact-match-v1",
        "scorer-gsm8k-numeric-v1",
        "scorer-mmlu-letter-v1",
    ):
        scorer = scorers[scorer_id]
        # Every scorer declares a direction, a scale, a description,
        # evidence requirements, and a configuration schema.
        assert scorer["direction"] == "higher_is_better"
        assert scorer["scale"] == "unit_interval"
        assert scorer["description"]
        assert "expected_output" in str(scorer["evidence_requirements"])
        assert scorer["configuration_schema"].get("type") == "object"


@pytest.mark.asyncio
async def test_an_invalid_scorer_configuration_blocks_publication(
    contracts_db,
):
    with pytest.raises(repository.BenchmarkConflict, match="invalid"):
        await repository.create_test_revision(
            test_id="test-invalid",
            revision_id="revision-invalid",
            name="invalid",
            description="",
            dataset_version_id="version-contract-early",
            configuration={"repetitions": 1, "seed": 1},
            arms=[_arm("arm-invalid")],
            scorers=[{
                "id": "scorer-gsm8k-numeric-v1",
                # The schema requires a decimal string tolerance.
                "configuration": {"tolerance": 0.5},
            }],
        )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS revisions FROM benchmark_test_revisions",
        )
        row = await cursor.fetchone()
    assert int(row["revisions"]) == 0


@pytest.mark.asyncio
async def test_the_run_plan_pins_the_set_and_scores_keep_checksums(
    contracts_db,
):
    configuration = {"case_sensitive": False}
    await repository.create_test_revision(
        test_id="test-contract",
        revision_id="revision-contract",
        name="contract",
        description="",
        dataset_version_id="version-contract-early",
        configuration={"repetitions": 1, "seed": 1,
                       "max_concurrency": 4, "timeout_seconds": 60},
        arms=[_arm("arm-contract")],
        scorers=[{
            "id": "scorer-exact-match-v1",
            "configuration": configuration,
        }],
    )
    run, _ = await repository.create_run(
        run_id="run-contract",
        revision_id="revision-contract",
        idempotency_key=None,
    )
    plan = run["execution_plan"]
    mapping_set = plan["outcome_mapping_set"]
    # The run plan carries one outcome-mapping-set digest, exact case
    # identifiers, and one valid member for each arm.
    assert mapping_set["digest"]
    assert mapping_set["contract_version"] == "1"
    expected_member = outcome_mappings.registered_mapping(
        "classic", "1",
    ).member()
    assert mapping_set["members"] == [expected_member]
    assert plan["arms"][0]["outcome_mapping"] == expected_member
    assert plan["estimand"]["families"] == {
        "math": ["alpha", "beta"],
    }

    # Drive both attempts to a terminal task and score them. Every
    # stored score carries the effective configuration checksum.
    async with aiosqlite.connect(contracts_db) as connection:
        connection.row_factory = aiosqlite.Row
        rows = await connection.execute_fetchall(
            "SELECT attempt.id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = 'run-contract'",
        )
        for index, row in enumerate(rows):
            task_id = f"task-contract-{index}"
            await connection.execute(
                "INSERT INTO tasks (id, label, full_input, status, "
                "terminal_kind, result_summary, total_cost_usd, "
                "total_tokens, duration_ms) VALUES (?, 'Benchmark', "
                "'Question', 'completed', 'completed', '42', 0.01, 10, 100)",
                (task_id,),
            )
            await connection.execute(
                "UPDATE benchmark_attempts SET status = 'running', "
                "task_id = ?, lease_token = 'lease', "
                "lease_expires_at = '2100-01-01T00:00:00.000Z' "
                "WHERE id = ?",
                (task_id, str(row["id"])),
            )
        await connection.commit()
        attempt_ids = [str(row["id"]) for row in rows]
    for attempt_id in attempt_ids:
        assert await repository.finish_attempt_from_task(
            attempt_id, "lease",
        )
    async with db._connect() as connection:  # noqa: SLF001
        score_rows = await connection.execute_fetchall(
            "SELECT configuration_checksum FROM benchmark_scores",
        )
    checksums = {row["configuration_checksum"] for row in score_rows}
    assert checksums == {content_checksum(configuration)}

    # The completed run's report resolves every terminal reason
    # through the pinned mapping set.
    stored = await repository.get_run("run-contract")
    assert stored is not None
    report = build_run_report(stored)
    assert report["analysis"]["outcome_mapping_set"]["digest"] == (
        mapping_set["digest"]
    )


@pytest.mark.asyncio
async def test_an_unknown_reason_rejects_the_stored_run_report(
    contracts_db,
):
    await repository.create_test_revision(
        test_id="test-unknown",
        revision_id="revision-unknown",
        name="unknown",
        description="",
        dataset_version_id="version-contract-early",
        configuration={"repetitions": 1, "seed": 1},
        arms=[_arm("arm-unknown")],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    await repository.create_run(
        run_id="run-unknown",
        revision_id="revision-unknown",
        idempotency_key=None,
    )
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_attempts SET status = 'failed', "
            "failure_category = 'victory'",
        )
        await connection.commit()
    stored = await repository.get_run("run-unknown")
    assert stored is not None
    with pytest.raises(
        outcome_mappings.OutcomeMappingError, match="victory",
    ):
        build_run_report(stored)


@pytest.mark.asyncio
async def test_distributions_describe_the_selected_immutable_revision(
    contracts_db,
):
    early = await db.get_dataset(
        "dataset-contract",
        distribution_version_id="version-contract-early",
    )
    late = await db.get_dataset(
        "dataset-contract",
        distribution_version_id="version-contract-late",
    )
    default = await db.get_dataset("dataset-contract")
    assert early is not None and late is not None and default is not None
    # The selected version's distribution matches its own cases, never
    # the latest upload's.
    assert early["subjects"] == {"math": 2}
    assert early["splits"] == {"test": 2}
    assert late["subjects"] == {"physics": 1}
    assert late["splits"] == {"validation": 1}
    assert default["distribution_version_id"] == "version-contract-late"
    by_id = {
        version["id"]: version for version in default["versions"]
    }
    assert by_id["version-contract-early"]["subjects"] == {"math": 2}
    assert by_id["version-contract-late"]["subjects"] == {"physics": 1}
