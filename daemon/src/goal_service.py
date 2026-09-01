"""Foundation goal concurrency: versions, transitions, merges, rollback.

Each goal stores a stable identifier, an optimistic version, an owner
and runtime pair, parent and dependency identifiers, an allowed
transition policy, completion evidence requirements, and a merge key.
Concurrent updates use the optimistic version: one commit wins and
the stale writer conflicts.

Dependency cycles reject before persistence. Duplicate goals merge
through one explicit deterministic rule: the earliest identifier
stays canonical, later identifiers become aliases, and a merge can
never weaken completion evidence. When required completion evidence
becomes invalid, a completed goal rolls back to ``blocked`` as one
new journal transaction.
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import database as db
import runtime_journal as journal
from activation_service import run_identity

if TYPE_CHECKING:
    import aiosqlite

GOAL_STATES = (
    "proposed",
    "active",
    "blocked",
    "completed",
    "abandoned",
    "merged",
)

GOAL_TERMINAL_STATES = frozenset({"abandoned", "merged"})

GOAL_TRANSITIONS: dict[tuple[str, str], str] = {
    ("proposed", "active"): "owner_activates_the_goal",
    ("proposed", "abandoned"): "no_work_started",
    ("proposed", "merged"): "deterministic_duplicate_merge",
    ("active", "blocked"): "dependency_or_evidence_blocks_progress",
    ("active", "completed"): "completion_evidence_satisfied",
    ("active", "abandoned"): "owner_abandons_the_goal",
    ("active", "merged"): "deterministic_duplicate_merge",
    ("blocked", "active"): "blocking_condition_cleared",
    ("blocked", "abandoned"): "owner_abandons_the_goal",
    ("completed", "blocked"): "required_evidence_became_invalid",
}


class GoalServiceError(ValueError):
    """One goal rule failed closed."""


class GoalConflictError(GoalServiceError):
    """The optimistic version was stale."""


class GoalCycleError(GoalServiceError):
    """The dependency graph would contain a cycle."""


class GoalMergeError(GoalServiceError):
    """The merge would violate the deterministic merge rule."""


def validate_goal_transition(current: str, target: str) -> str:
    """Validate one goal transition against the declared table."""
    if current not in GOAL_STATES:
        raise GoalServiceError(f"Unknown goal state: {current!r}")
    if target not in GOAL_STATES:
        raise GoalServiceError(f"Unknown goal state: {target!r}")
    if current in GOAL_TERMINAL_STATES:
        raise GoalServiceError(f"{current!r} is a terminal goal state")
    condition = GOAL_TRANSITIONS.get((current, target))
    if condition is None:
        raise GoalServiceError(
            f"The goal transition {current!r} -> {target!r} is not declared"
        )
    return condition


async def get_goal(goal_id: str) -> dict[str, Any]:
    """Read one goal index row."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_goal(connection, goal_id)


async def _load_goal(
    connection: aiosqlite.Connection, goal_id: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM goal_index WHERE goal_id = ?", (goal_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise GoalServiceError(f"Unknown goal: {goal_id}")
    return dict(row)


def _goal_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": str(row["state"]),
        "version": int(row["version"]),
        "alias_of": row["alias_of"],
        "revalidation_required": bool(row["revalidation_required"]),
    }


async def _journal_goal_update(
    *,
    run_id: str,
    goal_id: str,
    goal_state: str,
    payload_extra: dict[str, Any] | None = None,
    extra_writes: Any = None,
    task_fence: str | None = None,
    database_time: str | None = None,
    idempotency_token: str | None = None,
) -> journal.JournalRecord:
    identity = await run_identity(run_id)
    return await journal.commit_operation(
        journal.JournalOperation(
            operation_type="goal_update",
            task_id=identity["task_id"],
            run_id=run_id,
            runtime_id=identity["runtime_id"],
            runtime_contract_version=identity["runtime_contract_version"],
            payload={
                "goal_id": goal_id,
                "goal_state": goal_state,
                **(payload_extra or {}),
            },
            idempotency_token=idempotency_token
            or f"goal-{goal_id}-{uuid.uuid4()}",
            task_fence=task_fence,
            tenant_id=identity["tenant_id"],
        ),
        database_time=database_time,
        extra_writes=extra_writes,
    )


async def _journal_goal_projection(
    *,
    run_id: str,
    goal_id: str,
    task_fence: str | None,
    database_time: str | None,
) -> None:
    row = await get_goal(goal_id)
    await _journal_goal_update(
        run_id=run_id,
        goal_id=goal_id,
        goal_state=str(row["state"]),
        payload_extra={"goal_index": _goal_projection(row)},
        task_fence=task_fence,
        database_time=database_time,
    )


