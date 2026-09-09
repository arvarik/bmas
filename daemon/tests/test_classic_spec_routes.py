"""Preview parity, complete disclosures, field errors, and read-only boundaries."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from test_classic_spec_compiler import golden_deployment

from core.variants import RuntimeKey, require_admissible_runtime
from core.variants.classic.compiler import compile_specification, specification_digest
from core.variants.classic.editor import published_schema
from core.variants.classic.profiles import DEPLOYMENT_CAPS, SCHEMA_DEFAULTS
from core.variants.classic.runtime import specification_input_from
from core.variants.classic.spec import ClassicSpecInput
from routes import classic_spec


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(classic_spec, "BMAS_API_KEY", "preview-key")
    monkeypatch.setattr(classic_spec, "deployment_snapshot", AsyncMock(return_value=golden_deployment()))
    app = FastAPI()
    app.include_router(classic_spec.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer preview-key"}) as client:
        yield client


async def test_schema_publishes_all_controls_and_separate_profiles(client):
    response = await client.get("/classic/spec/schema")
    assert response.status_code == 200
    document = response.json()
    groups = document["x-editor"]["groups"]
    assert [group["name"] for group in groups] == ["Team", "Coordination", "Memory", "Verification", "Limits", "Recovery"]
    assert {control["path"] for group in groups for control in group["controls"]} == set(SCHEMA_DEFAULTS)
    assert document["properties"]["fidelity"]["enum"] == ["paper_aligned", "production_safe"]
    assert "standard" in document["properties"]["effort"]["enum"]
    assert document["additionalProperties"] is False
    assert document["$defs"]["TaskOverrideSet"]["properties"]["classic"]["additionalProperties"] is False
    assert document["x-editor"]["availability"] == "test_only"


async def test_equal_task_and_benchmark_inputs_match_admission_compiler(client):
    choices = {"fidelity": "production_safe", "effort": "thorough", "task_overrides": {"classic": {"limits.max_cost": "4.50"}, "seed": 7}}
    task = (await client.post("/classic/spec/compile", json=choices)).json()
    arm = (await client.post("/classic/spec/compile", json=choices)).json()
    admission_input = specification_input_from({"fidelity": choices["fidelity"], "effort": choices["effort"], **choices["task_overrides"]}, golden_deployment())
    assert task["specification_digest"] == arm["specification_digest"] == specification_digest(compile_specification(admission_input))
    assert task["specification"] == arm["specification"]
    assert task["admissible"] is False
    with pytest.raises(ValueError):
        require_admissible_runtime(RuntimeKey("classic", "2"))


async def test_preview_discloses_caps_rejections_estimates_and_every_effective_value(client):
    choices = {"fidelity": "paper_aligned", "effort": "thorough", "task_overrides": {"classic": {"coordination.max_rounds": 9999, "cleaner.enabled": True, "limits.max_cost": "5000"}, "seed": 42}}
    response = await client.post("/classic/spec/compile", json=choices)
    assert response.status_code == 200, response.text
    data = response.json()
    expected = compile_specification(ClassicSpecInput(**choices, deployment=golden_deployment()))
    assert data["specification"] == expected.model_dump(mode="json")
    assert data["caps"] == {key: list(value) for key, value in DEPLOYMENT_CAPS.items()}
    assert {item["field"] for item in data["specification"]["deployment_caps"]["adjustments"]} == {"coordination.max_rounds", "limits.max_cost"}
    assert {item["kind"] for item in data["specification"]["warnings"]} >= {"rejected_override", "clamped_value"}
    assert data["differences"]["fidelity"] and data["differences"]["effort"]
    assert data["specification"]["provider_capabilities"]["seed_support"] == "unsupported"
    assert data["provider_limits"]["context_window_tokens"] is None
    assert len(data["estimate_assumptions"]) == 4
    assert "new runs only" in data["endpoint_notice"]


@pytest.mark.parametrize(("choices", "fields"), [
    ({"fidelity": "wrong", "effort": "wrong"}, {"fidelity", "effort"}),
    ({"task_overrides": {"classic": {"coordination.max_rounds": True}}}, {"coordination.max_rounds"}),
    ({"task_overrides": {"classic": {"team.experts_by_tier.complex": 2.5}}}, {"team.experts_by_tier.complex"}),
    ({"task_overrides": {"classic": {"limits.max_cost": "bad"}}}, {"limits.max_cost"}),
    ({"task_overrides": {"classic": {"memory.actor_memory": "wrong", "verification.solution_review": "wrong"}}}, {"memory.actor_memory", "verification.solution_review"}),
    ({"task_overrides": {"classic": {}, "seed": -1}}, {"task_overrides.seed"}),
    ({"task_overrides": {"classic": {"unknown": 4}}}, {"advanced"}),
    ({"task_overrides": {"classic": {"limits.max_input_tokens": 10, "limits.max_output_tokens": 100}}}, {"limits.max_input_tokens", "limits.max_output_tokens"}),
    ({"deployment": {}}, {"deployment"}),
])
async def test_errors_name_each_responsible_control(client, choices, fields):
    response = await client.post("/classic/spec/compile", json=choices)
    assert response.status_code == 422, response.text
    assert {error["field"] for error in response.json()["detail"]["errors"]} == fields


async def test_preview_authenticates_reads_and_compiles_before_snapshot(client):
    classic_spec.deployment_snapshot.reset_mock()
    for method, path in (("GET", "schema"), ("POST", "compile")):
        response = await client.request(method, f"/classic/spec/{path}", headers={"Authorization": "Bearer wrong"}, json={})
        assert response.status_code == 401
    classic_spec.deployment_snapshot.assert_not_awaited()


async def test_invalid_json_and_non_object_inputs_fail_without_snapshot(client):
    for content in ("{", "null", "[]"):
        response = await client.post("/classic/spec/compile", content=content)
        assert response.status_code == 422
    classic_spec.deployment_snapshot.assert_not_awaited()


async def test_endpoint_edits_change_new_preview_without_mutating_old_preview(client):
    old = (await client.post("/classic/spec/compile", json={})).json()
    deployment = golden_deployment()
    deployment.node_endpoints = ["http://replacement.fixture"]
    deployment.role_registry = {}
    classic_spec.deployment_snapshot.return_value = deployment
    new = (await client.post("/classic/spec/compile", json={})).json()
    assert old["specification_digest"] != new["specification_digest"]
    assert "replacement.fixture" not in str(old)
    assert "replacement.fixture" in str(new)


def test_published_schema_returns_independent_documents():
    first = published_schema()
    first["x-editor"]["groups"][0]["controls"].clear()
    assert published_schema()["x-editor"]["groups"][0]["controls"]


@pytest.mark.parametrize("field", sorted(DEPLOYMENT_CAPS))
async def test_each_published_cap_reports_its_adjustment(client, field):
    _minimum, maximum = DEPLOYMENT_CAPS[field]
    requested = str(int(maximum) + 1) if field == "limits.max_cost" else maximum + 1
    response = await client.post("/classic/spec/compile", json={"task_overrides": {"classic": {field: requested}}})
    assert response.status_code == 200, response.text
    adjustments = response.json()["specification"]["deployment_caps"]["adjustments"]
    adjustment = next(item for item in adjustments if item["field"] == field)
    assert adjustment["requested"] == requested
    assert Decimal(str(adjustment["effective"])) == Decimal(str(maximum))
    assert adjustment["rule"] == "maximum"


async def test_profile_differences_compare_money_values_instead_of_decimal_format(client):
    response = await client.post("/classic/spec/compile", json={
        "effort": "balanced", "task_overrides": {"classic": {"limits.max_cost": "0.500"}},
    })
    assert response.status_code == 200, response.text
    assert "limits.max_cost" not in {item["field"] for item in response.json()["differences"]["effort"]}
