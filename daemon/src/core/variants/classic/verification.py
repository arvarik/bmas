"""The verification policy of the Classic runtime.

The verification policy selects the checks a candidate answer needs and
interprets the verifier results: it finds an accepted solution, names
the solution a committed critique reviewed, plans the grace
verification of a forced answer, builds the approval record, and
resolves the final answer from the board. Every function reads its
input from its arguments and touches no storage or provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.entry import BoardEntry

APPROVAL_TITLE = "Verification passed"


@dataclass(frozen=True)
class GracePlan:
    """The closing work the grace verification still owes."""

    candidate: BoardEntry | None
    revision: bool


@dataclass(frozen=True)
class ResolvedAnswer:
    """The final answer the board supplies without a vote."""

    answer: str
    answer_source: str
    verification_status: str


class VerificationPolicy:
    """Select checks and interpret verifier results (doc 05 §3)."""

    @staticmethod
    def accepted_solution(
        snapshot: dict[str, BoardEntry],
        current_round: int | None = None,
        reviewed_solution_id: str | None = None,
        require_review: bool = False,
    ) -> BoardEntry | None:
        """Find an accepted solution (survived one round without critique).

        A solution is 'accepted' if no open critique referencing it was
        posted in the same round (doc 05 §3).
        """
        solutions = [
            e for e in snapshot.values()
            if e.type == "solution" and e.status == "open"
        ]
        if not solutions:
            return None

        for sol in sorted(solutions, key=lambda e: e.round, reverse=True):
            if require_review and sol.id != reviewed_solution_id:
                continue
            # Check if any open critique references this solution
            contested = any(
                e.type == "critique"
                and e.status == "open"
                and sol.id in e.refs
                and (current_round is None or e.round >= sol.round)
                for e in snapshot.values()
            )
            if not contested:
                return sol
        return None

    @staticmethod
    def latest_open_solution(snapshot: dict[str, BoardEntry]) -> BoardEntry | None:
        """The newest open solution by round and identifier."""
        open_solutions = sorted(
            (
                entry for entry in snapshot.values()
                if entry.type == "solution" and entry.status == "open"
            ),
            key=lambda entry: (entry.round, entry.id),
            reverse=True,
        )
        return open_solutions[0] if open_solutions else None

    @classmethod
    def reviewed_solution(
        cls,
        snapshot: dict[str, BoardEntry],
        committed_critiques: list[BoardEntry],
    ) -> str | None:
        """The solution a committed approval critique names, else None."""
        latest = cls.latest_open_solution(snapshot)
        if latest is None:
            return None
        solution_id = latest.id
        if not any(
            entry.type == "critique"
            and entry.status == "superseded"
            and entry.title == APPROVAL_TITLE
            and solution_id in entry.refs
            for entry in committed_critiques
        ):
            return None
        return solution_id

    @staticmethod
    def approval_entry(solution_id: str, mutation_id: str | None) -> dict[str, Any]:
        """The critique entry that records one passed verification."""
        proposed: dict[str, Any] = {
            "type": "critique",
            "title": APPROVAL_TITLE,
            "body": (
                "The independent critic found no blocking issue in "
                f"solution {solution_id}."
            ),
            "refs": [solution_id],
            "confidence": 1.0,
        }
        if mutation_id:
            proposed["_mutation_id"] = f"{mutation_id}:approval"
        return proposed

    @classmethod
    def grace_plan(
        cls,
        snapshot: dict[str, BoardEntry],
        meta: dict[str, Any],
        *,
        grace_verification: bool,
        critic_enabled: bool,
        within_overrun: Callable[[], bool],
        revision_headroom: Callable[[], bool],
    ) -> GracePlan:
        """The grace work a forced decider still owes.

        A grace critic review of an unseen answer comes first. After the
        critic rejected the answer with a critique, the decider gets
        exactly one revision round when resources remain.
        """
        if not meta.get("decider_forced") or not grace_verification or not critic_enabled:
            return GracePlan(candidate=None, revision=False)
        latest_solution = cls.latest_open_solution(snapshot)
        if (
            latest_solution is None
            or latest_solution.id == meta.get("solution_reviewed_id")
            or not within_overrun()
        ):
            return GracePlan(candidate=None, revision=False)
        if latest_solution.id != meta.get("solution_candidate_id"):
            # The critic has not seen this answer yet.
            return GracePlan(candidate=latest_solution, revision=False)
        if not meta.get("grace_revision_done"):
            # The critic saw this answer and did not approve it. If it
            # posted a critique and resources remain, the decider gets
            # exactly one revision round.
            rejected = any(
                entry.type == "critique"
                and entry.status == "open"
                and latest_solution.id in (entry.refs or [])
                for entry in snapshot.values()
            )
            if rejected and revision_headroom():
                return GracePlan(candidate=None, revision=True)
        return GracePlan(candidate=None, revision=False)

    @classmethod
    def resolve_answer(
        cls,
        snapshot: dict[str, BoardEntry],
        reviewed_solution_id: str | None,
    ) -> ResolvedAnswer | None:
        """The board's own answer: reviewed, else the newest open solution.

        Returns None when the board holds no solution, so the caller
        falls back to the solution-extraction vote.
        """
        solution_entry = cls.accepted_solution(
            snapshot,
            reviewed_solution_id=reviewed_solution_id,
            require_review=True,
        )
        if solution_entry:
            return ResolvedAnswer(solution_entry.body, "decider", "critic_reviewed")
        latest = cls.latest_open_solution(snapshot)
        if latest is not None:
            return ResolvedAnswer(latest.body, "decider_unverified", "unverified")
        return None