async def _detect_cycle(
    connection: aiosqlite.Connection,
    run_id: str,
    goal_id: str,
    dependency_ids: tuple[str, ...],
    parent_goal_id: str | None,
) -> None:
    """Reject one dependency cycle before persistence.

    The scan reads only the run's goals. A goal dependency graph stays
    inside one run, so this scoping is exact and avoids a global scan.
    """
    cursor = await connection.execute(
        "SELECT goal_id, dependency_ids, parent_goal_id FROM goal_index "
        "WHERE run_id = ?",
        (run_id,),
    )
    edges: dict[str, set[str]] = {}
    for row in await cursor.fetchall():
        targets = set(json.loads(str(row["dependency_ids"])))
        if row["parent_goal_id"] is not None:
            targets.add(str(row["parent_goal_id"]))
        edges[str(row["goal_id"])] = targets
    new_targets = set(dependency_ids)
    if parent_goal_id is not None:
        new_targets.add(parent_goal_id)
    edges[goal_id] = new_targets

    visited: set[str] = set()
    stack: set[str] = set()

    def visit(node: str) -> None:
        if node in stack:
            raise GoalCycleError(
                f"The goal dependency graph would contain a cycle "
                f"through {node!r}"
            )
        if node in visited:
            return
        visited.add(node)
        stack.add(node)
        for target in edges.get(node, ()):
            visit(target)
        stack.discard(node)

    visit(goal_id)


