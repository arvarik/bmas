"""Run one cleaner crash probe in a fresh interpreter."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))



async def probe(arguments: dict) -> None:
    """Reopen the admitted run and exit at one selected durable write."""

    # Load the isolated test configuration before importing daemon services.
    __import__("conftest")

    import activation_service
    import config
    import database as db
    import interactive_admission as admission
    import runtime_journal as journal
    from core import foundation_gates
    from core.board_store import InMemoryBoardStore
    from core.capabilities import capabilities_for_role
    from core.event_emitter import InMemoryEventEmitter
    from core.variants.classic import projection, runtime

    db.DB_PATH = arguments["database"]
    config.FOUNDATION_GATES = {name: True for name in foundation_gates.PLANNED_WRITER_GATES}
    config.STORAGE_OPERATOR_CONFIRMED = True
    context = await admission.run_context_for(arguments["run_id"], lease_ref=arguments["lease_ref"])
    services = await admission.runtime_services_for(context, lease_owner="orchestrator",
        lease_fence=arguments["lease_ref"], lease_ttl_seconds=60.0,
        artifact_root=Path(arguments["artifacts"]))
    binding = runtime.NativeRunBinding(context=context, services=services)
    gateway = projection.NativeBoardGateway(InMemoryBoardStore(), InMemoryEventEmitter(), committer=binding)
    count = 0

    def crash(name: str) -> None:
        nonlocal count
        if name == arguments["point"]:
            count += 1
            if count == arguments["occurrence"]:
                os._exit(73)

    projection.failpoint = crash
    runtime.failpoint = crash
    activation_service.failpoint = crash
    journal.failpoint = crash
    await gateway.apply_condensation(task_id=context.task_id, actor="cleaner",
        capabilities=capabilities_for_role("cleaner"), proposal=arguments["proposal"],
        turn_id="activation-proposal", attempt=1, round_no=5)
    raise AssertionError("The selected crash boundary did not fire")


if __name__ == "__main__":
    asyncio.run(probe(json.loads(sys.stdin.read())))
