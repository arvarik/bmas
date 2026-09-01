"""Benchmarking Phase 0 regression tests: gates, status, denominators.

One test exists for every confirmed defect from the correctness
repair phase. Gates stay terminal and revision-compatible, execution,
scoring, and analysis status separate, aggregates come from the
server, and the success and resource denominators follow the frozen
rules.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import records, repository
from benchmarks.analysis import build_run_report
from benchmarks.gates import (
    GateCompatibilityError,
    GateTerminalityError,
    check_compatibility,
    evaluate_gate,
    invariant_digest,
    validate_display_exceptions,
    validate_rules,
)
from benchmarks.provenance import content_checksum

FUTURE = "2100-01-01T00:00:00.000Z"


def _attempt(
    identifier,
    arm_id,
    item,
    repeat,
    retry,
    status="completed",
    failure_category=None,
    cost=0.01,
):
    return {
        "id": identifier,
        "trial_id": f"trial-{arm_id}-{item}",
        "dataset_item_id": item,
        "item_key": item,
        "subject": "math",
        "split": "test",
        "tags": [],
        "arm_id": arm_id,
        "arm_name": arm_id.title(),
        "arm_slug": arm_id,
        "runtime_id": "classic",
        "repeat_index": repeat,
        "retry_index": retry,
        "status": status,
        "failure_category": failure_category,
        "total_cost_usd": cost,
        "total_tokens": 100,
        "duration_ms": 1000,
        "task_id": f"task-{identifier}",
        "snapshot_checksum": f"checksum-{identifier}",
    }


def _score(attempt_id, score, scorer_id="scorer-exact", status="scored"):
    return {
        "id": f"score-{attempt_id}-{scorer_id}",
        "attempt_id": attempt_id,
        "scorer_id": scorer_id,
        "scorer_name": scorer_id,
        "scorer_version": "1",
        "status": status,
        "score": score if status == "scored" else None,
        "passed": None if status != "scored" else int(score >= 1.0),
    }


def _revision_scorers():
    return [
        {
            "id": "scorer-exact",
            "name": "Exact",
            "version": "1",
            "configuration_checksum": "scorer-checksum",
            "sort_order": 0,
            "required": 1,
        },
    ]


def _arms(model="model-a", prompt="prompt-a", runtime="classic"):
    return [
        {
            "id": "arm-left",
            "name": "Left",
            "slug": "left",
            "runtime_id": runtime,
            "configuration": {
                "effective_configuration": {"model": model, "prompt": prompt},
            },
            "configuration_checksum": content_checksum([model, prompt]),
        },
    ]


def _gate_run(
    identifier="run-one",
    status="completed",
    *,
    dataset_checksum="dataset-checksum",
    configuration_checksum="configuration-checksum",
    scorers=None,
    arms=None,
    attempts=None,
    scores=None,
    configuration=None,
    analysis_status=None,
):
    attempts = attempts if attempts is not None else [
        _attempt("a-one", "left", "one", 1, 0),
    ]
    scores = scores if scores is not None else [_score("a-one", 1.0)]
    run = {
        "id": identifier,
        "status": status,
        "test_id": "test-one",
        "test_revision_id": "revision-one",
        "test_configuration": configuration or {"repetitions": 1},
        "test_configuration_checksum": configuration_checksum,
        "dataset_id": "dataset-one",
        "dataset_checksum": dataset_checksum,
        "execution_plan_checksum": "plan-checksum",
        "revision_scorers": scorers if scorers is not None else _revision_scorers(),
        "arms": arms if arms is not None else _arms(),
        "attempts": attempts,
        "scores": scores,
    }
    if analysis_status is not None:
        run["analysis_status"] = analysis_status
    return run


ARM_RULE = [{
    "id": "score-floor",
    "metric": "arm.left.score.scorer-exact",
    "operator": "gte",
    "value": 0.5,
}]


# ── Gates: preview, terminality, compatibility ───────────────────────


def test_preview_never_saves_and_final_needs_a_terminal_candidate():
    baseline = _gate_run("baseline")
    active = _gate_run("candidate", "running")
    preview = evaluate_gate(baseline, active, ARM_RULE, mode="preview")
    assert preview["mode"] == "preview"
    assert preview["status"] == "indeterminate"
    with pytest.raises(GateTerminalityError):
        evaluate_gate(baseline, active, ARM_RULE, mode="final")
    # The completed candidate produces one terminal final decision, so
    # the earlier preview blocked nothing.
    final = evaluate_gate(baseline, _gate_run("candidate"), ARM_RULE)
    assert final["mode"] == "final"
    assert final["status"] == "passed"


def test_gate_rejects_a_blocked_candidate_analysis():
    baseline = _gate_run("baseline")
    blocked = _gate_run("candidate", analysis_status="blocked")
    with pytest.raises(GateTerminalityError, match="no valid analysis"):
        evaluate_gate(baseline, blocked, ARM_RULE, mode="final")


def test_gate_rejects_different_dataset_checksums():
    with pytest.raises(GateCompatibilityError, match="dataset checksum"):
        check_compatibility(
            _gate_run("baseline"),
            _gate_run("candidate", dataset_checksum="other-dataset"),
            [],
        )


def test_gate_rejects_different_scorer_digests():
    changed = _revision_scorers()
    changed[0]["configuration_checksum"] = "other-scorer-checksum"
    with pytest.raises(GateCompatibilityError, match="scorer digest"):
        check_compatibility(
            _gate_run("baseline"),
            _gate_run("candidate", scorers=changed),
            [],
        )


def test_gate_rejects_different_primary_metrics():
    # Both runs use the same two scorers, but the candidate promotes
    # the other one to primary, so only the primary metric differs.
    base = _revision_scorers()[0]
    pair = [
        dict(base),
        {**base, "id": "scorer-other", "sort_order": 1},
    ]
    swapped = [
        {**base, "sort_order": 1},
        {**base, "id": "scorer-other", "sort_order": 0},
    ]
    with pytest.raises(GateCompatibilityError, match="primary metric"):
        check_compatibility(
            _gate_run("baseline", scorers=pair),
            _gate_run("candidate", scorers=swapped),
            [],
        )


def test_invariant_digests_match_across_declared_treatments():
    # Runtime, model, prompt, and configuration stay outside the
    # invariant digest, so declared treatments keep equal digests.
    mapping_set = {"outcome_mappings": {"passed": "success"},
                   "repetitions": 1}
    baseline = _gate_run("baseline", configuration=mapping_set)
    candidate = _gate_run(
        "candidate",
        configuration=mapping_set,
        configuration_checksum="other-configuration",
        arms=_arms(model="model-b", prompt="prompt-b", runtime="patchboard"),
    )
    assert invariant_digest(baseline) == invariant_digest(candidate)
    compatibility = check_compatibility(
        baseline, candidate,
        ["configuration", "model", "prompt", "runtime"],
    )
    # One outcome-mapping-set digest spans the runtime treatments, and
    # each runtime arm still selects its exact member through its own
    # arm configuration.
    assert compatibility["baseline_invariant_digest"] == (
        compatibility["candidate_invariant_digest"]
    )
    assert set(compatibility["observed_treatments"]) == {
        "configuration", "model", "prompt", "runtime",
    }


def test_gate_rejects_each_undeclared_treatment_difference():
    baseline = _gate_run("baseline")
    cases = {
        "runtime": _gate_run("candidate", arms=_arms(runtime="patchboard")),
        "model": _gate_run("candidate", arms=_arms(model="model-b")),
        "prompt": _gate_run("candidate", arms=_arms(prompt="prompt-b")),
        "configuration": _gate_run(
            "candidate", configuration_checksum="other-configuration",
        ),
    }
    for axis, candidate in cases.items():
        with pytest.raises(GateCompatibilityError, match="undeclared"):
            check_compatibility(baseline, candidate, [])
        # The same difference passes once the declaration allows it.
        assert check_compatibility(baseline, candidate, [axis])


def test_a_changed_outcome_mapping_set_breaks_the_invariant():
    baseline = _gate_run("baseline")
    baseline["execution_plan"] = {
        "outcome_mapping_set": {"digest": "a" * 64},
    }
    candidate = _gate_run("candidate")
    candidate["execution_plan"] = {
        "outcome_mapping_set": {"digest": "b" * 64},
    }
    # Only the complete mapping-set digest enters the invariant, and a
    # different set digest breaks gate compatibility.
    with pytest.raises(GateCompatibilityError, match="invariant digest"):
        check_compatibility(baseline, candidate, [])


# ── Gates: display exceptions ────────────────────────────────────────


def test_a_narrow_exception_covers_one_unavailable_secondary_display():
    baseline = _gate_run("baseline")
    candidate = _gate_run("candidate")
    # The secondary display scorer produced no scores, so its metric
    # is unavailable and the rule alone would stay indeterminate.
    rules = ARM_RULE + [{
        "id": "secondary-display",
        "metric": "arm.left.score.scorer-latency",
        "operator": "gte",
        "value": 0.0,
    }]
    exception = {
        "scope": "secondary_display:arm.left.score.scorer-latency",
        "author": "operator-a",
        "expires_at": FUTURE,
        "reason": "the latency scorer shipped no samples this run",
    }
    report = evaluate_gate(
        baseline, candidate, rules,
        display_exceptions=[exception], now="2026-09-01T00:00:00.000Z",
    )
    waived = [rule for rule in report["rules"]
              if rule["status"] == "waived_display"]
    assert len(waived) == 1
    assert waived[0]["display_exception"]["reason"] == exception["reason"]
    assert report["status"] == "passed"


def test_exceptions_never_cover_protected_targets():
    for target in ("cases", "scorers", "units", "outcomes",
                   "estimands", "missingness"):
        with pytest.raises(ValueError, match="never cover"):
            validate_display_exceptions(
                [{
                    "scope": target,
                    "author": "operator-a",
                    "expires_at": FUTURE,
                    "reason": "not allowed",
                }],
                primary_metric=None,
            )
    with pytest.raises(ValueError, match="primary metric"):
        validate_display_exceptions(
            [{
                "scope": "secondary_display:arm.left.score.scorer-exact",
                "author": "operator-a",
                "expires_at": FUTURE,
                "reason": "not allowed",
            }],
            primary_metric="scorer-exact",
        )
    with pytest.raises(ValueError, match="requires"):
        validate_display_exceptions(
            [{"scope": "secondary_display:x", "author": "operator-a",
              "expires_at": FUTURE}],
            primary_metric=None,
        )


# ── Gates: effect direction ──────────────────────────────────────────


def _regression_pair():
    """Build a baseline and a candidate with a clear paired regression."""
    items = [f"item-{index}" for index in range(12)]
    attempts = []
    for arm_id in ("left", "right"):
        for item in items:
            identifier = f"{arm_id}-{item}"
            attempts.append(_attempt(identifier, arm_id, item, 1, 0))
    arms = [
        {"id": "arm-left", "name": "Left", "slug": "left",
         "runtime_id": "classic", "configuration": {},
         "configuration_checksum": "arm-checksum"},
        {"id": "arm-right", "name": "Right", "slug": "right",
         "runtime_id": "classic", "configuration": {},
         "configuration_checksum": "arm-checksum"},
    ]

    def scored(right_scores):
        rows = []
        right_index = 0
        for attempt in attempts:
            if attempt["arm_id"] == "left":
                value = 1.0
            else:
                value = right_scores[right_index % len(right_scores)]
                right_index += 1
            rows.append(_score(attempt["id"], value))
        return rows

    baseline = _gate_run(
        "baseline", attempts=list(attempts), scores=scored([1.0]),
        arms=arms,
        configuration={"repetitions": 1, "practical_difference": 0.05},
    )
    # Varied regression magnitudes keep the bootstrap interval real
    # while every paired delta stays clearly negative.
    candidate = _gate_run(
        "candidate", attempts=list(attempts),
        scores=scored([0.0, 0.0, 0.0, 0.0, 0.2, 0.1]), arms=arms,
        configuration={"repetitions": 1, "practical_difference": 0.05},
    )
    return baseline, candidate


def test_a_clear_regression_cannot_pass_an_improvement_rule():
    baseline, candidate = _regression_pair()
    # The threshold alone would pass, but the corrected analysis shows
    # a meaningful regression, so the direction guard fails the rule.
    rules = [{
        "id": "paired-improvement",
        "metric": "comparison.left.right.score.scorer-exact",
        "operator": "gte",
        "value": -1.0,
        "direction": "improvement",
    }]
    report = evaluate_gate(baseline, candidate, rules)
    assert report["rules"][0]["classification"] == "meaningful_regression"
    assert report["rules"][0]["status"] == "failed"
    assert report["rules"][0]["direction_guard"]
    assert report["status"] == "failed"


def test_comparison_rules_require_direction_and_corrected_significance():
    with pytest.raises(ValueError, match="effect direction"):
        validate_rules([{
            "id": "paired",
            "metric": "comparison.left.right.score.scorer-exact",
            "operator": "gte",
            "value": 0.0,
        }])
    with pytest.raises(ValueError, match="corrected"):
        validate_rules([{
            "id": "raw-p",
            "metric": "comparison.left.right.score.scorer-exact.p_value_raw",
            "operator": "lte",
            "value": 0.05,
            "direction": "improvement",
        }])


# ── Status and aggregates (database-backed) ──────────────────────────


@pytest_asyncio.fixture
async def repairs_db(tmp_path, monkeypatch):
    database_path = str(tmp_path / "repairs.db")
    monkeypatch.setattr(db, "DB_PATH", database_path)
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-one",
        version_id="version-one",
        name="Dataset one",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="dataset-checksum",
        schema={"version": "1"},
        source_filename="one.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="source-checksum",
        source_path="/tmp/one.jsonl",
        version_metadata={},
        items=[{
            "id": "item-one",
            "item_key": "one",
            "input": "What is 20 plus 22?",
            "expected_output": "42",
            "subject": "math",
            "split": "test",
            "tags": [],
            "metadata": {},
        }],
    )
    envelope = {
        "schema_version": "1",
        "runtime_id": "classic",
        "submission_overrides": {},
        "effective_configuration": {"max_rounds": 2},
    }
    await repository.create_test_revision(
        test_id="test-one",
        revision_id="revision-one",
        name="Test one",
        description="",
        dataset_version_id="version-one",
        configuration={
            "schema_version": "1",
            "repetitions": 2,
            "seed": 50,
            "max_concurrency": 1,
            "timeout_seconds": 60,
            "cost_limit_usd": None,
        },
        arms=[{
            "id": "arm-one",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-gsm8k-numeric-v1", "configuration": {}}],
    )
    return database_path


async def _drive_run(
    database_path,
    run_id,
    *,
    score_status="scored",
    include_failed_retry=False,
):
    """Complete one run directly, with optional failures and retries."""
    async with aiosqlite.connect(database_path) as connection:
        connection.row_factory = aiosqlite.Row
        attempt_rows = await connection.execute_fetchall(
            "SELECT attempt.id, attempt.trial_id, attempt.repeat_index "
            "FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ? ORDER BY attempt.repeat_index",
            (run_id,),
        )
        for index, row in enumerate(attempt_rows):
            attempt_id = str(row["id"])
            if include_failed_retry and index == 0:
                # The first slot fails once, then a retry completes, so
                # the failed attempt and its cost stay in the totals.
                failed_task = f"task-{run_id}-{index}-failed"
                await connection.execute(
                    "INSERT INTO tasks (id, label, full_input, status, "
                    "terminal_kind, result_summary, total_cost_usd, "
                    "total_tokens, duration_ms) VALUES (?, 'Benchmark', "
                    "'Question', 'failed', 'failed', NULL, 0.02, 50, 500)",
                    (failed_task,),
                )
                await connection.execute(
                    "UPDATE benchmark_attempts SET status = 'failed', "
                    "failure_category = 'execution', task_id = ? "
                    "WHERE id = ?",
                    (failed_task, attempt_id),
                )
                retry_id = f"{attempt_id}-retry"
                await connection.execute(
                    "INSERT INTO benchmark_attempts (id, trial_id, "
                    "attempt_number, repeat_index, retry_index, status, "
                    "execution_snapshot, snapshot_checksum, random_seed) "
                    "VALUES (?, ?, 99, ?, 1, 'queued', '{}', ?, 1)",
                    (
                        retry_id,
                        row["trial_id"],
                        row["repeat_index"],
                        f"retry-{retry_id}",
                    ),
                )
                attempt_id = retry_id
            task_id = f"task-{run_id}-{index}"
            await connection.execute(
                "INSERT INTO tasks (id, label, full_input, status, "
                "terminal_kind, result_summary, total_cost_usd, "
                "total_tokens, duration_ms) VALUES (?, 'Benchmark', "
                "'Question', 'completed', 'completed', '42', 0.01, 100, "
                "1000)",
                (task_id,),
            )
            await connection.execute(
                "UPDATE benchmark_attempts SET status = 'completed', "
                "task_id = ? WHERE id = ?",
                (task_id, attempt_id),
            )
            await connection.execute(
                "INSERT INTO benchmark_scores (id, attempt_id, scorer_id, "
                "status, score, passed, evidence) VALUES (?, ?, "
                "'scorer-gsm8k-numeric-v1', ?, ?, ?, '{}')",
                (
                    f"score-{run_id}-{index}",
                    attempt_id,
                    score_status,
                    1.0 if score_status == "scored" else None,
                    1 if score_status == "scored" else None,
                ),
            )
        await connection.execute(
            "UPDATE benchmark_trials SET status = 'completed' "
            "WHERE run_id = ?",
            (run_id,),
        )
        await connection.commit()
        last_attempt = await connection.execute(
            "SELECT attempt.id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ? LIMIT 1",
            (run_id,),
        )
        attempt_row = await last_attempt.fetchone()
    await repository.refresh_run_for_attempt(str(attempt_row["id"]))


@pytest.mark.asyncio
async def test_required_scorer_failure_blocks_valid_analysis(repairs_db):
    await repository.create_run(
        run_id="run-one", revision_id="revision-one", idempotency_key=None,
    )
    await _drive_run(repairs_db, "run-one", score_status="error")
    run = await repository.get_run("run-one")
    # Execution completed; scoring failed; no valid analysis exists.
    assert run["status"] == "completed"
    assert run["scoring_status"] == "failed"
    assert run["analysis_status"] == "blocked"
    report = build_run_report(run)
    assert report["analysis_valid"] is False
    assert report["scoring"]["analysis_status"] == "blocked"
    assert any("required scorer failed" in warning
               for warning in report["warnings"])
    # The run list exposes the same failed scoring state.
    runs, _total = await repository.list_runs()
    listed = next(item for item in runs if item["id"] == "run-one")
    assert listed["scoring_status"] == "failed"
    assert listed["analysis_status"] == "blocked"
    # A blocked analysis can never become a baseline.
    with pytest.raises(repository.BenchmarkConflict):
        await records.create_baseline(
            baseline_id="baseline-blocked",
            run_id="run-one",
            name="Blocked",
            description="",
            rules=[{
                "id": "score",
                "metric": "arm.classic.score.scorer-gsm8k-numeric-v1",
                "operator": "gte",
                "value": 0.5,
            }],
            created_by="tester",
        )


@pytest.mark.asyncio
async def test_run_list_and_detail_cost_totals_match(repairs_db):
    await repository.create_run(
        run_id="run-one", revision_id="revision-one", idempotency_key=None,
    )
    await _drive_run(repairs_db, "run-one", include_failed_retry=True)
    run = await repository.get_run("run-one")
    runs, _total = await repository.list_runs()
    listed = next(item for item in runs if item["id"] == "run-one")
    # The list and the detail use one shared cost aggregation.
    assert listed["total_cost_usd"] == pytest.approx(run["total_cost_usd"])
    assert run["aggregates"]["total_cost_usd"] == pytest.approx(
        listed["total_cost_usd"],
    )
    # The total includes the failed attempt and the retry: two
    # completed slots at 0.01 each plus one failed attempt at 0.02.
    assert listed["total_cost_usd"] == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_run_list_shows_the_server_primary_metric(repairs_db):
    await repository.create_run(
        run_id="run-one", revision_id="revision-one", idempotency_key=None,
    )
    await _drive_run(repairs_db, "run-one")
    runs, _total = await repository.list_runs()
    listed = next(item for item in runs if item["id"] == "run-one")
    assert listed["primary_scorer_id"] == "scorer-gsm8k-numeric-v1"
    assert listed["primary_metric_mean"] == pytest.approx(1.0)
    assert int(listed["primary_metric_count"]) == 2
    # The detail exposes the same named metric with no cross-scorer
    # average anywhere in the aggregates.
    run = await repository.get_run("run-one")
    aggregates = run["aggregates"]
    assert aggregates["primary_metric"]["scorer_id"] == (
        "scorer-gsm8k-numeric-v1"
    )
    assert aggregates["primary_metric"]["mean"] == pytest.approx(1.0)
    assert aggregates["secondary_metrics"] == []
    assert "average_score" not in aggregates


@pytest.mark.asyncio
async def test_execution_completion_stays_a_separate_state(repairs_db):
    await repository.create_run(
        run_id="run-one", revision_id="revision-one", idempotency_key=None,
    )
    await _drive_run(repairs_db, "run-one")
    run = await repository.get_run("run-one")
    assert run["status"] == "completed"
    assert run["scoring_status"] == "completed"
    assert run["analysis_status"] == "valid"


@pytest.mark.asyncio
async def test_an_optional_scorer_failure_never_blocks_analysis(repairs_db):
    envelope = {
        "schema_version": "1",
        "runtime_id": "classic",
        "submission_overrides": {},
        "effective_configuration": {"max_rounds": 2},
    }
    await repository.create_test_revision(
        test_id="test-optional",
        revision_id="revision-optional",
        name="Optional scorer test",
        description="",
        dataset_version_id="version-one",
        configuration={
            "schema_version": "1",
            "repetitions": 2,
            "seed": 50,
            "max_concurrency": 1,
            "timeout_seconds": 60,
            "cost_limit_usd": None,
        },
        arms=[{
            "id": "arm-optional",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{
            "id": "scorer-gsm8k-numeric-v1",
            "configuration": {},
            "required": False,
        }],
    )
    await repository.create_run(
        run_id="run-optional",
        revision_id="revision-optional",
        idempotency_key=None,
    )
    await _drive_run(repairs_db, "run-optional", score_status="error")
    run = await repository.get_run("run-optional")
    # The only scorer failed, but it is optional, so scoring completes
    # and the analysis stays valid.
    assert run["status"] == "completed"
    assert run["scoring_status"] == "completed"
    assert run["analysis_status"] == "valid"


# ── Denominators (frozen rules) ──────────────────────────────────────


def _denominator_run():
    """Mix successes, agent failures, timeouts, and infrastructure."""
    attempts = [
        _attempt("a-success", "left", "one", 1, 0),
        _attempt("a-agent", "left", "two", 1, 0, "failed", "execution"),
        _attempt("a-timeout", "left", "three", 1, 0, "failed", "timeout"),
        _attempt(
            "a-budget", "left", "four", 1, 0, "failed", "budget_stop",
        ),
        _attempt(
            "a-infra", "left", "five", 1, 0, "failed", "infrastructure",
        ),
        # One retried slot: the first substantive execution failure
        # seals the slot, so the prohibited retry cannot replace it.
        # Both attempts stay in the resource totals.
        _attempt("a-retried-old", "left", "six", 1, 0, "failed",
                 "execution", cost=0.02),
        _attempt("a-retried-new", "left", "six", 1, 1),
    ]
    scores = [
        _score("a-success", 1.0),
        _score("a-retried-new", 1.0),
        _score("a-agent", None, status="excluded"),
        _score("a-timeout", None, status="excluded"),
        _score("a-budget", None, status="excluded"),
        _score("a-infra", None, status="excluded"),
    ]
    return _gate_run(
        "run-denominators",
        attempts=attempts,
        scores=scores,
        configuration={
            "repetitions": 1,
            "infrastructure_exclusions": {
                "categories": ["infrastructure"],
                "reason": "provider outage window",
            },
        },
    )


def test_unconditional_and_conditional_denominators_separate():
    report = build_run_report(_denominator_run())
    arm = report["arms"][0]
    denominators = arm["denominators"]
    # Six planned slots; one predeclared infrastructure exclusion.
    assert denominators["planned"] == 6
    assert denominators["excluded"] == 1
    assert denominators["unconditional_denominator"] == 5
    assert denominators["completed"] == 1
    # Agent failure, timeout, budget stop, and the sealed substantive
    # failure stay failures with zero success; only the predeclared
    # infrastructure slot left.
    assert denominators["failed"] == 4
    scorer = arm["scorers"][0]
    assert scorer["unconditional_success_rate"] == pytest.approx(1 / 5)
    assert scorer["conditional_success_rate"] == pytest.approx(1.0)
    # Every exclusion carries its category and the policy reason.
    assert denominators["exclusions"] == [{
        "dataset_item_id": "five",
        "repeat_index": 1,
        "category": "infrastructure",
        "reason": "provider outage window",
    }]
    assert report["denominators"]["policy"]["excluded_categories"] == [
        "infrastructure",
    ]


def test_only_a_predeclared_infrastructure_failure_can_be_excluded():
    run = _denominator_run()
    run["test_configuration"]["infrastructure_exclusions"] = {}
    report = build_run_report(run)
    denominators = report["arms"][0]["denominators"]
    # Without the predeclared policy, no slot leaves the denominator.
    assert denominators["excluded"] == 0
    assert denominators["unconditional_denominator"] == 6
    assert denominators["failed"] == 5
    scorer = report["arms"][0]["scorers"][0]
    assert scorer["unconditional_success_rate"] == pytest.approx(1 / 6)


def test_resource_totals_count_all_attempts_and_retries():
    report = build_run_report(_denominator_run())
    totals = report["arms"][0]["resource_totals"]
    # Seven attempt rows exist: six first tries plus one retry.
    assert totals["attempt_count"] == 7
    assert totals["cost_usd"] == pytest.approx(0.01 * 6 + 0.02)
    assert totals["tokens"] == 700


def test_reports_show_planned_admitted_missing_and_excluded():
    report = build_run_report(_denominator_run())
    summary = report["denominators"]
    assert summary["planned"] == 6
    assert summary["admitted"] == 6
    assert summary["missing"] == 0
    assert summary["excluded"] == 1
    assert summary["completed"] == 1
    assert summary["failed"] == 4
