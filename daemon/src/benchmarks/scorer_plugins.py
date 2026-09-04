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
    "reliability",
    "wasi_component",
    "native_microvm",
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
        "last_number",
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

    def _last_number(
        self, output: str, reference: str, configuration: dict[str, Any],
    ) -> dict[str, Any]:
        """The ported grade-school numeric convention.

        The final numeric value is the answer: a ``####`` marker wins,
        then an explicit answer phrase, then the last number in the
        response. Numbers compare after comma stripping and integer
        normalization.
        """
        del configuration
        extracted = extract_last_number(output)
        if extracted is None:
            return _scored(
                [{"name": "accuracy", "value": 0.0, "category": None}],
                passed=False,
                explanation="no_answer",
            )
        passed = normalize_number(extracted) == normalize_number(reference)
        return _scored(
            [{"name": "accuracy", "value": 1.0 if passed else 0.0,
              "category": extracted}],
            passed=passed,
            explanation="numeric_match" if passed else "numeric_mismatch",
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

_NUMBER_PATTERN = r"-?\d+(?:,\d{3})*(?:\.\d+)?"


def extract_last_number(text: str) -> str | None:
    """Extract the final numeric answer from one free-form response."""
    import re

    if not text or not text.strip():
        return None
    if "####" in text:
        after = text.split("####")[-1].strip()
        numbers = re.findall(_NUMBER_PATTERN, after)
        if numbers:
            return numbers[0].replace(",", "")
    for pattern in (
        r"(?:the\s+)?answer\s+is\s*[:\s]*(" + _NUMBER_PATTERN + ")",
        r"answer\s*:\s*(" + _NUMBER_PATTERN + ")",
        r"=\s*\$?\s*(" + _NUMBER_PATTERN + r")\s*$",
    ):
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).replace(",", "")
    numbers = re.findall(_NUMBER_PATTERN, text)
    return numbers[-1].replace(",", "") if numbers else None


def normalize_number(text: str) -> str:
    """Normalize one numeric string with exact decimal arithmetic."""
    from decimal import Decimal, InvalidOperation

    cleaned = str(text).strip().replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return cleaned
    normalized = value.normalize()
    return format(normalized, "f")


# ── Reliability scoring ported from the soak harness ─────────────────


def _rate(values: list[bool]) -> float:
    return sum(bool(value) for value in values) / len(values) if values else 0.0


def _nearest_rank(values: list[float], percentile: float) -> float:
    import math

    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(len(ordered) * percentile / 100)
    return float(ordered[max(0, min(len(ordered) - 1, rank - 1))])


def validate_trial_outcome(outcome: dict[str, Any]) -> None:
    """Reject one inconsistent soak trial outcome before scoring."""
    if int(outcome.get("effective_actions", 0)) < 0:
        raise ScorerPluginError("effective_actions must not be negative")
    expected = int(outcome.get("retrieval_expected", 0))
    found = int(outcome.get("retrieval_found", 0))
    if expected < 0 or found < 0:
        raise ScorerPluginError("retrieval counts must not be negative")
    if found > expected:
        raise ScorerPluginError("retrieval_found exceeds retrieval_expected")
    if int(outcome.get("minority_corrections", 0)) > int(
        outcome.get("minority_opportunities", 0),
    ):
        raise ScorerPluginError(
            "minority_corrections exceeds minority_opportunities"
        )
    if outcome.get("restart_recovered") and not outcome.get(
        "restart_attempted",
    ):
        raise ScorerPluginError(
            "restart recovery requires a restart attempt"
        )