async def create_goal(
    *,
    run_id: str,
    goal_id: str,
    owner: str,
    parent_goal_id: str | None = None,
    dependency_ids: tuple[str, ...] = (),
    transition_policy: str = "shared",
    completion_evidence: tuple[str, ...] = (),
    merge_key: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Create one goal with cycle rejection before persistence."""
    identity = await run_identity(run_id)

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        await _detect_cycle(
            connection, run_id, goal_id, dependency_ids, parent_goal_id,
        )
        await connection.execute(
            "INSERT INTO goal_index ("
            "goal_id, tenant_id, run_id, task_id, state, version, owner, "
            "runtime_id, runtime_contract_version, parent_goal_id, "
            "dependency_ids, transition_policy, completion_evidence, "
            "merge_key, created_at, updated_at, journal_cursor) "
            "VALUES (?, ?, ?, ?, 'proposed', 1, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (
                goal_id,
                identity["tenant_id"],
                run_id,
                identity["task_id"],
                owner,
                identity["runtime_id"],
                identity["runtime_contract_version"],
                parent_goal_id,
                json.dumps(list(dependency_ids)),
                transition_policy,
                json.dumps(sorted(completion_evidence)),
                merge_key,
                now,
                now,
                journal_cursor,
            ),
        )

    return await _journal_goal_update(
        run_id=run_id,
        goal_id=goal_id,
        goal_state="proposed",
        payload_extra={
            "goal_index": {
                "state": "proposed",
                "version": 1,
                "alias_of": None,
                "revalidation_required": False,
            },
        },
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
        idempotency_token=f"goal-create-{goal_id}",
    )


_UPDATABLE_COLUMNS = (
    "owner",
    "dependency_ids",
    "completion_evidence",
    "transition_policy",
)


async def update_goal(
    *,
    run_id: str,
    goal_id: str,
    expected_version: int,
    changes: dict[str, Any],
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Apply one optimistic goal update.

    The update commits only against the exact expected version, so
    one concurrent writer commits and the stale writer conflicts.
    """
    unknown = set(changes) - set(_UPDATABLE_COLUMNS)
    if unknown:
        raise GoalServiceError(f"Unknown goal columns: {sorted(unknown)}")
    result: dict[str, Any] = {}

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_goal(connection, goal_id)
        if int(row["version"]) != expected_version:
            raise GoalConflictError(
                f"The goal version moved past {expected_version}"
            )
        if "dependency_ids" in changes:
            await _detect_cycle(
                connection,
                run_id,
                goal_id,
                tuple(changes["dependency_ids"]),
                row["parent_goal_id"],
            )
        assignments = ["version = version + 1", "updated_at = ?",
                       "journal_cursor = ?"]
        values: list[Any] = [now, journal_cursor]
        for column, value in changes.items():
            assignments.append(f"{column} = ?")
            values.append(
                json.dumps(sorted(value))
                if isinstance(value, (list, tuple))
                else value,
            )
        values.extend([goal_id, expected_version])
        cursor = await connection.execute(
            f"UPDATE goal_index SET {', '.join(assignments)} "
            "WHERE goal_id = ? AND version = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise GoalConflictError(
                f"The goal version moved past {expected_version}"
            )
        result["version"] = expected_version + 1

    row = await get_goal(goal_id)
    await _journal_goal_update(
        run_id=run_id,
        goal_id=goal_id,
        goal_state=str(row["state"]),
        payload_extra={"expected_version": expected_version},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
    )
    await _journal_goal_projection(
        run_id=run_id, goal_id=goal_id,
        task_fence=task_fence, database_time=database_time,
    )
    return result


async def _completion_evidence_satisfied(
    connection: aiosqlite.Connection, row: dict[str, Any],
) -> list[str]:
    """List every required claim that is not currently supported."""
    unsatisfied = []
    for claim_id in json.loads(str(row["completion_evidence"])):
        cursor = await connection.execute(
            "SELECT supported FROM claim_index WHERE claim_id = ?",
            (claim_id,),
        )
        claim = await cursor.fetchone()
        if claim is None or not int(claim["supported"]):
            unsatisfied.append(claim_id)
    return unsatisfied


async def transition_goal(
    *,
    run_id: str,
    goal_id: str,
    target_state: str,
    expected_version: int,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Commit one declared goal transition under the optimistic version."""

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_goal(connection, goal_id)
        if int(row["version"]) != expected_version:
            raise GoalConflictError(
                f"The goal version moved past {expected_version}"
            )
        validate_goal_transition(str(row["state"]), target_state)
        if target_state == "completed":
            unsatisfied = await _completion_evidence_satisfied(
                connection, row,
            )
            if unsatisfied:
                raise GoalServiceError(
                    "Completion requires supported evidence for "
                    f"{unsatisfied}"
                )
        await connection.execute(
            "UPDATE goal_index SET state = ?, version = version + 1, "
            "updated_at = ?, journal_cursor = ? "
            "WHERE goal_id = ? AND version = ?",
            (target_state, now, journal_cursor, goal_id, expected_version),
        )

    record = await _journal_goal_update(
        run_id=run_id,
        goal_id=goal_id,
        goal_state=target_state,
        payload_extra={"expected_version": expected_version},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
    )
    await _journal_goal_projection(
        run_id=run_id, goal_id=goal_id,
        task_fence=task_fence, database_time=database_time,
    )
    return record


async def merge_duplicate_goals(
    *,
    run_id: str,
    merge_key: str,
    proposed_completion_evidence: tuple[str, ...] | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Merge every duplicate goal under one merge key deterministically.

    The earliest identifier stays canonical: the earliest creation
    time wins and the lowest goal identifier breaks a tie. Later
    identifiers become aliases in the ``merged`` state. The canonical
    goal keeps the union of every completion evidence requirement; a
    proposed evidence set that drops one requirement is a weakening
    merge and rejects.
    """
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM goal_index WHERE merge_key = ? "
            "AND state != 'merged' ORDER BY created_at, goal_id",
            (merge_key,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    if len(rows) < 2:
        raise GoalMergeError(
            f"The merge key {merge_key!r} names fewer than two live goals"
        )
    canonical = rows[0]
    duplicates = rows[1:]
    union: set[str] = set()
    for row in rows:
        union.update(json.loads(str(row["completion_evidence"])))
    merged_evidence = sorted(union)
    if proposed_completion_evidence is not None:
        proposed = sorted(proposed_completion_evidence)
        if not union.issubset(set(proposed)):
            raise GoalMergeError(
                "A merge can never weaken completion evidence"
            )
        merged_evidence = proposed

    canonical_id = str(canonical["goal_id"])

    async def canonical_extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        await connection.execute(
            "UPDATE goal_index SET completion_evidence = ?, "
            "version = version + 1, updated_at = ?, journal_cursor = ? "
            "WHERE goal_id = ?",
            (
                json.dumps(merged_evidence),
                now,
                journal_cursor,
                canonical_id,
            ),
        )

    await _journal_goal_update(
        run_id=run_id,
        goal_id=canonical_id,
        goal_state=str(canonical["state"]),
        payload_extra={"merge_key": merge_key, "role": "canonical"},
        extra_writes=canonical_extra,
        task_fence=task_fence,
        database_time=database_time,
    )
    await _journal_goal_projection(
        run_id=run_id, goal_id=canonical_id,
        task_fence=task_fence, database_time=database_time,
    )

    aliases = []
    for duplicate in duplicates:
        duplicate_id = str(duplicate["goal_id"])
        validate_goal_transition(str(duplicate["state"]), "merged")

        async def alias_extra(
            connection: aiosqlite.Connection,
            journal_cursor: int,
            now: str,
            duplicate_id: str = duplicate_id,
        ) -> None:
            await connection.execute(
                "UPDATE goal_index SET state = 'merged', alias_of = ?, "
                "version = version + 1, updated_at = ?, "
                "journal_cursor = ? WHERE goal_id = ?",
                (canonical_id, now, journal_cursor, duplicate_id),
            )

        await _journal_goal_update(
            run_id=run_id,
            goal_id=duplicate_id,
            goal_state="merged",
            payload_extra={"merge_key": merge_key, "alias_of": canonical_id},
            extra_writes=alias_extra,
            task_fence=task_fence,
            database_time=database_time,
        )
        await _journal_goal_projection(
            run_id=run_id, goal_id=duplicate_id,
            task_fence=task_fence, database_time=database_time,
        )
        aliases.append(duplicate_id)
    return {
        "canonical_goal_id": canonical_id,
        "aliases": aliases,
        "completion_evidence": merged_evidence,
    }


async def rollback_completed_goal(
    *,
    run_id: str,
    goal_id: str,
    reason: str,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> journal.JournalRecord:
    """Roll one completed goal back to blocked as one new transaction."""

    async def extra(
        connection: aiosqlite.Connection, journal_cursor: int, now: str,
    ) -> None:
        row = await _load_goal(connection, goal_id)
        validate_goal_transition(str(row["state"]), "blocked")
        await connection.execute(
            "UPDATE goal_index SET state = 'blocked', "
            "revalidation_required = 1, version = version + 1, "
            "updated_at = ?, journal_cursor = ? WHERE goal_id = ?",
            (now, journal_cursor, goal_id),
        )

    record = await _journal_goal_update(
        run_id=run_id,
        goal_id=goal_id,
        goal_state="blocked",
        payload_extra={"rollback_reason": reason},
        extra_writes=extra,
        task_fence=task_fence,
        database_time=database_time,
    )
    await _journal_goal_projection(
        run_id=run_id, goal_id=goal_id,
        task_fence=task_fence, database_time=database_time,
    )
    return record


async def rollback_goals_for_claim(
    *,
    run_id: str,
    claim_id: str,
    reason: str,
    source_decision_id: str | None = None,
    task_fence: str | None = None,
    database_time: str | None = None,
) -> list[str]:
    """Roll back or mark every goal that requires one invalid claim."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT goal_id, state, completion_evidence FROM goal_index "
            "WHERE run_id = ?",
            (run_id,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    affected = [
        row
        for row in rows
        if claim_id in json.loads(str(row["completion_evidence"]))
    ]
    rolled_back = []
    identity = await run_identity(run_id)
    for row in affected:
        goal_id = str(row["goal_id"])
        if str(row["state"]) == "completed":
            await rollback_completed_goal(
                run_id=run_id,
                goal_id=goal_id,
                reason=reason,
                task_fence=task_fence,
                database_time=database_time,
            )
            rolled_back.append(goal_id)

        async def marker_extra(
            connection: aiosqlite.Connection,
            journal_cursor: int,
            now: str,
            goal_id: str = goal_id,
        ) -> None:
            await connection.execute(
                "UPDATE goal_index SET revalidation_required = 1, "
                "updated_at = ?, journal_cursor = ? WHERE goal_id = ?",
                (now, journal_cursor, goal_id),
            )
            await connection.execute(
                "INSERT INTO revalidation_markers ("
                "marker_id, tenant_id, run_id, target_kind, target_id, "
                "source_decision_id, reason, created_at, journal_cursor) "
                "VALUES (?, ?, ?, 'goal', ?, ?, ?, ?, ?)",
                (
                    f"marker-{uuid.uuid4()}",
                    identity["tenant_id"],
                    run_id,
                    goal_id,
                    source_decision_id,
                    reason,
                    now,
                    journal_cursor,
                ),
            )

        await _journal_goal_update(
            run_id=run_id,
            goal_id=goal_id,
            goal_state=str((await get_goal(goal_id))["state"]),
            payload_extra={"revalidation_marker": claim_id},
            extra_writes=marker_extra,
            task_fence=task_fence,
            database_time=database_time,
        )
        await _journal_goal_projection(
            run_id=run_id, goal_id=goal_id,
            task_fence=task_fence, database_time=database_time,
        )
    return rolled_back
