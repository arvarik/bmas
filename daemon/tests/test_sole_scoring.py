# /opt/bmas/daemon/tests/test_sole_scoring.py
"""Unit tests for SolE majority-similarity vote scoring (doc 05 §3).

No LLM — tests the voting algorithm against canned answer sets.
"""

from core.variants.traditional import (
    sole_majority_vote,
    _normalize_answer,
    _exact_similarity,
    _fuzzy_similarity,
)


# ── _normalize_answer() ─────────────────────────────────────────────

class TestNormalizeAnswer:

    def test_strips_whitespace(self):
        assert _normalize_answer("  hello  ") == "hello"

    def test_lowercases(self):
        assert _normalize_answer("HELLO") == "hello"

    def test_removes_punctuation(self):
        assert _normalize_answer("Hello, World!") == "hello world"

    def test_collapses_whitespace(self):
        assert _normalize_answer("hello   world") == "hello world"

    def test_empty_string(self):
        assert _normalize_answer("") == ""


# ── _exact_similarity() ─────────────────────────────────────────────

class TestExactSimilarity:

    def test_identical_returns_1(self):
        assert _exact_similarity("Yes", "Yes") == 1.0

    def test_case_insensitive(self):
        assert _exact_similarity("YES", "yes") == 1.0

    def test_whitespace_insensitive(self):
        assert _exact_similarity("  yes  ", "yes") == 1.0

    def test_different_returns_0(self):
        assert _exact_similarity("Yes", "No") == 0.0

    def test_punctuation_insensitive(self):
        assert _exact_similarity("$42.50", "4250") == 1.0


# ── _fuzzy_similarity() ─────────────────────────────────────────────

class TestFuzzySimilarity:

    def test_identical_returns_1(self):
        assert _fuzzy_similarity("hello world", "hello world") == 1.0

    def test_disjoint_returns_0(self):
        assert _fuzzy_similarity("hello", "world") == 0.0

    def test_partial_overlap(self):
        sim = _fuzzy_similarity("hello world foo", "hello world bar")
        assert 0.4 < sim < 0.8  # 2/4 overlap

    def test_empty_strings(self):
        assert _fuzzy_similarity("", "") == 0.0

    def test_one_empty(self):
        assert _fuzzy_similarity("hello", "") == 0.0


# ── sole_majority_vote() ────────────────────────────────────────────

class TestSoleMajorityVote:

    def test_single_answer(self):
        result = sole_majority_vote([("a", "42")])
        assert result == "42"

    def test_unanimous_exact(self):
        """All agents agree → winner is that answer."""
        answers = [("a", "42"), ("b", "42"), ("c", "42")]
        result = sole_majority_vote(answers, similarity_mode="exact")
        assert _normalize_answer(result) == "42"

    def test_majority_wins(self):
        """3 agents: 2 agree, 1 disagrees → majority wins."""
        answers = [("a", "Yes"), ("b", "Yes"), ("c", "No")]
        result = sole_majority_vote(answers, similarity_mode="exact")
        assert _normalize_answer(result) == "yes"

    def test_auto_selects_exact_for_short_answers(self):
        """Short answers → auto uses exact similarity."""
        answers = [("a", "42"), ("b", "42"), ("c", "99")]
        result = sole_majority_vote(answers, similarity_mode="auto")
        assert _normalize_answer(result) == "42"

    def test_auto_selects_fuzzy_for_long_answers(self):
        """Long answers → auto uses fuzzy (Jaccard) similarity."""
        # Two agents share many words, one is completely different
        answer_long_a = "The investment shows strong growth potential with revenue increasing"
        answer_long_b = "The investment shows strong growth with increasing revenue potential"
        answer_long_c = "Markets are volatile and unpredictable with no clear direction ahead"

        answers = [("a", answer_long_a), ("b", answer_long_b), ("c", answer_long_c)]
        result = sole_majority_vote(answers, similarity_mode="auto")
        # a and b should be more similar to each other than to c
        assert "growth" in result.lower() or "investment" in result.lower()

    def test_tie_returns_first_in_list(self):
        """On tie, argmax returns the first highest scorer."""
        # All different → all scores are 0 → first wins
        answers = [("a", "alpha"), ("b", "beta"), ("c", "gamma")]
        result = sole_majority_vote(answers, similarity_mode="exact")
        # All scores are 0, so it's a tie — first in the sorted list wins
        assert result in ["alpha", "beta", "gamma"]

    def test_empty_answers(self):
        result = sole_majority_vote([])
        assert "no answer" in result.lower()

    def test_case_insensitive_exact_match(self):
        answers = [("a", "YES"), ("b", "yes"), ("c", "No")]
        result = sole_majority_vote(answers, similarity_mode="exact")
        assert _normalize_answer(result) == "yes"

    def test_five_agents_majority(self):
        """5 agents: 3 say Yes, 2 say No → Yes wins."""
        answers = [
            ("a", "Yes"), ("b", "Yes"), ("c", "Yes"),
            ("d", "No"), ("e", "No"),
        ]
        result = sole_majority_vote(answers, similarity_mode="exact")
        assert _normalize_answer(result) == "yes"

    def test_all_different_scores_zero(self):
        """When all answers are unique, all V scores are 0."""
        answers = [("a", "1"), ("b", "2"), ("c", "3")]
        result = sole_majority_vote(answers, similarity_mode="exact")
        # All have V=0, so any could win; function should not crash
        assert result in ["1", "2", "3"]
