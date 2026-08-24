"""Long-horizon deliberation tests: effort profiles, grace verification,
semantic stall, the task ledger, cleaner aging, and payload context modes.
"""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.board_store import InMemoryBoardStore
from core.entry import BoardEntry
from core.variants.effort import (
    CLASSIC_EFFORT_PROFILES,
    apply_effort_profile,
    resolve_effort,
)
from core.variants.traditional import (
    STALL_SIMILARITY,
    TraditionalVariant,
    _round_token_set,
    _token_jaccard,
)

# ── Helpers (mirrors test_traditional_guards) ────────────────────────

def _make_entry(
    eid: str, etype: str, author: str, body: str,
    status: str = "open", refs: list[str] | None = None,
    round_no: int = 0, confidence: float = 0.8,
    salience: float = 0.5,
) -> BoardEntry:
    return BoardEntry(
        id=eid,
        task_id="test-task",
        type=etype,
        author=author,
        body=body,
        title=body[:80],
        refs=refs or [],
        confidence=confidence,
        status=status,
        salience=salience,
        round=round_no,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def _make_variant(**overrides) -> TraditionalVariant:
    config = {
        "max_rounds": overrides.pop("max_rounds", 4),
        "max_duration_s": overrides.pop("max_duration_s", 1800),
        "budget_ceiling_usd": overrides.pop("budget_ceiling_usd", 0.50),
        "max_concurrent_activations": overrides.pop("max_concurrent_activations", 3),
        "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
        "stall_rounds": overrides.pop("stall_rounds", 2),
        "cu_mode": "llm",
        "sole_similarity": "auto",
        "grace_verification": overrides.pop("grace_verification", True),
        "actor_context": overrides.pop("actor_context", "chained"),
        "require_evidence": overrides.pop("require_evidence", False),
    }
    registry = overrides.pop("role_registry", {
        role: {"profile": role, "endpoints": ["http://node-a:8000"], "enabled": True}
        for role in ("planner", "expert", "critic", "cleaner", "decider")
    })
    variant = TraditionalVariant(
        gateway=AsyncMock(),
        board_store=None,
        event_emitter=None,
        triage=None,
        config=config,
        litellm_url="",
        litellm_key="",
        node_endpoints=["http://node-a:8000"],
        role_registry=registry,
        model_routing={},
    )
    variant.store = InMemoryBoardStore()
    variant.genesis_time = time.monotonic()
    return variant


def _seed(variant: TraditionalVariant, task_id: str, entries: list[BoardEntry]) -> None:
    variant.store._entries[task_id] = {entry.id: entry for entry in entries}


# ── Effort profiles ──────────────────────────────────────────────────

class TestEffortProfiles:

    def test_resolve_accepts_known_levels_and_default(self):
        assert resolve_effort(None) == "standard"
        assert resolve_effort("Thorough") == "thorough"
        with pytest.raises(ValueError):
            resolve_effort("hyperdrive")

    def test_standard_changes_nothing(self):
        base = {"max_rounds": 7, "budget_ceiling_usd": 1.25}
        merged = apply_effort_profile(base, CLASSIC_EFFORT_PROFILES, "standard")
        assert merged == base

    def test_thorough_layers_over_session_settings(self):
        base = {"max_rounds": 4, "budget_ceiling_usd": 0.5, "cu_mode": "llm"}
        merged = apply_effort_profile(base, CLASSIC_EFFORT_PROFILES, "thorough")
        assert merged["max_rounds"] == 12
        assert merged["actor_context"] == "fresh"
        assert merged["cu_mode"] == "llm"  # untouched keys survive

    @pytest.mark.asyncio
    async def test_capture_records_effort_and_applies_profile(self):
        from core.variants.classic import ClassicVariantRuntime
        configuration = await ClassicVariantRuntime.capture_configuration(
            {"effort": "exhaustive"},
        )
        classic = configuration["settings"]["classic"]
        assert configuration["effort"] == "exhaustive"
        assert classic["max_rounds"] == 32
        assert classic["grace_verification"] is True
        assert classic["actor_context"] == "fresh"

    @pytest.mark.asyncio
    async def test_explicit_classic_override_beats_profile(self):
        from core.variants.classic import ClassicVariantRuntime
        configuration = await ClassicVariantRuntime.capture_configuration(
            {"effort": "thorough", "classic": {"max_rounds": 6}},
        )
        assert configuration["settings"]["classic"]["max_rounds"] == 6

    @pytest.mark.asyncio
    async def test_unknown_effort_is_rejected(self):
        from core.variants import VariantConfigurationError
        from core.variants.classic import ClassicVariantRuntime
        with pytest.raises(VariantConfigurationError):
            await ClassicVariantRuntime.capture_configuration({"effort": "warp"})

    def test_capability_document_advertises_profiles(self):
        from core.variants.classic import ClassicVariantRuntime
        record = ClassicVariantRuntime.descriptor.to_dict()
        profiles = record["effort_profiles"]
        assert set(profiles) == {"quick", "standard", "thorough", "exhaustive"}
        assert profiles["thorough"]["settings"]["max_rounds"] == 12
        assert profiles["standard"]["settings"] == {}


# ── Grace verification ───────────────────────────────────────────────

class TestGraceVerification:

    @pytest.mark.asyncio
    async def test_forced_decider_solution_gets_one_critic_round(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(task_id, round=5, decider_forced=True, terminal_reason="max_rounds")
        _seed(v, task_id, [
            _make_entry("e-1", "objective", "control_unit", "Objective"),
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
        ])
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is False
        assert [a.actor for a in result.activations] == ["critic"]
        assert result.selection_source == "grace_verification"
        v.gateway.set_meta.assert_any_call(
            task_id, grace_verification_done=True, solution_candidate_id="e-9",
        )

    @pytest.mark.asyncio
    async def test_approved_grace_solution_terminates_verified(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=6, decider_forced=True,
            terminal_reason="max_rounds", grace_verification_done=True,
            solution_reviewed_id="e-9",
        )
        _seed(v, task_id, [
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
        ])
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True
        assert result.reason == "solution"

    @pytest.mark.asyncio
    async def test_unapproved_grace_terminates_with_original_reason(self):
        # Once the review and the single revision chance are both spent,
        # an unapproved answer stops with the original limit reason.
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=6, decider_forced=True,
            terminal_reason="max_rounds", grace_verification_done=True,
            solution_candidate_id="e-9", grace_revision_done=True,
        )
        _seed(v, task_id, [
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
            _make_entry("e-10", "critique", "critic", "Wrong", refs=["e-9"], round_no=6),
        ])
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True
        assert result.reason == "max_rounds"

    @pytest.mark.asyncio
    async def test_grace_disabled_terminates_immediately(self):
        v = _make_variant(grace_verification=False)
        task_id = "test-task"
        await v.store.set_meta(task_id, round=5, decider_forced=True, terminal_reason="budget")
        _seed(v, task_id, [
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
        ])
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True
        assert result.reason == "budget"

    @pytest.mark.asyncio
    async def test_grace_skipped_when_critic_disabled(self):
        registry = {
            role: {"profile": role, "endpoints": ["http://node-a:8000"], "enabled": role != "critic"}
            for role in ("planner", "expert", "critic", "decider")
        }
        v = _make_variant(role_registry=registry)
        task_id = "test-task"
        await v.store.set_meta(task_id, round=5, decider_forced=True, terminal_reason="max_rounds")
        _seed(v, task_id, [
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
        ])
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True


# ── Semantic stall ───────────────────────────────────────────────────

class TestSemanticStall:

    def test_paraphrased_round_counts_as_stall(self):
        v = _make_variant(stall_rounds=2)
        first = {"e-1": _make_entry(
            "e-1", "finding", "expert.a",
            "The database migration requires a maintenance window because the "
            "index rebuild locks the primary table during the copy phase.",
            round_no=1,
        )}
        v._is_stalled(first, current_round=2)
        assert v._stall_counter == 0
        paraphrase = {"e-2": _make_entry(
            "e-2", "finding", "expert.b",
            "Because the index rebuild locks the primary table during the "
            "copy phase, the database migration requires a maintenance window.",
            round_no=2,
        )}
        v._is_stalled(paraphrase, current_round=3)
        assert v._stall_counter == 1

    def test_new_information_resets_the_counter(self):
        v = _make_variant(stall_rounds=2)
        v._is_stalled({"e-1": _make_entry("e-1", "finding", "a", "alpha constraint holds tightly", round_no=1)}, 2)
        fresh = {"e-2": _make_entry(
            "e-2", "finding", "b",
            "Completely different topic: caching strategy for the gateway tier with eviction windows.",
            round_no=2,
        )}
        v._is_stalled(fresh, current_round=3)
        assert v._stall_counter == 0

    def test_token_helpers(self):
        entries = [_make_entry("e-1", "finding", "a", "Alpha beta gamma delta")]
        tokens = _round_token_set(entries)
        assert "alpha" in tokens and "beta" in tokens
        assert _token_jaccard(tokens, tokens) == 1.0
        assert _token_jaccard(tokens, frozenset()) == 0.0
        assert STALL_SIMILARITY > 0.5

    @pytest.mark.asyncio
    async def test_stall_state_round_trips_through_checkpoint(self):
        v = _make_variant()
        task_id = "test-task"
        v._round_token_sets = [frozenset({"alpha", "beta"}), frozenset({"gamma"})]
        v._round_hashes = ["abc"]
        v._stall_counter = 1
        v.gateway = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
        await v.checkpoint(task_id)
        saved = v.gateway.set_meta.call_args.kwargs
        assert saved["round_token_sets"] == [["alpha", "beta"], ["gamma"]]

        restored = _make_variant()
        await restored.store.set_meta(task_id, **{
            "round_hashes": saved["round_hashes"],
            "round_token_sets": saved["round_token_sets"],
            "stall_counter": saved["stall_counter"],
            "replan_count": saved["replan_count"],
            "genesis_started_at": saved["genesis_started_at"],
        })
        await restored.resume({"task_id": task_id, "query": "Q"})
        assert restored._round_token_sets == [frozenset({"alpha", "beta"}), frozenset({"gamma"})]
        assert restored._stall_counter == 1


# ── Control-unit progress block ──────────────────────────────────────

class TestCuProgress:

    def test_prompt_contains_progress_and_constraints(self):
        v = _make_variant()
        v.budget_spent = 0.1
        v._stall_counter = 1
        snapshot = {
            "e-1": _make_entry("e-1", "critique", "critic", "Unresolved point", round_no=2),
        }
        meta = {"progress_ledger": [{"round": 2, "entries_added": 3}]}
        prompt = v._cu_prompt("Objective", "(board)", "(roster)", 3, snapshot, meta)
        assert "## Progress" in prompt
        assert "Last round added 3 entries" in prompt
        assert "1 unresolved critiques" in prompt
        assert "Stall counter: 1/2" in prompt
        assert "BUDGET PRESSURE" not in prompt

    def test_prompt_warns_under_budget_pressure(self):
        v = _make_variant(budget_ceiling_usd=1.0)
        v.budget_spent = 0.85
        prompt = v._cu_prompt("Objective", "(board)", "(roster)", 3, {}, {})
        assert "BUDGET PRESSURE" in prompt


# ── Task ledger ──────────────────────────────────────────────────────

class TestTaskLedger:

    @pytest.mark.asyncio
    async def test_new_ledger_supersedes_previous(self):
        from core.event_emitter import InMemoryEventEmitter
        from core.gateway import BoardGateway
        store = InMemoryBoardStore()
        gateway = BoardGateway(store, InMemoryEventEmitter())
        v = _make_variant()
        v.store = store
        v.gateway = gateway
        task_id = "test-task"
        task = {"task_id": task_id, "query": "Q"}

        first = await v.apply(task, [{
            "actor": "planner",
            "entries": [{"type": "ledger", "title": "Task Ledger", "body": "v1", "confidence": 0.9}],
            "turn_id": "turn-1", "round": 1,
        }])
        assert len(first) == 1
        second = await v.apply(task, [{
            "actor": "planner",
            "entries": [{"type": "ledger", "title": "Task Ledger", "body": "v2", "confidence": 0.9}],
            "turn_id": "turn-2", "round": 2,
        }])
        assert len(second) == 1
        snapshot = await store.get_snapshot(task_id)
        ledgers = sorted(
            (entry for entry in snapshot.values() if entry.type == "ledger"),
            key=lambda entry: entry.round,
        )
        assert [entry.status for entry in ledgers] == ["superseded", "open"]
        assert ledgers[-1].body == "v2"

    def test_serialization_pins_the_ledger(self):
        v = _make_variant()
        v.view_budget_tokens = 512
        entries = [
            _make_entry("e-1", "objective", "control_unit", "Objective body"),
            _make_entry("e-2", "ledger", "planner", "Ledger body with plan status", round_no=3),
        ]
        entries += [
            _make_entry(f"e-{i}", "finding", "expert.x", f"Filler finding number {i} " + "words " * 40, round_no=2)
            for i in range(3, 30)
        ]
        board = {entry.id: entry for entry in entries}
        data = v._serialize_board(board, actor="expert.x")
        included = {item["id"] for item in data["entries"]}
        assert "e-2" in included and "e-1" in included

    def test_cleaner_protects_ledger_and_ages_out_old_plans(self):
        v = _make_variant()
        board = {
            "e-1": _make_entry("e-1", "ledger", "planner", "Ledger", round_no=6),
            "e-2": _make_entry("e-2", "plan", "planner", "Ancient plan", round_no=1, salience=0.0),
            "e-3": _make_entry("e-3", "plan", "planner", "Recent plan", round_no=6, salience=0.0),
            "e-4": _make_entry("e-4", "critique", "critic", "Ancient critique", round_no=1, salience=0.0),
            "e-5": _make_entry("e-5", "finding", "expert.x", "Low value finding", round_no=1, salience=0.0),
        }
        candidates = {entry.id for entry in v._get_eviction_candidates(board)}
        assert "e-1" not in candidates
        assert "e-3" not in candidates
        assert "e-2" in candidates
        assert "e-4" in candidates
        assert "e-5" in candidates


# ── Payload context modes and duration reserve ───────────────────────

class TestPayloadAndDuration:

    def _payload(self, v: TraditionalVariant) -> dict:
        from core.variants.traditional import CONSTANT_ROLE_DESCRIPTIONS, AgentRoster
        v.roster = AgentRoster(constants=dict(CONSTANT_ROLE_DESCRIPTIONS), experts=[])
        board = {"e-1": _make_entry("e-1", "objective", "cu", "Objective")}
        return v.build_turn_payload({"task_id": "test-task", "query": "Q"}, "critic", board)

    def test_chained_context_keeps_response_chaining(self):
        v = _make_variant(actor_context="chained")
        v._response_ids["critic"] = "resp-1"
        payload = self._payload(v)
        assert payload["previous_response_id"] == "resp-1"

    def test_fresh_context_drops_response_chaining(self):
        v = _make_variant(actor_context="fresh")
        v._response_ids["critic"] = "resp-1"
        payload = self._payload(v)
        assert payload["previous_response_id"] is None

    def test_budget_pressure_adds_convergence_directive(self):
        v = _make_variant(budget_ceiling_usd=1.0)
        v.budget_spent = 0.9
        payload = self._payload(v)
        assert "Converge now" in payload["budget_status"]
        calm = _make_variant(budget_ceiling_usd=1.0)
        calm.budget_spent = 0.1
        assert "budget_status" not in self._payload(calm)

    @pytest.mark.asyncio
    async def test_duration_guard_reserves_time_for_the_decider(self):
        v = _make_variant(max_duration_s=1000)
        task_id = "test-task"
        await v.store.set_meta(task_id, round=0, budget_spent=0.0)
        # 1000s cap, 80s reserve → the guard fires at 920s of elapsed time.
        v.genesis_time = time.monotonic() - 930
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is False
        assert [a.actor for a in result.activations] == ["decider"]

    def test_reserve_scales_with_duration(self):
        assert _make_variant(max_duration_s=600)._duration_reserve_s == 48
        assert _make_variant(max_duration_s=10800)._duration_reserve_s == 180


# ── Benchmark scheduler timeout alignment ────────────────────────────

class TestSchedulerTimeout:

    def test_default_stays(self):
        from benchmarks.scheduler import _attempt_timeout_seconds
        assert _attempt_timeout_seconds({"timeout_seconds": 3600}) == 3600

    def test_arm_duration_extends_timeout(self):
        from benchmarks.scheduler import _attempt_timeout_seconds
        configuration = {
            "timeout_seconds": 3600,
            "arms": [{"configuration": {"classic": {"max_duration_s": 7200}}}],
        }
        assert _attempt_timeout_seconds(configuration) == 7500

    def test_effort_level_extends_timeout(self):
        from benchmarks.scheduler import _attempt_timeout_seconds
        configuration = {
            "timeout_seconds": 3600,
            "arms": [{"configuration": {"effort": "exhaustive"}}],
        }
        assert _attempt_timeout_seconds(configuration) == 11100


# ── Submit models ────────────────────────────────────────────────────

class TestSubmitModels:

    def test_submission_accepts_effort_and_classic(self):
        from routes.submit import TaskOverrides, TaskSubmission
        submission = TaskSubmission.model_validate({
            "task": "Do it",
            "effort": "thorough",
            "overrides": {"classic": {"max_rounds": 8}},
        })
        assert submission.effort == "thorough"
        assert submission.overrides.classic == {"max_rounds": 8}
        assert TaskOverrides.model_validate({"classic": {"budget_ceiling_usd": 2}}).classic

    def test_submission_rejects_bad_effort_shape(self):
        from pydantic import ValidationError

        from routes.submit import TaskSubmission
        with pytest.raises(ValidationError):
            TaskSubmission.model_validate({"task": "Do it", "effort": "Not A Level!"})


# ── Full-loop integration through the lifecycle harness ──────────────

class TestLongHorizonLifecycle:

    def _grace_harness(self, max_rounds: int):
        from classic_harness import TASK_ID, ClassicLifecycleHarness
        harness = ClassicLifecycleHarness("concurrent")
        harness.variant.max_rounds = max_rounds
        harness.variant.grace_verification = True
        original_response = harness.worker._response

        def scripted_response(actor, board, private):
            payload = original_response(actor, board, private)
            if actor == "planner":
                import json as json_module
                decoded = json_module.loads(payload["result"])
                decoded["entries"].append({
                    "type": "ledger",
                    "title": "Task Ledger",
                    "body": "Verified facts: none yet. Open questions: both constraints. Plan status: active.",
                    "confidence": 0.9,
                })
                payload["result"] = json_module.dumps(decoded)
            return payload

        harness.worker._response = scripted_response
        return harness, TASK_ID

    @pytest.mark.asyncio
    async def test_limit_stop_ends_verified_through_grace(self):
        """max_rounds forces the decider, grace routes the critic, and the
        task finishes as a verified solution instead of an unverified stop."""
        harness, task_id = self._grace_harness(max_rounds=2)

        async def schedule(task_id_, query, snapshot, current_round, meta):
            return {
                1: ["planner", "expert.alpha"],
                2: ["expert.beta"],
            }.get(current_round, ["decider"]), "long-horizon schedule"

        harness.variant._cu_select = schedule
        run = await harness.run()

        assert run.result["terminated_by"] == "solution"
        assert run.result["answer_source"] == "decider"
        assert run.result["verification_status"] == "critic_reviewed"
        meta = await harness.store.get_meta(task_id)
        assert meta.get("grace_verification_done") is True
        assert meta.get("terminal_reason") == "max_rounds"
        snapshot = await harness.store.get_snapshot(task_id)
        ledgers = [e for e in snapshot.values() if e.type == "ledger"]
        assert len(ledgers) == 1 and ledgers[0].status == "open"

    @pytest.mark.asyncio
    async def test_paraphrase_stall_replans_then_ends_verified(self):
        """Paraphrased expert rounds trip the semantic stall, the planner
        replans (superseding its ledger), and the stalled stop still ends
        with a critic-verified solution through grace."""
        import json as json_module

        from classic_harness import TASK_ID, ClassicLifecycleHarness
        harness = ClassicLifecycleHarness("concurrent")
        harness.variant.max_rounds = 8
        harness.variant.stall_rounds = 1
        harness.variant.max_replans = 1
        harness.variant.grace_verification = True
        original_response = harness.worker._response

        base_sentence = (
            "The reconciliation depends on the alpha constraint because the "
            "final value must remain forty-two across every validation pass."
        )

        def scripted_response(actor, board, private):
            if actor.startswith("expert."):
                harness.worker._actor_calls[actor] += 1
                words = base_sentence.split()
                calls = harness.worker._actor_calls[actor]
                rotated = words[calls % 3:] + words[:calls % 3]
                return {
                    "status": "completed",
                    "result": json_module.dumps({"entries": [{
                        "type": "finding",
                        "title": f"Restatement {calls}",
                        "body": " ".join(rotated),
                        "confidence": 0.8,
                    }]}),
                    "usage": {"model": "m", "prompt_tokens": 50, "completion_tokens": 10},
                    "response_id": f"r-{actor}-{calls}",
                    "node_id": "deterministic-node",
                    "duration_ms": 5,
                }
            payload = original_response(actor, board, private)
            if actor == "planner":
                decoded = json_module.loads(payload["result"])
                decoded["entries"].append({
                    "type": "ledger",
                    "title": "Task Ledger",
                    "body": f"Ledger revision after {harness.worker._actor_calls[actor]} plans.",
                    "confidence": 0.9,
                })
                payload["result"] = json_module.dumps(decoded)
            return payload

        harness.worker._response = scripted_response

        async def schedule(task_id_, query, snapshot, current_round, meta):
            if current_round == 1:
                return ["planner"], "paraphrase schedule"
            return ["expert.alpha"], "paraphrase schedule"

        harness.variant._cu_select = schedule
        run = await harness.run()

        meta = await harness.store.get_meta(TASK_ID)
        assert meta.get("replan_count") == 1
        assert meta.get("terminal_reason") == "stalled"
        assert run.result["terminated_by"] == "solution"
        assert run.result["verification_status"] == "critic_reviewed"
        snapshot = await harness.store.get_snapshot(TASK_ID)
        open_ledgers = [
            e for e in snapshot.values()
            if e.type == "ledger" and e.status == "open"
        ]
        superseded_ledgers = [
            e for e in snapshot.values()
            if e.type == "ledger" and e.status == "superseded"
        ]
        assert len(open_ledgers) == 1
        assert len(superseded_ledgers) >= 1


# ── Phase 3: evidence-gated rounds ───────────────────────────────────

class TestEvidenceGating:

    def test_round_lacks_evidence_only_for_unsourced_contributions(self):
        from core.variants.traditional import _round_lacks_evidence
        unsourced = _make_entry("e-1", "finding", "expert.a", "New unsourced claim about tariffs")
        sourced = _make_entry("e-2", "finding", "expert.a", "Grounded claim about tariffs")
        sourced.sources = ["https://example.org/report"]
        plan = _make_entry("e-3", "plan", "planner", "Investigate tariffs")

        assert _round_lacks_evidence([unsourced]) is True
        assert _round_lacks_evidence([unsourced, sourced]) is False
        assert _round_lacks_evidence([plan]) is False
        assert _round_lacks_evidence([]) is False

    def test_unsourced_novel_round_counts_toward_stall(self):
        v = _make_variant(require_evidence=True, stall_rounds=2)
        snapshot = {
            "e-1": _make_entry(
                "e-1", "finding", "expert.a",
                "A completely novel unsourced statement about currency flows",
                round_no=3,
            ),
        }
        assert v._is_stalled(snapshot, 4) is False
        assert v._stall_counter == 1

    def test_sourced_novel_round_resets_the_counter(self):
        v = _make_variant(require_evidence=True, stall_rounds=2)
        v._stall_counter = 1
        entry = _make_entry(
            "e-1", "finding", "expert.a",
            "A completely novel grounded statement about currency flows",
            round_no=3,
        )
        entry.sources = ["https://example.org/data"]
        assert v._is_stalled({"e-1": entry}, 4) is False
        assert v._stall_counter == 0

    def test_without_the_setting_unsourced_novelty_still_resets(self):
        v = _make_variant(require_evidence=False, stall_rounds=2)
        v._stall_counter = 1
        snapshot = {
            "e-1": _make_entry(
                "e-1", "finding", "expert.a",
                "A completely novel unsourced statement about currency flows",
                round_no=3,
            ),
        }
        assert v._is_stalled(snapshot, 4) is False
        assert v._stall_counter == 0

    def test_high_effort_profiles_require_evidence(self):
        for level in ("thorough", "exhaustive"):
            settings = CLASSIC_EFFORT_PROFILES[level]["settings"]
            assert settings["require_evidence"] is True
        assert "require_evidence" not in CLASSIC_EFFORT_PROFILES["quick"]["settings"]

    def test_cu_prompt_reports_evidence_and_requirement(self):
        v = _make_variant(require_evidence=True)
        sourced = _make_entry("e-2", "finding", "expert.a", "Grounded claim", round_no=3)
        sourced.sources = ["https://example.org"]
        snapshot = {
            "e-1": _make_entry("e-1", "finding", "expert.a", "Plain claim", round_no=3),
            "e-2": sourced,
        }
        prompt = v._cu_prompt("Q", "board", "roster", 4, snapshot, {})
        assert "(1 with external sources)" in prompt
        assert "EVIDENCE REQUIRED" in prompt
        relaxed = _make_variant(require_evidence=False)
        assert "EVIDENCE REQUIRED" not in relaxed._cu_prompt("Q", "b", "r", 4, snapshot, {})

    def test_payload_carries_evidence_notice_for_contributors(self):
        v = _make_variant(require_evidence=True)
        task = {"task_id": "test-task", "query": "Q"}
        board = {}
        assert "sources" in v.build_turn_payload(task, "expert.alpha", board)["evidence_status"]
        assert "evidence_status" in v.build_turn_payload(task, "planner", board)
        assert "evidence_status" not in v.build_turn_payload(task, "critic", board)
        relaxed = _make_variant(require_evidence=False)
        assert "evidence_status" not in relaxed.build_turn_payload(task, "expert.alpha", board)

    def test_settings_validator_accepts_and_rejects_require_evidence(self):
        from settings_store import validate_classic_settings
        base = {
            "max_rounds": 4, "max_duration_s": 1800, "budget_ceiling_usd": 0.5,
            "max_concurrent_activations": 3,
            "experts_per_tier": {"simple": 0, "light": 1, "medium": 2, "complex": 3},
            "stall_rounds": 2, "max_replans": 2, "cu_mode": "llm",
            "coordinator_narration": False, "sole_similarity": "auto",
        }
        validated = validate_classic_settings({**base, "require_evidence": True})
        assert validated["require_evidence"] is True
        assert validate_classic_settings(dict(base))["require_evidence"] is False
        with pytest.raises(ValueError, match="require_evidence"):
            validate_classic_settings({**base, "require_evidence": "yes"})

    def test_expert_persona_documents_the_sources_contract(self):
        from models.personas import generate_expert_persona
        persona = generate_expert_persona("Analyst", "Finds facts", "Q")
        assert '"sources"' in persona or "`sources`" in persona


class TestSourcesPipeline:

    def test_gateway_source_normalization(self):
        from core.gateway import _normalize_sources
        assert _normalize_sources(None) == []
        assert _normalize_sources("https://a.example") == ["https://a.example"]
        assert _normalize_sources([" https://a.example ", "", 7, "tool:web_search"]) == [
            "https://a.example", "tool:web_search",
        ]
        many = _normalize_sources([f"https://x.example/{i}" for i in range(20)])
        assert len(many) == 8
        assert len(_normalize_sources(["y" * 2000])[0]) == 500

    def test_parser_preserves_sources_extras(self):
        from core.response_parser import _clean_entry
        cleaned = _clean_entry(
            {
                "type": "finding",
                "title": "Grounded",
                "body": "Claim with citation",
                "sources": ["https://example.org/paper"],
            },
            "expert.alpha",
            None,
        )
        assert cleaned is not None
        assert cleaned["sources"] == ["https://example.org/paper"]

    def test_entry_round_trips_sources(self):
        from core.entry import entry_from_dict, entry_to_dict
        entry = _make_entry("e-1", "finding", "expert.a", "Body")
        entry.sources = ["https://example.org"]
        restored = entry_from_dict(entry_to_dict(entry))
        assert restored.sources == ["https://example.org"]
        assert entry_from_dict({
            "id": "e-2", "task_id": "t", "type": "finding",
            "author": "a", "body": "b", "sources": '["https://x.example"]',
        }).sources == ["https://x.example"]


# ── Phase 3: grace revision (verified-stop as the primary rule) ──────

class TestGraceRevision:

    def _rejected_state(self, v, task_id="test-task", budget_spent=0.05):
        return [
            _make_entry("e-1", "objective", "control_unit", "Objective"),
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
            _make_entry(
                "e-10", "critique", "critic",
                "The answer skips the second constraint",
                refs=["e-9"], round_no=6,
            ),
        ]

    @pytest.mark.asyncio
    async def test_rejected_answer_gets_one_decider_revision(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=7, decider_forced=True, terminal_reason="max_rounds",
            grace_verification_done=True, solution_candidate_id="e-9",
            budget_spent=0.05,
        )
        _seed(v, task_id, self._rejected_state(v))
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is False
        assert [a.actor for a in result.activations] == ["decider"]
        assert result.selection_source == "grace_revision"
        v.gateway.set_meta.assert_any_call(task_id, grace_revision_done=True)

    @pytest.mark.asyncio
    async def test_revision_happens_at_most_once(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=8, decider_forced=True, terminal_reason="max_rounds",
            grace_verification_done=True, solution_candidate_id="e-9",
            grace_revision_done=True, budget_spent=0.05,
        )
        _seed(v, task_id, self._rejected_state(v))
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True
        assert result.reason == "max_rounds"

    @pytest.mark.asyncio
    async def test_no_revision_without_budget_headroom(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=7, decider_forced=True, terminal_reason="budget",
            grace_verification_done=True, solution_candidate_id="e-9",
            budget_spent=0.50,
        )
        _seed(v, task_id, self._rejected_state(v))
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True
        assert result.reason == "budget"

    @pytest.mark.asyncio
    async def test_no_revision_without_a_rejecting_critique(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=7, decider_forced=True, terminal_reason="max_rounds",
            grace_verification_done=True, solution_candidate_id="e-9",
            budget_spent=0.05,
        )
        _seed(v, task_id, [
            _make_entry("e-1", "objective", "control_unit", "Objective"),
            _make_entry("e-9", "solution", "decider", "Final answer", round_no=5),
        ])
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is True
        assert result.reason == "max_rounds"

    @pytest.mark.asyncio
    async def test_revised_solution_gets_a_fresh_grace_review(self):
        v = _make_variant()
        task_id = "test-task"
        await v.store.set_meta(
            task_id, round=8, decider_forced=True, terminal_reason="max_rounds",
            grace_verification_done=True, solution_candidate_id="e-9",
            grace_revision_done=True, budget_spent=0.05,
        )
        entries = self._rejected_state(v)
        entries.append(
            _make_entry("e-11", "solution", "decider", "Revised answer", round_no=8),
        )
        _seed(v, task_id, entries)
        result = await v.step({"task_id": task_id, "query": "Q"}, None)
        assert result.terminal is False
        assert [a.actor for a in result.activations] == ["critic"]
        assert result.selection_source == "grace_verification"
        v.gateway.set_meta.assert_any_call(
            task_id, grace_verification_done=True, solution_candidate_id="e-11",
        )

    @pytest.mark.asyncio
    async def test_full_loop_rejection_revision_then_verified(self):
        """The critic rejects the forced answer, the decider revises once,
        and the second grace review verifies the revision."""
        import json as json_module

        from classic_harness import TASK_ID, ClassicLifecycleHarness
        harness = ClassicLifecycleHarness("concurrent")
        harness.variant.max_rounds = 2
        harness.variant.grace_verification = True
        original_response = harness.worker._response
        critic_calls = {"n": 0}

        def scripted_response(actor, board, private):
            if actor == "critic":
                solution = harness.worker._entry(board, entry_type="solution")
                if solution is not None:
                    critic_calls["n"] += 1
                    if critic_calls["n"] == 1:
                        return {
                            "status": "completed",
                            "result": json_module.dumps({"entries": [{
                                "type": "critique",
                                "title": "Answer skips the beta constraint",
                                "body": "The solution never reconciles beta's claim.",
                                "refs": [solution["id"]],
                                "confidence": 0.9,
                            }]}),
                            "usage": {"model": "m", "prompt_tokens": 40, "completion_tokens": 12},
                            "response_id": "r-critic-reject",
                            "node_id": "deterministic-node",
                            "duration_ms": 5,
                        }
            return original_response(actor, board, private)

        harness.worker._response = scripted_response

        async def schedule(task_id_, query, snapshot, current_round, meta):
            return {
                1: ["planner", "expert.alpha"],
                2: ["expert.beta"],
            }.get(current_round, ["decider"]), "revision schedule"

        harness.variant._cu_select = schedule
        run = await harness.run()

        assert critic_calls["n"] >= 2
        assert run.result["terminated_by"] == "solution"
        assert run.result["answer_source"] == "decider"
        assert run.result["verification_status"] == "critic_reviewed"
        meta = await harness.store.get_meta(TASK_ID)
        assert meta.get("grace_revision_done") is True
        snapshot = await harness.store.get_snapshot(TASK_ID)
        solutions = [e for e in snapshot.values() if e.type == "solution"]
        assert len(solutions) >= 2
