"""The termination policy of the Classic runtime.

The termination policy decides success, failure, or more work: it
checks the hard limits of a round, detects a stalled board, keeps the
turn timing that sizes the closing reserve, extracts a solution by
vote when the board holds none, and finds the best finding as the last
resort. The policy keeps the stall history and the turn durations as
its state, and the engine checkpoints both. The provider call of the
vote stays outside the policy: the engine passes one answer callable.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from budget_service import BudgetError
from core.model_parameters import completion_parameters, profile_for_alias
from core.variants.classic.consensus import ConsensusPolicy

if TYPE_CHECKING:
    from core.entry import BoardEntry
    from core.variants.classic.roster import AgentRoster

logger = logging.getLogger("bmas.classic.termination")

STALL_SIMILARITY = 0.9
TURN_DURATION_HISTORY = 12
CLOSING_TURN_TIMEOUT_FLOOR_S = 120
CLOSING_TURN_TIMEOUT_CAP_S = 600
STALL_HISTORY_ROUNDS = 6
REVISION_HEADROOM_S = 30.0
NO_ANSWER = "No answer could be determined."

# One answer per actor: the callable builds and posts the vote prompt of
# one actor and returns the reply text. The engine owns the transport
# and the model resolution of each call.
AnswerCall = Callable[[str], Awaitable[str]]


def round_token_set(entries: list[BoardEntry]) -> frozenset[str]:
    """Return the normalized word set of one round's open entry bodies."""
    words: set[str] = set()
    for entry in entries:
        body = (entry.body or "").lower()
        words.update(
            token for token in re.findall(r"[a-z0-9]+", body) if len(token) > 2
        )
    return frozenset(words)


