"""Shared fixtures for eval tests."""

import pytest

from eval.scorer import ScoredResult


@pytest.fixture
def gsm8k_correct_responses():
    """Sample GSM8K responses that should score as correct."""
    return [
        # (expected_answer, model_response)
        ("42", "Let me work through this step by step.\n\nFirst we add 20 + 22 = 42.\n\nThe answer is 42."),
        ("72", "Natalia sold 48/2 = 24 clips in May.\nSo total = 48 + 24 = 72.\n#### 72"),
        ("10", "Weng earns 12/60 = $0.2 per minute.\nWorking 50 minutes = 0.2 * 50 = $10.\n#### 10"),
        ("5", "Betty needs 100 - 50 - 30 - 15 = $5 more.\n#### 5"),
        ("1234", "The total comes to 1,234 items."),
        ("-7", "After subtracting, we get -7 as the final answer."),
        ("3.14", "The value of pi is approximately 3.14."),
    ]


@pytest.fixture
def gsm8k_wrong_responses():
    """Sample GSM8K responses that should score as incorrect."""
    return [
        # (expected_answer, model_response)
        ("42", "The answer is 43."),
        ("72", "I think it's about 100 clips total.\n#### 100"),
        ("10", "She earned $12 for the hour."),
    ]


@pytest.fixture
def gsm8k_no_answer_responses():
    """Sample GSM8K responses with no extractable answer."""
    return [
        # (expected_answer, model_response)
        ("42", "I don't know the answer to this question."),
        ("72", "This is a complex problem that requires more information."),
        ("10", ""),
    ]


@pytest.fixture
def mmlu_correct_responses():
    """Sample MMLU responses that should score as correct."""
    return [
        # (expected_letter, model_response)
        ("B", "The answer is B"),
        ("C", "(C)"),
        ("D", "I think the correct answer is D because the symptoms indicate..."),
        ("A", "A"),
        ("B", "**B**"),
        ("A", "A. This is correct because..."),
        ("D", "The correct answer is D."),
    ]


@pytest.fixture
def mmlu_wrong_responses():
    """Sample MMLU responses that should score as incorrect."""
    return [
        # (expected_letter, model_response)
        ("B", "The answer is A"),
        ("A", "(C) is correct"),
        ("D", "B"),
    ]


@pytest.fixture
def mmlu_no_answer_responses():
    """Sample MMLU responses with no extractable letter."""
    return [
        # (expected_letter, model_response)
        ("B", "I'm not sure about this one."),
        ("A", "This requires more context to answer properly."),
        ("C", ""),
    ]


@pytest.fixture
def sample_scored_results():
    """A mixed set of scored results for aggregate testing."""
    return [
        ScoredResult(
            id="gsm8k-0", question="Q0", expected_answer="42",
            actual_response="The answer is 42", dataset="gsm8k", subject=None,
            extracted_answer="42", correct=True, score_method="numeric_match",
            task_id="task-001", duration_ms=5000, cost_usd=0.01, tokens=500,
            model_used="gemini-flash", terminated_by="solution",
        ),
        ScoredResult(
            id="gsm8k-1", question="Q1", expected_answer="72",
            actual_response="The answer is 72", dataset="gsm8k", subject=None,
            extracted_answer="72", correct=True, score_method="numeric_match",
            task_id="task-002", duration_ms=3000, cost_usd=0.008, tokens=400,
            model_used="gemini-flash", terminated_by="solution",
        ),
        ScoredResult(
            id="gsm8k-2", question="Q2", expected_answer="10",
            actual_response="The answer is 15", dataset="gsm8k", subject=None,
            extracted_answer="15", correct=False, score_method="numeric_match",
            task_id="task-003", duration_ms=8000, cost_usd=0.02, tokens=800,
            model_used="gemini-pro", terminated_by="solution",
        ),
        ScoredResult(
            id="mmlu-0", question="Q3", expected_answer="B",
            actual_response="B", dataset="mmlu", subject="abstract_algebra",
            extracted_answer="B", correct=True, score_method="letter_match",
            task_id="task-004", duration_ms=2000, cost_usd=0.005, tokens=200,
            model_used="gemini-flash", terminated_by="solution",
        ),
        ScoredResult(
            id="mmlu-1", question="Q4", expected_answer="C",
            actual_response="A", dataset="mmlu", subject="abstract_algebra",
            extracted_answer="A", correct=False, score_method="letter_match",
            task_id="task-005", duration_ms=2500, cost_usd=0.005, tokens=250,
            model_used="gemini-flash", terminated_by="solution",
        ),
        ScoredResult(
            id="mmlu-2", question="Q5", expected_answer="D",
            actual_response="D", dataset="mmlu", subject="machine_learning",
            extracted_answer="D", correct=True, score_method="letter_match",
            task_id="task-006", duration_ms=1500, cost_usd=0.004, tokens=180,
            model_used="gemini-flash-lite", terminated_by="solution",
        ),
        ScoredResult(
            id="mmlu-3", question="Q6", expected_answer="A",
            actual_response="A", dataset="mmlu", subject="machine_learning",
            extracted_answer="A", correct=True, score_method="letter_match",
            task_id="task-007", duration_ms=1800, cost_usd=0.004, tokens=190,
            model_used="gemini-flash-lite", terminated_by="solution",
        ),
    ]


@pytest.fixture
def sample_scored_all_correct():
    """10 scored results all correct — for 100% accuracy test."""
    return [
        ScoredResult(
            id=f"item-{i}", question=f"Q{i}", expected_answer=str(i),
            actual_response=str(i), dataset="gsm8k", subject=None,
            extracted_answer=str(i), correct=True, score_method="numeric_match",
            task_id=f"task-{i:03d}", duration_ms=1000 + i * 100,
            cost_usd=0.01, tokens=100, model_used="gemini-flash",
            terminated_by="solution",
        )
        for i in range(10)
    ]


@pytest.fixture
def sample_scored_all_wrong():
    """10 scored results all incorrect — for 0% accuracy test."""
    return [
        ScoredResult(
            id=f"item-{i}", question=f"Q{i}", expected_answer=str(i),
            actual_response=str(i + 1), dataset="gsm8k", subject=None,
            extracted_answer=str(i + 1), correct=False, score_method="numeric_match",
            task_id=f"task-{i:03d}", duration_ms=2000 + i * 200,
            cost_usd=0.02, tokens=200, model_used="gemini-pro",
            terminated_by="solution",
        )
        for i in range(10)
    ]
