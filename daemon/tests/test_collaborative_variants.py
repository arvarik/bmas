"""Golden contract tests for Patchboard and Stigmergic runtimes."""

from typing import Any

import pytest

from core.triage import Complexity, TriageResult
from core.variants import VariantExecutionRequest, variant_capabilities
from core.variants.collaborative import (
    PatchboardVariantRuntime,
    StigmergicVariantRuntime,
)


class FakeHost:
    def __init__(self, checkpoint: dict[str, Any] | None = None):
        self.checkpoint = checkpoint
        self.dispatches: list[dict[str, Any]] = []
        self.phases: list[tuple[str, int]] = []
        self.progress: list[list[dict[str, Any]]] = []

    async def publish_phase(self, phase, iteration, task_id):
        self.phases.append((phase, iteration))

    async def check_abort(self, task_id):
        return None

    async def log_event(self, node_id, message, task_id, **kwargs):
        return None

    async def dispatch_agent(self, *, task_id, activation_id, **kwargs):
        self.dispatches.append({
            "task_id": task_id,
            "activation_id": activation_id,
            **kwargs,
        })
        return {
            "status": "completed",
            "result": f"output-{len(self.dispatches)}-{kwargs['role']}",
        }

    async def publish_progress(self, task_id, label, status, items):
        self.progress.append(items)

    def task_lease_token(self, task_id):
        return "lease"

    async def load_variant_checkpoint(self, task_id, variant_id):
        return self.checkpoint

    async def save_variant_checkpoint(self, task_id, variant_id, checkpoint):
        self.checkpoint = {**checkpoint, "variant_id": variant_id}


def _request(runtime_id: str, settings: dict[str, Any], *, resume: bool = False):
    return VariantExecutionRequest(
        task_id="task-golden",
        session_id="session-golden",
        user_task="Produce the final answer.",
        triage=TriageResult(Complexity.MEDIUM, "model-medium"),
        resume=resume,
        effective_configuration={
            "variant": runtime_id,
            "configuration_schema_version": "1",
            "settings": {runtime_id: settings},
            "model_routing": {"medium": "model-medium"},
            "role_registry": {
                "planner": {"profile": "planner", "endpoints": ["http://agent"]},
                "critic": {"profile": "critic", "endpoints": ["http://agent"]},
                "decider": {"profile": "decider", "endpoints": ["http://agent"]},
            },
        },
    )


def test_capabilities_register_all_concrete_runtime_contracts():
    variants = {item["id"]: item for item in variant_capabilities()["variants"]}

    assert set(variants) >= {"classic", "patchboard", "stigmergic"}
    assert variants["patchboard"]["supports_recovery"] is True
    assert variants["stigmergic"]["benchmark"]["seed_strategy"] == "recorded"
    assert variants["patchboard"]["features"]["controls"] == ["abort"]
    assert (
        variants["patchboard"]["benchmark"]["configuration_schema"]
        ["properties"]["rounds"]["maximum"]
        == 1
    )


@pytest.mark.asyncio
async def test_patchboard_golden_lifecycle_and_resume():
    request = _request("patchboard", {
        "contributor_roles": ["planner", "critic"],
        "integrator_role": "decider",
        "rounds": 1,
    })
    host = FakeHost()

    outcome = await PatchboardVariantRuntime.run(host, request)

    assert outcome.answer == "output-3-decider"
    assert outcome.result == {
        "answer": "output-3-decider",
        "runtime_id": "patchboard",
        "coordination": "independent_patches_then_integration",
        "steps": 3,
        "contributors": ["planner", "critic"],
        "integrator": "decider",
    }
    assert [item["role"] for item in host.dispatches] == [
        "planner",
        "critic",
        "decider",
    ]
    assert len({item["activation_id"] for item in host.dispatches}) == 3

    resumed = FakeHost(host.checkpoint)
    replay = await PatchboardVariantRuntime.run(
        resumed,
        _request("patchboard", request.effective_configuration["settings"]["patchboard"], resume=True),
    )
    assert replay.answer == outcome.answer
    assert resumed.dispatches == []


@pytest.mark.asyncio
async def test_stigmergic_golden_revision_order():
    host = FakeHost()
    request = _request("stigmergic", {
        "worker_roles": ["planner", "critic"],
        "integrator_role": "decider",
        "rounds": 2,
    })

    outcome = await StigmergicVariantRuntime.run(host, request)

    assert [item["role"] for item in host.dispatches] == [
        "planner",
        "critic",
        "planner",
        "critic",
        "decider",
    ]
    assert outcome.result["coordination"] == "ordered_shared_artifact_revisions"
    assert outcome.result["rounds"] == 2
    assert outcome.result["steps"] == 5
    assert host.checkpoint["step"] == "complete"


def test_collaborative_runtime_rejects_unknown_saved_settings():
    with pytest.raises(ValueError, match="does not support"):
        PatchboardVariantRuntime.configuration_from_metadata({
            "effective_configuration": {
                "variant": "patchboard",
                "configuration_schema_version": "1",
                "settings": {"patchboard": {"unknown": True}},
            }
        })


@pytest.mark.asyncio
async def test_patchboard_rejects_an_unused_extra_round():
    with pytest.raises(ValueError, match="equal 1"):
        await PatchboardVariantRuntime.prepare_benchmark_configuration({
            "contributor_roles": ["planner", "critic"],
            "integrator_role": "decider",
            "rounds": 2,
        })


@pytest.mark.asyncio
async def test_collaborative_preflight_rejects_an_unroutable_role():
    with pytest.raises(ValueError, match="cannot dispatch: unknown-role"):
        await StigmergicVariantRuntime.prepare_benchmark_configuration({
            "worker_roles": ["unknown-role"],
            "integrator_role": "decider",
            "rounds": 1,
            "submission_overrides": {
                "role_registry": {
                    "decider": {"endpoints": ["http://agent"]},
                },
            },
        })
