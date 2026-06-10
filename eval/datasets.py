"""Benchmark dataset loaders — GSM8K + MMLU subset.

Two modes:
  1. Fixture mode (default): loads small bundled fixtures from eval/fixtures/
     for deterministic CI and offline testing.
  2. Download mode (--download): fetches full datasets from HuggingFace via
     plain HTTP for real evaluation runs.

See docs/proposals/10-migration-and-rollout.md Phase E bullet 1
and docs/proposals/15-novelty-and-research-directions.md §3.4.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("bmas.eval.datasets")

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# MMLU subjects matching the paper's evaluation beds (doc 15 §3.4)
MMLU_SUBJECTS = [
    "abstract_algebra",
    "college_chemistry",
    "global_facts",
    "machine_learning",
    "professional_medicine",
]


@dataclass
class EvalItem:
    """A single evaluation question with ground-truth answer."""

    id: str
    question: str
    answer: str
    dataset: str  # "gsm8k" | "mmlu"
    subject: str | None = None  # MMLU subject name, None for GSM8K

    def to_dict(self) -> dict:
        return asdict(self)


def load_gsm8k(limit: int | None = None) -> list[EvalItem]:
    """Load GSM8K items from bundled fixtures.

    The fixture format is JSONL with fields: {"question": str, "answer": str}
    where answer contains the chain-of-thought ending with "#### <number>".
    """
    fixture_path = FIXTURES_DIR / "gsm8k.jsonl"
    if not fixture_path.is_file():
        raise FileNotFoundError(
            f"GSM8K fixture not found at {fixture_path}. "
            "Run with --download to fetch, or check eval/fixtures/."
        )

    items: list[EvalItem] = []
    with open(fixture_path) as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            # Extract numeric answer after ####
            raw_answer = record["answer"]
            numeric = _extract_gsm8k_ground_truth(raw_answer)
            items.append(
                EvalItem(
                    id=f"gsm8k-{i}",
                    question=record["question"],
                    answer=numeric,
                    dataset="gsm8k",
                )
            )

    logger.info("Loaded %d GSM8K items from fixtures", len(items))
    return items


def load_mmlu(
    subjects: list[str] | None = None, limit: int | None = None
) -> list[EvalItem]:
    """Load MMLU items from bundled fixtures.

    The fixture format is JSONL with fields:
    {"question": str, "choices": [str, str, str, str], "answer": int, "subject": str}
    where answer is 0-indexed (0=A, 1=B, 2=C, 3=D).
    """
    subjects = subjects or MMLU_SUBJECTS
    fixture_path = FIXTURES_DIR / "mmlu.jsonl"
    if not fixture_path.is_file():
        raise FileNotFoundError(
            f"MMLU fixture not found at {fixture_path}. "
            "Run with --download to fetch, or check eval/fixtures/."
        )

    answer_map = {0: "A", 1: "B", 2: "C", 3: "D"}
    items: list[EvalItem] = []
    count = 0

    with open(fixture_path) as f:
        for line in f:
            if limit is not None and count >= limit:
                break
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            subj = record.get("subject", "")
            if subj not in subjects:
                continue

            choices = record["choices"]
            # Format as multiple-choice question
            formatted_q = record["question"] + "\n"
            for j, choice in enumerate(choices):
                letter = answer_map[j]
                formatted_q += f"  {letter}. {choice}\n"
            formatted_q += "\nAnswer with the letter (A, B, C, or D)."

            correct_letter = answer_map[record["answer"]]
            items.append(
                EvalItem(
                    id=f"mmlu-{subj}-{count}",
                    question=formatted_q,
                    answer=correct_letter,
                    dataset="mmlu",
                    subject=subj,
                )
            )
            count += 1

    logger.info("Loaded %d MMLU items from fixtures (%s)", len(items), subjects)
    return items


def _extract_gsm8k_ground_truth(answer_text: str) -> str:
    """Extract the numeric answer from GSM8K's '#### <number>' format.

    Returns the number as a string (stripped of commas).
    """
    if "####" in answer_text:
        after_marker = answer_text.split("####")[-1].strip()
        # Remove commas from numbers like "1,234"
        return after_marker.replace(",", "").strip()
    # Fallback: try to find the last number in the text
    import re

    numbers = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", answer_text)
    if numbers:
        return numbers[-1].replace(",", "")
    return answer_text.strip()