class ReliabilityScorer:
    """The long-horizon reliability measures ported from the soak harness.

    The plugin reads one list of trial outcomes for one configuration
    and horizon and reports every soak measure as one named dimension:
    exact task success, strict repeated-run success, false completion,
    reliability decay against the declared baseline success, restart
    recovery, duplicate external actions, budget overshoot, context
    retrieval recall, stalls, replans, unresolved conflicts, minority
    corrections, and average effective actions. Role measurements
    report as evidence marks with nearest-rank percentiles.
    """

    plugin_type = "reliability"
    trust_class = "repository_reviewed_deterministic"
    evidence_requirements = ("trial_outcomes",)

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        missing = _require(evidence, self.evidence_requirements)
        if missing:
            return unavailable(missing)
        outcomes = [dict(outcome) for outcome in evidence["trial_outcomes"]]
        if not outcomes:
            return unavailable(["trial_outcomes"])
        for outcome in outcomes:
            validate_trial_outcome(outcome)
        exact = [bool(o.get("exact_success")) for o in outcomes]
        exact_success = _rate(exact)
        restart_trials = [o for o in outcomes if o.get("restart_attempted")]
        overshoots = [
            max(0.0, float(o.get("budget_spent_usd", 0.0))
                - float(o.get("budget_limit_usd", 0.0)))
            for o in outcomes
        ]
        retrieval_expected = sum(int(o.get("retrieval_expected", 0))
                                 for o in outcomes)
        retrieval_found = sum(int(o.get("retrieval_found", 0))
                              for o in outcomes)
        minority_opportunities = sum(int(o.get("minority_opportunities", 0))
                                     for o in outcomes)
        minority_corrections = sum(int(o.get("minority_corrections", 0))
                                   for o in outcomes)
        baseline = configuration.get("baseline_success")
        duplicates = sum(
            len(o.get("external_action_keys") or [])
            - len(set(o.get("external_action_keys") or []))
            for o in outcomes
        )
        dimensions: list[dict[str, Any]] = [
            {"name": "exact_task_success", "value": exact_success,
             "category": None},
            {"name": "strict_repeated_run_success",
             "value": 1.0 if all(exact) else 0.0, "category": None},
            {"name": "false_completion_rate",
             "value": _rate([bool(o.get("completed"))
                             and not bool(o.get("exact_success"))
                             for o in outcomes]), "category": None},
            {"name": "reliability_decay",
             "value": (float(baseline) - exact_success
                       if baseline is not None else None),
             "category": None},
            {"name": "restart_recovery_rate",
             "value": _rate([bool(o.get("restart_recovered"))
                             for o in restart_trials]), "category": None},
            {"name": "duplicate_external_actions", "value": float(duplicates),
             "category": None},
            {"name": "budget_overshoot_rate",
             "value": _rate([value > 0 for value in overshoots]),
             "category": None},
            {"name": "context_retrieval_recall",
             "value": (retrieval_found / retrieval_expected
                       if retrieval_expected else 1.0), "category": None},
            {"name": "stall_count",
             "value": float(sum(int(o.get("stall_count", 0))
                                for o in outcomes)), "category": None},
            {"name": "replan_count",
             "value": float(sum(int(o.get("replan_count", 0))
                                for o in outcomes)), "category": None},
            {"name": "unresolved_conflict_count",
             "value": float(sum(int(o.get("unresolved_conflicts", 0))
                                for o in outcomes)), "category": None},
            {"name": "minority_correction_rate",
             "value": (minority_corrections / minority_opportunities
                       if minority_opportunities else 1.0),
             "category": None},
            {"name": "average_effective_actions",
             "value": sum(int(o.get("effective_actions", 0))
                          for o in outcomes) / len(outcomes),
             "category": None},
        ]
        roles: dict[str, list[dict[str, Any]]] = {}
        for outcome in outcomes:
            for measurement in outcome.get("role_measurements") or []:
                roles.setdefault(str(measurement["role"]), []).append(
                    measurement,
                )
        role_metrics = {}
        for role, measurements in sorted(roles.items()):
            latencies = [float(m.get("latency_ms", 0.0)) for m in measurements]
            costs = [float(m.get("cost_usd", 0.0)) for m in measurements]
            role_metrics[role] = {
                "activations": len(measurements),
                "total_cost_usd": sum(costs),
                "average_cost_usd": sum(costs) / len(costs),
                "average_latency_ms": sum(latencies) / len(latencies),
                "p95_latency_ms": _nearest_rank(latencies, 95),
            }
        return {
            **_scored(
                dimensions,
                passed=all(exact),
                explanation=f"reliability over {len(outcomes)} trials",
            ),
            "evidence_marks": {"role_metrics": role_metrics,
                               "trials": len(outcomes)},
        }


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


def _judge_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Keep the token counts and the provider cost text of one judge call."""
    kept: dict[str, Any] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        if isinstance(value, (int, float)) and value >= 0:
            kept[name] = int(value)
    cost = usage.get("provider_cost_text", usage.get("cost"))
    if isinstance(cost, (int, float)) and cost >= 0:
        kept["provider_cost_text"] = f"{float(cost):.9f}"
    elif isinstance(cost, str) and cost.strip():
        kept["provider_cost_text"] = cost.strip()
    return kept


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
                **({"usage": _judge_usage(response["usage"])}
                   if isinstance(response.get("usage"), dict) else {}),
                **({"model": str(response["model"])}
                   if response.get("model") else {}),
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


class WasiComponentScorer:
    """One untrusted scorer compiled as a WebAssembly component.

    The component runs inside the pinned Wasmtime runtime through the
    boundary contract; this plugin only carries the artifact and the
    evidence the host marshals into the component.
    """

    plugin_type = "wasi_component"
    trust_class = "sandboxed_wasi"
    evidence_requirements = ("final_output", "reference_answer")

    def __init__(self, component: Any) -> None:
        self.component = component

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del evidence, configuration
        raise ScorerPluginError(
            "A component scorer executes only inside the Wasmtime boundary"
        )


class NativeMicroVmScorer:
    """One native scorer that runs only inside a pinned microVM."""

    plugin_type = "native_microvm"
    trust_class = "sandboxed_native"
    evidence_requirements = ("final_output", "reference_answer")

    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def score(
        self, evidence: dict[str, Any], configuration: dict[str, Any],
    ) -> dict[str, Any]:
        del evidence, configuration
        raise ScorerPluginError(
            "A native scorer executes only inside the microVM boundary"
        )


def plugin_for(
    plugin_type: str, *, judge: Any = None, component: Any = None,
    microvm: Any = None,
) -> Any:
    """Return one plugin instance for the requested type."""
    if plugin_type == "wasi_component":
        if component is None:
            raise ScorerPluginError(
                "A component scorer requires one compiled component"
            )
        return WasiComponentScorer(component)
    if plugin_type == "native_microvm":
        if microvm is None:
            raise ScorerPluginError(
                "A native scorer requires one microVM runner"
            )
        return NativeMicroVmScorer(microvm)
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
    if plugin_type == "reliability":
        return ReliabilityScorer()
    raise ScorerPluginError(f"Unknown plugin type: {plugin_type!r}")
