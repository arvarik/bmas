"""Tests for the GSM8K and MMLU scorers.

Covers:
  - GSM8K: numeric extraction (####, "the answer is", last-number fallback),
    commas, negatives, decimals, no-answer, wrong-answer
  - MMLU: letter extraction (explicit pattern, parenthesized, bold, start-of-line),
    case normalization, no-answer, wrong-answer
  - Aggregate: accuracy computation, per-subject breakdown, empty dataset

See docs/proposals/10-migration-and-rollout.md Phase E.
"""

import pytest

from eval.scorer import (
    score_gsm8k,
    score_mmlu,
    compute_accuracy,
    compute_accuracy_by_subject,
    ScoredResult,
    _extract_gsm8k_answer,
    _extract_mmlu_letter,
    _normalize_number,
)


# ═══════════════════════════════════════════════════════════════════════
# GSM8K Scorer Tests
# ═══════════════════════════════════════════════════════════════════════


class TestGSM8KScorer:
    """Tests for score_gsm8k and its internal extraction helpers."""

    def test_correct_with_answer_is_phrase(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[0]  # "42"
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "42"
        assert method == "numeric_match"

    def test_correct_with_hash_marker(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[1]  # "72" with ####
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "72"
        assert method == "numeric_match"

    def test_correct_with_hash_marker_10(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[2]  # "10" with ####
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "10"

    def test_correct_dollar_5(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[3]  # "5" with ####
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "5"

    def test_comma_separated_number(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[4]  # "1234" from "1,234"
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "1234"

    def test_negative_number(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[5]  # "-7"
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "-7"

    def test_decimal_number(self, gsm8k_correct_responses):
        expected, response = gsm8k_correct_responses[6]  # "3.14"
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is True
        assert extracted == "3.14"

    def test_wrong_answer(self, gsm8k_wrong_responses):
        expected, response = gsm8k_wrong_responses[0]  # expect 42, get 43
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is False
        assert extracted == "43"
        assert method == "numeric_match"

    def test_wrong_answer_with_hash(self, gsm8k_wrong_responses):
        expected, response = gsm8k_wrong_responses[1]  # expect 72, get 100
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is False
        assert extracted == "100"

    def test_wrong_answer_different(self, gsm8k_wrong_responses):
        expected, response = gsm8k_wrong_responses[2]  # expect 10, get 12
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is False
        assert extracted == "12"

    def test_no_answer_found(self, gsm8k_no_answer_responses):
        expected, response = gsm8k_no_answer_responses[0]
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is False
        assert method == "no_answer"

    def test_no_answer_complex_text(self, gsm8k_no_answer_responses):
        expected, response = gsm8k_no_answer_responses[1]
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is False
        assert extracted is None
        assert method == "no_answer"

    def test_empty_response(self, gsm8k_no_answer_responses):
        expected, response = gsm8k_no_answer_responses[2]
        extracted, correct, method = score_gsm8k(expected, response)
        assert correct is False
        assert extracted is None
        assert method == "no_answer"

    def test_multi_number_picks_last(self):
        """GSM8K convention: last number is the answer."""
        extracted, correct, method = score_gsm8k(
            "42", "We compute 10 + 20 = 30, then add 12 to get 42."
        )
        assert correct is True
        assert extracted == "42"

    def test_multi_number_wrong_picks_last(self):
        extracted, correct, method = score_gsm8k(
            "42", "We compute 42 - 10 = 32, so the answer is 32."
        )
        assert correct is False
        assert extracted == "32"


class TestGSM8KExtraction:
    """Tests for the internal _extract_gsm8k_answer helper."""

    def test_hash_marker(self):
        assert _extract_gsm8k_answer("blah\n#### 42") == "42"

    def test_hash_marker_with_comma(self):
        assert _extract_gsm8k_answer("blah\n#### 1,234") == "1234"

    def test_answer_is_pattern(self):
        assert _extract_gsm8k_answer("The answer is 99.") == "99"

    def test_answer_colon_pattern(self):
        assert _extract_gsm8k_answer("answer: 55") == "55"

    def test_last_number_fallback(self):
        assert _extract_gsm8k_answer("First 10 then 20 then 30") == "30"

    def test_no_numbers(self):
        assert _extract_gsm8k_answer("No numbers here") is None

    def test_empty_string(self):
        assert _extract_gsm8k_answer("") is None


class TestNumberNormalization:
    """Tests for _normalize_number."""

    def test_integer(self):
        assert _normalize_number("42") == "42"

    def test_float_integer(self):
        assert _normalize_number("42.0") == "42"

    def test_float(self):
        assert _normalize_number("3.14") == "3.14"

    def test_comma(self):
        assert _normalize_number("1,234") == "1234"

    def test_whitespace(self):
        assert _normalize_number("  42  ") == "42"

    def test_negative(self):
        assert _normalize_number("-7") == "-7"


# ═══════════════════════════════════════════════════════════════════════
# MMLU Scorer Tests
# ═══════════════════════════════════════════════════════════════════════


class TestMMLUScorer:
    """Tests for score_mmlu and its internal extraction helpers."""

    def test_answer_is_pattern(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[0]  # "The answer is B"
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "B"
        assert method == "letter_match"

    def test_parenthesized(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[1]  # "(C)"
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "C"

    def test_sentence_with_letter(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[2]  # "correct answer is D"
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "D"

    def test_standalone_letter(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[3]  # "A"
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "A"

    def test_bold_letter(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[4]  # "**B**"
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "B"

    def test_letter_with_period(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[5]  # "A. This is correct..."
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "A"

    def test_correct_answer_is_d(self, mmlu_correct_responses):
        expected, response = mmlu_correct_responses[6]  # "The correct answer is D."
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is True
        assert extracted == "D"

    def test_lowercase_normalization(self):
        extracted, correct, method = score_mmlu("B", "b")
        assert correct is True
        assert extracted == "B"

    def test_wrong_letter(self, mmlu_wrong_responses):
        expected, response = mmlu_wrong_responses[0]  # expect B, get A
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is False
        assert extracted == "A"

    def test_wrong_parenthesized(self, mmlu_wrong_responses):
        expected, response = mmlu_wrong_responses[1]  # expect A, get C
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is False
        assert extracted == "C"

    def test_wrong_standalone(self, mmlu_wrong_responses):
        expected, response = mmlu_wrong_responses[2]  # expect D, get B
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is False
        assert extracted == "B"

    def test_no_answer(self, mmlu_no_answer_responses):
        expected, response = mmlu_no_answer_responses[0]
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is False
        assert method == "no_answer"

    def test_empty_response(self, mmlu_no_answer_responses):
        expected, response = mmlu_no_answer_responses[2]
        extracted, correct, method = score_mmlu(expected, response)
        assert correct is False
        assert extracted is None
        assert method == "no_answer"


class TestMMLUExtraction:
    """Tests for the internal _extract_mmlu_letter helper."""

    def test_the_answer_is(self):
        assert _extract_mmlu_letter("The answer is B") == "B"

    def test_answer_colon(self):
        assert _extract_mmlu_letter("Answer: C") == "C"

    def test_parenthesized(self):
        assert _extract_mmlu_letter("(D)") == "D"

    def test_bold(self):
        assert _extract_mmlu_letter("**A**") == "A"

    def test_with_period(self):
        assert _extract_mmlu_letter("B. Because this option...") == "B"

    def test_lowercase(self):
        assert _extract_mmlu_letter("the answer is c") == "C"

    def test_single_letter_short(self):
        assert _extract_mmlu_letter("D") == "D"

    def test_no_letter_long_text(self):
        result = _extract_mmlu_letter(
            "This is a long response that discusses the topic at length "
            "without ever committing to a specific answer letter."
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Aggregate Scorer Tests
# ═══════════════════════════════════════════════════════════════════════


class TestAggregateAccuracy:
    """Tests for compute_accuracy and compute_accuracy_by_subject."""

    def test_100_percent(self, sample_scored_all_correct):
        acc = compute_accuracy(sample_scored_all_correct)
        assert acc == 1.0

    def test_0_percent(self, sample_scored_all_wrong):
        acc = compute_accuracy(sample_scored_all_wrong)
        assert acc == 0.0

    def test_mixed(self, sample_scored_results):
        # 5 correct out of 7: gsm8k-0, gsm8k-1 correct; gsm8k-2 wrong;
        # mmlu-0 correct, mmlu-1 wrong, mmlu-2 correct, mmlu-3 correct
        acc = compute_accuracy(sample_scored_results)
        assert acc == pytest.approx(5 / 7)

    def test_empty_dataset(self):
        acc = compute_accuracy([])
        assert acc == 0.0

    def test_single_correct(self):
        results = [
            ScoredResult(
                id="x", question="Q", expected_answer="1",
                actual_response="1", dataset="gsm8k", subject=None,
                extracted_answer="1", correct=True, score_method="numeric_match",
            )
        ]
        assert compute_accuracy(results) == 1.0

    def test_single_wrong(self):
        results = [
            ScoredResult(
                id="x", question="Q", expected_answer="1",
                actual_response="2", dataset="gsm8k", subject=None,
                extracted_answer="2", correct=False, score_method="numeric_match",
            )
        ]
        assert compute_accuracy(results) == 0.0


class TestSubjectAccuracy:
    """Tests for compute_accuracy_by_subject."""

    def test_subject_breakdown(self, sample_scored_results):
        by_subject = compute_accuracy_by_subject(sample_scored_results)
        # abstract_algebra: mmlu-0 correct, mmlu-1 wrong → 0.5
        assert by_subject["abstract_algebra"] == pytest.approx(0.5)
        # machine_learning: mmlu-2 correct, mmlu-3 correct → 1.0
        assert by_subject["machine_learning"] == pytest.approx(1.0)

    def test_no_subjects(self, sample_scored_all_correct):
        # All GSM8K, no subjects
        by_subject = compute_accuracy_by_subject(sample_scored_all_correct)
        assert by_subject == {}

    def test_empty(self):
        by_subject = compute_accuracy_by_subject([])
        assert by_subject == {}
