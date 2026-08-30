"""Capture helpers for the Foundation Stage 0A golden runtime fixtures.

Each capture function returns one JSON-encodable record for one
existing runtime pair. The frozen copies live in
``conformance/runtime_fixtures``. The golden-fixture test recaptures
every record and compares it byte for byte, so an unexplained contract
change fails before any Foundation stage changes routing.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from classic_harness import ClassicLifecycleHarness

import settings_store
from core import protocol
from core.triage import Complexity, TriageResult
from core.variants import VariantExecutionRequest, variant_capabilities
from core.variants.classic import ClassicVariantRuntime
from core.variants.collaborative import (
    PatchboardVariantRuntime,
    StigmergicVariantRuntime,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "conformance" / "runtime_fixtures"
FIXTURE_CONTRACT_VERSION = "1.0.0"

RUNTIME_CLASSES = {
    "classic": ClassicVariantRuntime,
    "patchboard": PatchboardVariantRuntime,
    "stigmergic": StigmergicVariantRuntime,
}

# The frozen historical labels and the planned study exposure. The
# patchboard study label changes at the benchmark interface stage; the
# historical label stays frozen for existing tasks.
STUDY_LABELS = {"patchboard": "Parallel synthesis"}

# One deterministic role registry for configuration capture. The
# collaborative runtimes refuse a capture when a required role has no
# registered endpoint.
CAPTURE_ROLE_REGISTRY = {
    "planner": {"profile": "planner", "endpoints": ["http://agent.fixture"]},
    "critic": {"profile": "critic", "endpoints": ["http://agent.fixture"]},
    "decider": {"profile": "decider", "endpoints": ["http://agent.fixture"]},
}


def reset_runtime_settings() -> None:
    """Reseed the settings store so a capture never sees test residue."""
    settings_store._store = None


def encode_fixture(record: Any) -> bytes:
    text = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def fixture_path(fixture_id: str) -> Path:
    return FIXTURES_DIR / f"{fixture_id}.json"


def wrap(fixture_id: str, record: Any) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "metadata": {"contract_version": FIXTURE_CONTRACT_VERSION},
        "record": record,
    }


class ScriptedHost:
    """Drive one collaborative runtime with deterministic responses."""

    def __init__(self, checkpoint: dict[str, Any] | None = None) -> None:
        self.checkpoint = checkpoint
        self.dispatches: list[dict[str, Any]] = []
        self.saved_checkpoints: list[dict[str, Any]] = []
        self.phases: list[dict[str, Any]] = []
        self.progress: list[list[dict[str, Any]]] = []

    async def publish_phase(self, phase, iteration, task_id) -> None:
        self.phases.append({"phase": phase, "iteration": iteration})

    async def check_abort(self, task_id) -> None:
        return None

    async def log_event(self, node_id, message, task_id, **kwargs) -> None:
        return None

    async def dispatch_agent(self, *, task_id, activation_id, **kwargs) -> dict[str, Any]:
        self.dispatches.append(
            {"task_id": task_id, "activation_id": activation_id, **kwargs}
        )
        return {
            "status": "completed",
            "result": f"agent-output-{len(self.dispatches)}-{kwargs['role']}",
        }

    async def publish_progress(self, task_id, label, status, items) -> None:
        self.progress.append(items)

    def task_lease_token(self, task_id) -> str | None:
        return "fixture-lease"

    async def load_variant_checkpoint(self, task_id, variant_id) -> dict[str, Any] | None:
        return self.checkpoint

    async def save_variant_checkpoint(self, task_id, variant_id, checkpoint) -> None:
        self.checkpoint = checkpoint
        self.saved_checkpoints.append(checkpoint)


def _collaborative_request(runtime_id: str, settings: dict[str, Any]) -> VariantExecutionRequest:
    return VariantExecutionRequest(
        task_id="task-fixture",
        session_id="session-fixture",
        user_task="Produce the frozen fixture answer.",
        triage=TriageResult(Complexity.MEDIUM, "model-medium"),
        effective_configuration={
            "variant": runtime_id,
            "configuration_schema_version": "1",
            "settings": {runtime_id: settings},
            "model_routing": {"medium": "model-medium"},
            "role_registry": CAPTURE_ROLE_REGISTRY,
        },
    )


def _outcome_record(outcome: Any) -> dict[str, Any]:
    return {
        "variant_id": outcome.variant_id,
        "answer": outcome.answer,
        "result": outcome.result,
        "public_result": outcome.public_result,
        "cost_usd": outcome.cost_usd,
        "completed_subtasks": list(outcome.completed_subtasks),
    }


async def capture_capability_document() -> dict[str, Any]:
    """Freeze the capability document for the built-in runtimes.

    Other tests can register temporary runtimes in the shared registry,
    so the capture keeps only the built-in identifiers.
    """
    document = variant_capabilities()
    return {
        "api_version": document["api_version"],
        "variants": [
            record
            for record in document["variants"]
            if record["id"] in RUNTIME_CLASSES
        ],
    }


async def capture_effective_configuration(runtime_id: str) -> dict[str, Any]:
    reset_runtime_settings()
    runtime = RUNTIME_CLASSES[runtime_id]
    overrides = None
    if runtime_id != "classic":
        overrides = {"role_registry": CAPTURE_ROLE_REGISTRY}
    return await runtime.capture_configuration(overrides)


async def capture_classic_legacy_migration() -> dict[str, Any]:
    metadata = {
        "effective_task_config": {
            "traditional": {
                "max_rounds": 4,
                "budget_ceiling_usd": 0.5,
                "stall_rounds": 2,
            },
            "model_pools": {},
        },
        "effective_routing": {"medium": "model-medium"},
        "effective_registry": {
            "planner": {"profile": "planner", "endpoints": ["http://agent.fixture"]},
        },
    }
    migrated = ClassicVariantRuntime.configuration_from_metadata(metadata)
    return {"metadata": metadata, "migrated": migrated}


async def capture_collaborative_lifecycle(runtime_id: str) -> dict[str, Any]:
    runtime = RUNTIME_CLASSES[runtime_id]
    settings = runtime._settings()
    fresh_host = ScriptedHost()
    fresh_outcome = await runtime.run(fresh_host, _collaborative_request(runtime_id, settings))

    resumed_host = ScriptedHost(checkpoint=fresh_host.checkpoint)
    resumed_outcome = await runtime.run(
        resumed_host, _collaborative_request(runtime_id, settings)
    )

    return {
        "runtime_id": runtime_id,
        "contract_version": runtime.descriptor.contract_version,
        "settings": settings,
        "fresh_run": {
            "phases": fresh_host.phases,
            "dispatches": fresh_host.dispatches,
            "checkpoints": fresh_host.saved_checkpoints,
            "progress": fresh_host.progress,
            "outcome": _outcome_record(fresh_outcome),
        },
        "resumed_run": {
            "dispatch_count": len(resumed_host.dispatches),
            "checkpoint_writes": len(resumed_host.saved_checkpoints),
            "outcome": _outcome_record(resumed_outcome),
        },
    }


async def capture_classic_lifecycle() -> dict[str, Any]:
    run = await ClassicLifecycleHarness("sequential").run()
    board = sorted(
        [entry.type, entry.author, entry.title, entry.body, entry.status, entry.space]
        for entry in run.snapshot.values()
    )
    return {
        "runtime_id": "classic",
        "contract_version": ClassicVariantRuntime.descriptor.contract_version,
        "mode": run.mode,
        "terminal_result": run.result,
        "board": board,
        "worker_calls": [
            {"actor": call.actor, "role": call.role, "round_no": call.round_no}
            for call in run.calls
        ],
        "event_type_counts": dict(sorted(Counter(
            str(event.get("event_type")) for event in run.events
        ).items())),
        "external_actions": [
            {"action": action, "count": count}
            for action, count in sorted(run.external_actions.items())
        ],
        "mutation_checks": run.mutation_checks,
    }


async def capture_protocol_vocabulary() -> dict[str, Any]:
    key_patterns = [
        {"pattern": pattern, **details}
        for pattern, details in sorted(protocol.V2_KEY_PATTERNS.items())
    ]
    return {
        "legacy_event_names": sorted(protocol.LEGACY_EVENT_NAMES),
        "board_event_names": protocol.all_v2_event_names(),
        "key_patterns": key_patterns,
    }


async def capture_runtime_labels() -> dict[str, Any]:
    labels = []
    for runtime_id in sorted(RUNTIME_CLASSES):
        descriptor = RUNTIME_CLASSES[runtime_id].descriptor
        labels.append({
            "runtime_id": runtime_id,
            "contract_version": descriptor.contract_version,
            "historical_label": descriptor.label,
            "study_label": STUDY_LABELS.get(runtime_id, descriptor.label),
            "aliases": list(descriptor.aliases),
        })
    return {"labels": labels}


async def capture_ui_adapter_support() -> dict[str, Any]:
    variants = []
    for runtime_id in sorted(RUNTIME_CLASSES):
        descriptor = RUNTIME_CLASSES[runtime_id].descriptor
        variants.append({
            "id": runtime_id,
            "contract_versions": [descriptor.contract_version],
            "panels": list(descriptor.features.panels),
            "graphs": list(descriptor.features.graphs),
            "result_fields": list(descriptor.features.result),
        })
    return {"variants": variants}


async def _capture_named_configuration(runtime_id: str) -> Any:
    return await capture_effective_configuration(runtime_id)


CAPTURES: dict[str, Any] = {
    "capability-document": capture_capability_document,
    "classic-effective-configuration": lambda: _capture_named_configuration("classic"),
    "patchboard-effective-configuration": lambda: _capture_named_configuration("patchboard"),
    "stigmergic-effective-configuration": lambda: _capture_named_configuration("stigmergic"),
    "classic-legacy-migration": capture_classic_legacy_migration,
    "classic-lifecycle": capture_classic_lifecycle,
    "patchboard-lifecycle": lambda: capture_collaborative_lifecycle("patchboard"),
    "stigmergic-lifecycle": lambda: capture_collaborative_lifecycle("stigmergic"),
    "protocol-vocabulary": capture_protocol_vocabulary,
    "runtime-labels": capture_runtime_labels,
    "ui-adapter-support": capture_ui_adapter_support,
}


async def capture_fixture_bytes(fixture_id: str) -> bytes:
    record = await CAPTURES[fixture_id]()
    return encode_fixture(wrap(fixture_id, record))
