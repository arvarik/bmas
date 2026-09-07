"""The consensus policy of the Classic runtime.

The consensus policy compares independent final candidates: the
majority-similarity vote of the solution extraction, the evidence
weighted vote, and the similarity helpers behind them. The strategy
vocabulary lives in the configuration schema, and every vote resolves
its strategy through it. Every function reads its input from its
arguments and touches no storage or provider.
"""
from __future__ import annotations

import logging
import re

from config_schema import DEFAULT_CONSENSUS_STRATEGY, resolve_consensus_strategy

logger = logging.getLogger("bmas.classic.consensus")

# Answers under this average length compare exactly under the
# token-similarity strategy.
SHORT_ANSWER_CHARS = 100
NO_ANSWER = "No answer could be determined."


def normalize_answer(text: str) -> str:
    """Normalize an answer for comparison."""
    # Lowercase, strip whitespace and punctuation
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def exact_similarity(a: str, b: str) -> float:
    """Exact match after normalization."""
    return 1.0 if normalize_answer(a) == normalize_answer(b) else 0.0


def fuzzy_similarity(a: str, b: str) -> float:
    """Token-overlap Jaccard similarity (cheap, no LLM)."""
    tokens_a = set(normalize_answer(a).split())
    tokens_b = set(normalize_answer(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0


def evidence_similarity(answer: str, evidence: str) -> float:
    """Measure whether a concise answer appears in an evidence statement."""
    answer_tokens = set(normalize_answer(answer).split())
    evidence_tokens = set(normalize_answer(evidence).split())
    if not answer_tokens or not evidence_tokens:
        return 0.0
    if len(answer_tokens) <= 4 and answer_tokens.issubset(evidence_tokens):
        return 1.0
    return fuzzy_similarity(answer, evidence)


class ConsensusPolicy:
    """Compare independent final candidates (doc 05 §3)."""

    @staticmethod
    def majority_vote(
        answers: list[tuple[str, str]],
        similarity_mode: str = DEFAULT_CONSENSUS_STRATEGY,
    ) -> str:
        """Majority-similarity vote: V(a_i) = Σ_j sim(a_i, a_j), argmax V.

        The registered strategies:
          - exact: normalized exact match (for short/numeric answers)
          - token_similarity: the legacy tiered behavior. Short answers
            (under 100 characters on average) compare by normalized exact
            match, longer answers by token Jaccard similarity.

        The legacy alias ``auto`` resolves to ``token_similarity``. An
        unregistered strategy raises ``ValueError`` instead of degrading to
        token similarity.
        """
        strategy = resolve_consensus_strategy(similarity_mode)
        if not answers:
            return NO_ANSWER

        if len(answers) == 1:
            return answers[0][1]

        # Determine similarity function
        if strategy == "exact":
            sim_fn = exact_similarity
        else:
            avg_len = sum(len(a[1]) for a in answers) / len(answers)
            if avg_len < SHORT_ANSWER_CHARS:
                sim_fn = exact_similarity
            else:
                sim_fn = fuzzy_similarity

        # Compute V(a_i) = Σ_j sim(a_i, a_j)
        scores: list[tuple[float, str, str]] = []
        for i, (actor_i, answer_i) in enumerate(answers):
            v = 0.0
            for j, (_actor_j, answer_j) in enumerate(answers):
                if i != j:
                    v += sim_fn(answer_i, answer_j)
            scores.append((v, actor_i, answer_i))

        # argmax V
        scores.sort(key=lambda x: x[0], reverse=True)
        winner = scores[0][2]

        logger.info(
            "SolE vote: winner=%s (score=%.2f), %d answers",
            scores[0][1], scores[0][0], len(answers),
        )

        return winner

    @classmethod
    def evidence_vote(
        cls,
        answers: list[tuple[str, str]],
        evidence: list[tuple[str, float, float]],
        similarity_mode: str = DEFAULT_CONSENSUS_STRATEGY,
    ) -> str:
        """Select an answer by peer support and independent board evidence."""
        strategy = resolve_consensus_strategy(similarity_mode)
        if not answers:
            return NO_ANSWER
        if not evidence:
            return cls.majority_vote(answers, strategy)

        if strategy == "exact":
            peer_similarity = exact_similarity
        else:
            peer_similarity = fuzzy_similarity

        scored: list[tuple[float, str, str]] = []
        for index, (actor, answer) in enumerate(answers):
            peer_score = sum(
                peer_similarity(answer, other_answer)
                for other_index, (_other_actor, other_answer) in enumerate(answers)
                if index != other_index
            )
            evidence_score = sum(
                evidence_similarity(answer, body)
                * max(0.0, min(2.0, confidence + salience))
                * 2.0
                for body, confidence, salience in evidence
            )
            scored.append((peer_score + evidence_score, actor, answer))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][2]


# The module functions the engine once defined.
sole_majority_vote = ConsensusPolicy.majority_vote
sole_evidence_vote = ConsensusPolicy.evidence_vote