def token_jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard overlap of two word sets; 0.0 when either side is empty."""
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def entries_hash(entries: list[BoardEntry]) -> str:
    """Hash entry bodies for near-duplicate detection."""
    bodies = sorted(e.body.strip().lower() for e in entries)
    combined = "|".join(bodies)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LimitVerdict:
    """What the hard limits of one round decide."""

    force_decider: bool = False
    force_replan: bool = False
    term_reason: str | None = None


@dataclass
class TerminationPolicy:
    """Decide success, failure, pause, or more work (doc 05 §5)."""

    # The stall detector state: consecutive stalled rounds, the hashes
    # of the rounds seen, and the recent round word sets.
    stall_counter: int = 0
    round_hashes: list[str] = field(default_factory=list)
    round_token_sets: list[frozenset[str]] = field(default_factory=list)
    # The wall-clock durations of the recent turns, in seconds.
    turn_durations: list[float] = field(default_factory=list)

    # ── Stall detection ───────────────────────────────────────────────

    def is_stalled(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        *,
        stall_rounds: int,
        require_evidence: bool,
        round_lacks_evidence: Callable[[list[BoardEntry]], bool],
    ) -> bool:
        """Check if the board is stalled (doc 05 §5).

        Stall = rounds with no accepted entries, exact-duplicate bodies, or
        paraphrased near-duplicates of a recent round (token-set overlap).
        """
        # Get entries from the previous round
        prev_round = current_round - 1
        prev_entries = [
            e for e in snapshot.values()
            if e.round == prev_round and e.status == "open"
        ]

        if not prev_entries:
            # No entries produced last round
            self.stall_counter += 1
            return self.stall_counter >= stall_rounds

        # Exact repetition: normalized hash of the round's bodies.
        round_hash = entries_hash(prev_entries)
        round_tokens = round_token_set(prev_entries)
        if round_hash in self.round_hashes:
            self.stall_counter += 1
        elif any(
            token_jaccard(round_tokens, seen) >= STALL_SIMILARITY
            for seen in self.round_token_sets[-STALL_HISTORY_ROUNDS:]
        ):
            # Paraphrased repetition: the round restates recent content in
            # new words without adding new information.
            self.stall_counter += 1
        elif require_evidence and round_lacks_evidence(prev_entries):
            # Novel words without external grounding: at evidence-gated
            # effort levels an unsourced contribution round is not progress.
            self.stall_counter += 1
            self.round_hashes.append(round_hash)
        else:
            self.stall_counter = 0
            self.round_hashes.append(round_hash)
        if (
            not self.round_token_sets
            or round_tokens != self.round_token_sets[-1]
        ):
            self.round_token_sets.append(round_tokens)
            del self.round_token_sets[:-STALL_HISTORY_ROUNDS]

        return self.stall_counter >= stall_rounds

    # ── Hard limits ───────────────────────────────────────────────────

    @staticmethod
    def limit_verdict(
        *,
        current_round: int,
        max_rounds: int,
        budget_spent: float,
        budget_ceiling: float,
        elapsed_s: float,
        duration_limit_s: float,
        stalled: Callable[[], bool],
        stall_counter: Callable[[], int],
        stall_rounds: int,
        replan_count: int,
        max_replans: int,
    ) -> LimitVerdict:
        """The decision of the round guards, in their fixed order.

        The guards run in order: rounds, budget, duration, stall. The
        stall check runs only when no earlier guard forced the decider,
        because it advances the stall state.
        """
        if current_round > max_rounds:
            return LimitVerdict(force_decider=True, term_reason="max_rounds")
        if budget_spent >= budget_ceiling:
            return LimitVerdict(force_decider=True, term_reason="budget")
        if elapsed_s >= duration_limit_s:
            return LimitVerdict(force_decider=True, term_reason="duration")
        if stalled():
            logger.info(
                "Stall detected at round %d (stall_counter=%d)",
                current_round, stall_counter(),
            )
            if stall_counter() >= stall_rounds:
                if replan_count < max_replans:
                    return LimitVerdict(force_replan=True)
                return LimitVerdict(force_decider=True, term_reason="stalled")
            # Not yet at threshold — continue but note the stall
        return LimitVerdict()

    @staticmethod
    def revision_headroom(
        meta: dict[str, Any],
        *,
        budget_ceiling: float,
        elapsed_s: float,
        max_duration_s: float,
    ) -> bool:
        """True when budget and wall clock allow one revision round."""
        budget_spent = float(meta.get("budget_spent", 0.0))
        if budget_ceiling > 0 and budget_spent >= budget_ceiling:
            return False
        return elapsed_s < max_duration_s - REVISION_HEADROOM_S

    @staticmethod
    def is_terminal(
        board: Any,
        accepted_solution: Callable[[dict[str, BoardEntry]], BoardEntry | None],
    ) -> tuple[bool, str | None]:
        """Pure check: is the board in a terminal state?"""
        if isinstance(board, dict):
            snapshot = board
        else:
            # Synchronous check — only works with pre-fetched snapshot
            return (False, None)

        if accepted_solution(snapshot):
            return (True, "solution")
        return (False, None)

    # ── Turn timing ───────────────────────────────────────────────────

    def note_turn_duration(self, duration_ms: Any) -> None:
        """Record one completed turn's wall-clock duration."""
        if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
            return
        self.turn_durations.append(float(duration_ms) / 1000.0)
        del self.turn_durations[:-TURN_DURATION_HISTORY]

    def average_turn_s(self) -> float:
        if not self.turn_durations:
            return 0.0
        return sum(self.turn_durations) / len(self.turn_durations)

    def closing_turn_timeout_s(self) -> int:
        """Timeout floor for closing-sequence turns (decider, grace)."""
        return int(min(
            CLOSING_TURN_TIMEOUT_CAP_S,
            max(CLOSING_TURN_TIMEOUT_FLOOR_S, 2.0 * self.average_turn_s()),
        ))

    def duration_reserve_s(self, static_reserve_s: float, max_duration_s: float) -> float:
        """Duration reserve scaled to observed turn latency.

        The static reserve assumes fast API models. On a slow local tier
        one turn can outlast the whole reserve, so the guard must fire
        early enough that the closing sequence starts before the cap.
        """
        adaptive = 2.0 * self.average_turn_s()
        return min(
            max(static_reserve_s, adaptive),
            0.4 * max_duration_s,
        )

    # ── Solution extraction (doc 05 §3, path 2) ───────────────────────

    @staticmethod
    def best_finding(snapshot: dict[str, BoardEntry]) -> str:
        """Last-resort: return the highest-salience finding."""
        findings = [
            e for e in snapshot.values()
            if e.type in ("finding", "solution") and e.status == "open"
        ]
        if not findings:
            return NO_ANSWER
        findings.sort(key=lambda e: e.salience, reverse=True)
        return findings[0].body

    @staticmethod
    def sole_request(actor: str, query: str, board_text: str, model: str) -> dict[str, Any]:
        """The request body of one solution-extraction answer."""
        from models.personas import SOLE_SYSTEM_PROMPT

        return {
            "model": model,
            "messages": [
                {"role": "system", "content": SOLE_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Objective: {query}\n\n"
                    f"Board state:\n{board_text}\n\n"
                    f"Your role: {actor}\n"
                    f"Provide your answer:"
                )},
            ],
            **completion_parameters(
                profile_for_alias(model), output_tokens=512,
                temperature=0.1, reasoning="low",
            ),
        }

    @classmethod
    async def solution_extraction(
        cls,
        snapshot: dict[str, BoardEntry],
        *,
        roster: AgentRoster | None,
        strategy: str,
        answer: AnswerCall,
    ) -> str:
        """Majority-similarity vote when no accepted solution exists.

        ``answer`` returns the reply text of one actor's vote prompt. A
        failed answer is logged and dropped. With no usable answer the
        best finding wins.
        """
        if not roster:
            return cls.best_finding(snapshot)

        # Collect one answer per agent identity (bare completion calls)
        answers: list[tuple[str, str]] = []
        tasks = [answer(actor) for actor, _ in roster.all_actors()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (actor, _), result in zip(roster.all_actors(), results, strict=False):
            if isinstance(result, str) and result.strip():
                answers.append((actor, result.strip()))
            elif isinstance(result, BudgetError):
                raise result
            elif isinstance(result, Exception):
                logger.warning("SolE answer failed for %s: %s", actor, result)

        if not answers:
            return cls.best_finding(snapshot)

        evidence = [
            (entry.body, float(entry.confidence), float(entry.salience))
            for entry in snapshot.values()
            if entry.status == "open"
            and entry.type in ("finding", "rebuttal", "artifact")
        ]
        return ConsensusPolicy.evidence_vote(answers, evidence, strategy)
