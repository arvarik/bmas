"""The scorer plugin registry with every documented plugin class.

The registry implements deterministic answer scorers, structured
final-state verification, environment success verification, rubric
judges, trajectory scorers, human review scorers, and composite
scorers with explicit formulas. Every plugin declares its evidence
requirements and validates them before execution; missing evidence
returns one clear unavailable result, never a fabricated score. Every
plugin declares one trust class, and only repository-reviewed
deterministic plugins run inside the trusted service. Judges receive
blind candidate content: no runtime label enters a judge request, and
candidate order randomizes deterministically from the pinned seed.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from benchmarks.provenance import content_checksum

TRUST_CLASSES = (
    "repository_reviewed_deterministic",
    "sandboxed_wasi",
    "sandboxed_native",
)

PLUGIN_TYPES = (
    "deterministic",
    "final_state",
    "environment",
    "rubric_judge",
    "trajectory",
    "human_review",
    "composite",
)


class ScorerPluginError(ValueError):
    """The plugin request violates the scorer contract."""


def unavailable(missing: list[str]) -> dict[str, Any]:
    """Build the one clear unavailable result for missing evidence."""
    return {
        "status": "unavailable",
        "missing_evidence": sorted(missing),
        "dimensions": [],
        "passed": None,
        "explanation": (
            "Required evidence is unavailable: " + ", ".join(sorted(missing))
        ),
    }


def _require(
    evidence: dict[str, Any], requirements: tuple[str, ...],
) -> list[str]:
    return [name for name in requirements if evidence.get(name) is None]


def _scored(
    dimensions: list[dict[str, Any]],
    *,
    passed: bool | None,
    explanation: str,
    uncertainty: float | None = None,
) -> dict[str, Any]:
    return {
        "status": "scored",
        "dimensions": dimensions,
        "passed": passed,
        "explanation": explanation,
        "uncertainty": uncertainty,
    }


def _normalized(text: str) -> str:
    collapsed = " ".join(str(text).split())
    return unicodedata.normalize("NFC", collapsed).casefold()


# ── Deterministic answer scorers ─────────────────────────────────────


class DeterministicAnswerScorer:
    """Exact, normalized, numeric, choice, and assertion comparisons."""

    plugin_type = "deterministic"
    trust_class = "repository_reviewed_deterministic"
    evidence_requirements = ("final_output", "reference_answer")

    _COMPARISONS = (
        "exact",
        "normalized_exact",
        "numeric_tolerance",
        "multiple_choice",
        "structured_assertions",
    )

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        comparison = str(configuration.get("comparison") or "exact")
        if comparison not in self._COMPARISONS:
            raise ScorerPluginError(
                f"Unknown comparison: {comparison!r}"
            )
        requirements = (
            ("final_output",)
            if comparison == "structured_assertions"
            else self.evidence_requirements
        )
        missing = _require(evidence, requirements)
        if missing:
            return unavailable(missing)
        output = str(evidence["final_output"])
        reference = str(evidence.get("reference_answer") or "")
        handler = getattr(self, f"_{comparison}")
        return handler(output, reference, configuration)

    def _exact(
        self, output: str, reference: str, configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del configuration
        passed = output == reference
        return _scored(
            [{"name": "accuracy", "value": 1.0 if passed else 0.0,
              "category": None}],
            passed=passed,
            explanation="exact_match" if passed else "exact_mismatch",
        )

    def _normalized_exact(
        self, output: str, reference: str, configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del configuration
        passed = _normalized(output) == _normalized(reference)
        return _scored(
            [{"name": "accuracy", "value": 1.0 if passed else 0.0,
              "category": None}],
            passed=passed,
            explanation=(
                "normalized_match" if passed else "normalized_mismatch"
            ),
        )

    def _numeric_tolerance(
        self, output: str, reference: str, configuration: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            observed = float(output.strip())
            expected = float(reference.strip())
        except ValueError:
            return _scored(
                [{"name": "accuracy", "value": 0.0, "category": None}],
                passed=False,
                explanation="not_numeric",
            )
        absolute = float(configuration.get("absolute_tolerance", 0.0))
        relative = float(configuration.get("relative_tolerance", 0.0))
        allowed = max(absolute, relative * abs(expected))
        passed = abs(observed - expected) <= allowed
        return _scored(
            [{"name": "accuracy", "value": 1.0 if passed else 0.0,
              "category": None}],
            passed=passed,
            explanation=(
                "within_tolerance" if passed else "outside_tolerance"
            ),
        )

    def _multiple_choice(
        self, output: str, reference: str, configuration: dict[str, Any],
    ) -> dict[str, Any]:
        choices = [
            str(choice) for choice in configuration.get("choices") or []
        ]
        if not choices:
            raise ScorerPluginError(
                "multiple_choice names its choices"
            )
        tokens = _normalized(output).replace(".", " ").split()
        selected = next(
            (
                choice
                for choice in choices
                if _normalized(choice) in tokens
            ),
            None,
        )
        passed = selected is not None and (
            _normalized(selected) == _normalized(reference)
        )
        return _scored(
            [{"name": "accuracy", "value": 1.0 if passed else 0.0,
              "category": selected}],
            passed=passed,
            explanation=(
                f"selected_{selected}" if selected else "no_choice_found"
            ),
        )

    def _structured_assertions(
        self, output: str, reference: str, configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del reference
        assertions = configuration.get("assertions") or []
        if not assertions:
            raise ScorerPluginError(
                "structured_assertions names its assertions"
            )
        try:
            document = json.loads(output)
        except json.JSONDecodeError:
            return _scored(
                [{"name": "assertions_passed", "value": 0.0,
                  "category": None}],
                passed=False,
                explanation="output_not_json",
            )
        satisfied = 0
        failures: list[str] = []
        for assertion in assertions:
            pointer = str(assertion.get("pointer") or "")
            operator = str(assertion.get("operator") or "eq")
            expected = assertion.get("value")
            value = _resolve_pointer(document, pointer)
            if operator == "exists":
                success = value is not _ABSENT
            elif operator == "eq":
                success = value is not _ABSENT and value == expected
            elif operator == "ne":
                success = value is not _ABSENT and value != expected
            elif operator == "contains":
                success = isinstance(value, str) and str(expected) in value
            else:
                raise ScorerPluginError(
                    f"Unknown assertion operator: {operator!r}"
                )
            if success:
                satisfied += 1
            else:
                failures.append(pointer or "/")
        passed = satisfied == len(assertions)
        return _scored(
            [{"name": "assertions_passed",
              "value": satisfied / len(assertions), "category": None}],
            passed=passed,
            explanation=(
                "all_assertions_hold" if passed
                else "failed_assertions: " + ", ".join(failures)
            ),
        )


_ABSENT = object()


def _resolve_pointer(value: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return value
    current = value
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif (
            isinstance(current, list)
            and token.isdigit()
            and int(token) < len(current)
        ):
            current = current[int(token)]
        else:
            return _ABSENT
    return current


# ── Final-state and environment verification ─────────────────────────


class FinalStateVerifier:
    """Grade the final environment state without a reference answer."""

    plugin_type = "final_state"
    trust_class = "repository_reviewed_deterministic"
    evidence_requirements = ("final_state", "expected_final_state")

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del configuration
        missing = _require(evidence, self.evidence_requirements)
        if missing:
            return unavailable(missing)
        state = dict(
            (evidence["final_state"] or {}).get("state")
            or evidence["final_state"]
            or {},
        )
        expected = dict(evidence["expected_final_state"] or {})
        mismatched = sorted(
            key
            for key, value in expected.items()
            if key not in state or state[key] != value
        )
        passed = not mismatched
        return _scored(
            [{"name": "environment_success",
              "value": 1.0 if passed else 0.0, "category": None}],
            passed=passed,
            # The verifier follows the final state only; final prose
            # never changes this result.
            explanation=(
                "final_state_matches" if passed
                else "state_mismatch: " + ", ".join(mismatched)
            ),
        )


# ── Trajectory scoring ───────────────────────────────────────────────


class TrajectoryScorer:
    """Detect loops, false completion, forgotten constraints, recovery."""

    plugin_type = "trajectory"
    trust_class = "repository_reviewed_deterministic"
    evidence_requirements = ("trace_events",)

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        missing = _require(evidence, self.evidence_requirements)
        if missing:
            return unavailable(missing)
        events = list(evidence["trace_events"])
        loop_threshold = int(configuration.get("loop_threshold", 3))

        references: dict[str, list[int]] = {}

        def mark(name: str, index: int) -> None:
            references.setdefault(name, []).append(index)

        counts: dict[str, int] = {}
        loop_detected = False
        for index, event in enumerate(events):
            key = content_checksum({
                "kind": event.get("kind"),
                "action": event.get("action"),
            })
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= loop_threshold:
                loop_detected = True
                mark("loop", index)

        completion_claims = [
            index for index, event in enumerate(events)
            if event.get("kind") == "completion_claim"
        ]
        verified = any(
            event.get("kind") == "verified_success" for event in events
        )
        false_completion = bool(completion_claims) and not verified
        for index in completion_claims:
            if not verified:
                mark("false_completion", index)

        declared: set[str] = set()
        forgotten = False
        for index, event in enumerate(events):
            if event.get("kind") == "constraint_declared":
                declared.add(str(event.get("constraint")))
            if (
                event.get("kind") == "constraint_violated"
                and str(event.get("constraint")) in declared
            ):
                forgotten = True
                mark("forgotten_constraint", index)

        failure_indexes = [
            index for index, event in enumerate(events)
            if event.get("kind") == "failure"
        ]
        recovered = bool(failure_indexes) and any(
            index > failure_indexes[0]
            and event.get("kind") == "verified_success"
            for index, event in enumerate(events)
        )
        if recovered:
            mark("recovery", failure_indexes[0])

        dimensions = [
            {"name": "loop_free", "value": 0.0 if loop_detected else 1.0,
             "category": None},
            {"name": "no_false_completion",
             "value": 0.0 if false_completion else 1.0, "category": None},
            {"name": "constraints_kept",
             "value": 0.0 if forgotten else 1.0, "category": None},
            {"name": "recovered_from_failure",
             "value": 1.0 if recovered else 0.0, "category": None},
        ]
        return {
            **_scored(
                dimensions,
                passed=not (loop_detected or false_completion or forgotten),
                explanation="trajectory_analysis",
            ),
            "evidence_marks": {
                name: indexes for name, indexes in sorted(references.items())
            },
        }


# ── Rubric judges with blind identity ────────────────────────────────


def build_judge_request(
    *,
    rubric: dict[str, Any],
    candidates: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    """Build one blind judge request with randomized order.

    No runtime label, model name, or candidate identity enters the
    request. The order permutes deterministically from the pinned
    seed, and the mapping stays outside the request for later
    un-blinding.
    """
    order = sorted(
        range(len(candidates)),
        key=lambda index: hashlib.sha256(
            f"judge-order:{seed}:{index}".encode(),
        ).digest(),
    )
    blinded = [
        {
            "label": f"candidate-{position + 1}",
            "content": str(candidates[order[position]].get("content") or ""),
        }
        for position in range(len(order))
    ]
    request = {"rubric": rubric, "candidates": blinded}
    mapping = {
        f"candidate-{position + 1}": str(
            candidates[order[position]].get("candidate_id") or "",
        )
        for position in range(len(order))
    }
    return {
        "request": request,
        "request_digest": content_checksum(request),
        "order_mapping": mapping,
    }


class RubricJudgeScorer:
    """Score with one rubric through an injected judge transport."""

    plugin_type = "rubric_judge"
    trust_class = "sandboxed_wasi"
    evidence_requirements = ("candidates", "rubric")

    def __init__(self, judge: Any) -> None:
        self._judge = judge

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        missing = _require(evidence, self.evidence_requirements)
        if missing:
            return unavailable(missing)
        seed = int(configuration.get("seed", 0))
        built = build_judge_request(
            rubric=evidence["rubric"],
            candidates=evidence["candidates"],
            seed=seed,
        )
        try:
            response = self._judge(built["request"])
        except Exception as error:  # noqa: BLE001 — a judge fault is a scorer failure.
            return {
                "status": "error",
                "dimensions": [],
                "passed": None,
                "explanation": "judge_transport_failure",
                "error": str(error)[:500],
            }
        return {
            **_scored(
                [{"name": str(dimension.get("name") or "rubric"),
                  "value": dimension.get("value"),
                  "category": dimension.get("category")}
                 for dimension in response.get("dimensions") or []]
                or [{"name": "rubric", "value": None, "category": None}],
                passed=response.get("passed"),
                explanation=str(response.get("explanation") or "rubric"),
                uncertainty=response.get("uncertainty"),
            ),
            "judge": {
                "request_digest": built["request_digest"],
                "response_digest": content_checksum(response),
            },
        }


# ── Human review and composite scoring ───────────────────────────────


class HumanReviewScorer:
    """Adopt one recorded human review decision as a score."""

    plugin_type = "human_review"
    trust_class = "repository_reviewed_deterministic"
    evidence_requirements = ("human_review",)

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del configuration
        missing = _require(evidence, self.evidence_requirements)
        if missing:
            return unavailable(missing)
        review = dict(evidence["human_review"])
        if not review.get("reviewer") or "passed" not in review:
            raise ScorerPluginError(
                "A human review names its reviewer and its decision"
            )
        passed = bool(review["passed"])
        return _scored(
            [{"name": "human_review", "value": 1.0 if passed else 0.0,
              "category": None}],
            passed=passed,
            explanation=str(review.get("notes") or "human_review"),
        )


class CompositeScorer:
    """Combine child dimensions through one explicit formula."""

    plugin_type = "composite"
    trust_class = "repository_reviewed_deterministic"
    evidence_requirements = ("child_results",)

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        missing = _require(evidence, self.evidence_requirements)
        if missing:
            return unavailable(missing)
        weights = dict(configuration.get("weights") or {})
        if not weights:
            raise ScorerPluginError(
                "A composite scorer declares explicit weights"
            )
        values: dict[str, float] = {}
        for child in evidence["child_results"]:
            for dimension in child.get("dimensions") or []:
                if dimension.get("value") is not None:
                    values[str(dimension["name"])] = float(
                        dimension["value"],
                    )
        missing_children = sorted(set(weights) - set(values))
        if missing_children:
            return unavailable(missing_children)
        total_weight = sum(float(weight) for weight in weights.values())
        combined = sum(
            float(weight) * values[name]
            for name, weight in weights.items()
        ) / total_weight
        formula = " + ".join(
            f"{weights[name]}*{name}" for name in sorted(weights)
        )
        return _scored(
            [
                {"name": "composite", "value": combined, "category": None},
                *(
                    {"name": name, "value": values[name], "category": None}
                    for name in sorted(weights)
                ),
            ],
            passed=None,
            # The formula is explicit; one unexplained average never
            # stores.
            explanation=f"weighted_formula: ({formula}) / {total_weight}",
        )


def plugin_for(
    plugin_type: str, *, judge: Any = None,
) -> Any:
    """Return one plugin instance for the requested type."""
    if plugin_type == "deterministic":
        return DeterministicAnswerScorer()
    if plugin_type in ("final_state", "environment"):
        return FinalStateVerifier()
    if plugin_type == "trajectory":
        return TrajectoryScorer()
    if plugin_type == "rubric_judge":
        if judge is None:
            raise ScorerPluginError(
                "A rubric judge requires one judge transport"
            )
        return RubricJudgeScorer(judge)
    if plugin_type == "human_review":
        return HumanReviewScorer()
    if plugin_type == "composite":
        return CompositeScorer()
    raise ScorerPluginError(f"Unknown plugin type: {plugin_type!r}")
