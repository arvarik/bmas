# /opt/bmas/daemon/src/core/triage.py
"""
Semantic Triage Router.

Classifies task complexity and routes to the appropriate LiteLLM model
alias via the MODEL_ROUTING config table.

Supports two backends:
  - gemini: Routes classification through LiteLLM → Gemini API (default).
    No GPU required. Uses the same label extraction logic.
  - local:  Uses a local vLLM server (Qwen3-1.7B) with guided_choice
    constrained decoding. Requires GPU + docker compose --profile gpu.

Hardened label extraction strips markdown formatting and falls back
to MEDIUM if no valid tier is found (safe default: never under-routes).
Qwen3's <think> tags are stripped from responses via regex.
"""

import re
from dataclasses import dataclass
from enum import Enum

import httpx

from config import MODEL_ROUTING as _CONFIG_ROUTING
from config import (
    TRIAGE_BACKEND,
    TRIAGE_DEFAULT_COMPLEXITY,
    TRIAGE_ENABLED,
    TRIAGE_GEMINI_MODEL,
    TRIAGE_LOCAL_MODEL,
)


class Complexity(Enum):
    SIMPLE = "simple"     # Route to edge nodes (free)
    LIGHT = "light"       # Route to Gemini Flash Lite (cheap cloud)
    MEDIUM = "medium"     # Route to Gemini Flash ($)
    COMPLEX = "complex"   # Route to Gemini Pro ($$)


@dataclass
class TriageResult:
    complexity: Complexity
    litellm_model: str


# Constrained decoding labels — vLLM will mask all tokens except these
GUIDED_CHOICE_LABELS = ["SIMPLE", "LIGHT", "MEDIUM", "COMPLEX"]
_VALID_TIERS = set(GUIDED_CHOICE_LABELS)

TRIAGE_SYSTEM_PROMPT = """You are a task complexity classifier for a distributed AI system.
Classify the given task into exactly one tier based on the ACTUAL TASK requested, ignoring any background context or described environment.

Tier definitions:
- SIMPLE: Factual lookups, basic formatting, unit conversions, single-step operations. Answer is a known fact or mechanical transform.
- LIGHT: Simple extraction, short translations, regex/pattern generation, 1-3 sentence summaries, short comparisons or lists.
- MEDIUM: Single-function coding tasks, focused technical explanations (even with multiple examples), email/document drafting, debugging a single issue, comparing 2-3 options on one topic. Produces a working artifact but does NOT require designing a whole system.
- COMPLEX: Multi-component system architecture, full application development, research synthesis across many sources, multi-system integration. Requires designing multiple interacting components or synthesizing across 3+ knowledge domains.

Examples:
- "What is the capital of France?" → SIMPLE
- "Remove whitespace from a string" → SIMPLE
- "Extract the email from this text" → LIGHT
- "Write a glob pattern for Python files" → LIGHT
- "List 3 differences between TCP and UDP" → LIGHT
- "Write a FastAPI endpoint with JWT auth" → MEDIUM
- "Explain the CAP theorem with examples for each tradeoff" → MEDIUM
- "Debug why a database query is slow" → MEDIUM
- "Design a microservices architecture for a trading platform" → COMPLEX

Respond with ONLY the tier name. /no_think
"""

# Precompiled regex for stripping Qwen3 thinking tags
_THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)

# Build MODEL_ROUTING from the config file's routing section.
# The config stores {"simple": "local", "medium": "gemini-flash", ...}
# and we convert to {Complexity.SIMPLE: "local", ...}
# "local" is a sentinel resolved at dispatch time via round-robin.
MODEL_ROUTING = {
    Complexity(tier): model
    for tier, model in _CONFIG_ROUTING.items()
}


def _extract_label(text: str) -> str:
    """Extract the first valid tier label from model output.

    Handles responses that may include markdown formatting
    (e.g. '**COMPLEX**') or extra text after the label.

    Falls back to MEDIUM if no valid tier is found — safest routing
    default since all observed misclassifications are over-routing.
    """
    cleaned = re.sub(r'[*_#\[\]():`]', ' ', text.upper())
    for word in cleaned.split():
        if word in _VALID_TIERS:
            return word
    return "MEDIUM"  # safe fallback: never under-routes


class TriageRouter:
    def __init__(
        self,
        triage_url: str,
        litellm_url: str,
        litellm_key: str,
    ):
        self.triage_url = triage_url
        self.litellm_url = litellm_url
        self.litellm_key = litellm_key
        self.client = httpx.AsyncClient(timeout=30.0)

    async def classify(
        self,
        task_description: str,
        routing_override: dict[str, str] | None = None,
    ) -> TriageResult:
        """Classify task complexity using the configured triage backend.

        Supports two backends (configured via triage.backend in bmas.yaml):
          - gemini: Routes through LiteLLM → Gemini API (default, no GPU).
          - local:  Uses local vLLM server with guided_choice constrained decoding.

        If triage is disabled in bmas.yaml, returns the default_complexity
        tier without making any API call.

        Args:
            task_description: The raw user task text to classify.
            routing_override: Optional dict mapping tier names (e.g. 'medium') to
                model aliases. When provided, overrides the static MODEL_ROUTING table
                for this classification. Supports both full and partial overrides.
        """
        # Build effective routing: static defaults → optional override
        effective_routing = dict(MODEL_ROUTING)  # copy to avoid mutation
        if routing_override:
            for tier_str, model in routing_override.items():
                try:
                    cplx = Complexity(tier_str.lower())
                    effective_routing[cplx] = model
                except ValueError:
                    pass  # ignore unknown tier names

        if not TRIAGE_ENABLED:
            complexity = Complexity(TRIAGE_DEFAULT_COMPLEXITY)
            return TriageResult(
                complexity=complexity,
                litellm_model=effective_routing.get(complexity, MODEL_ROUTING.get(complexity, "medium")),
            )

        if TRIAGE_BACKEND == "gemini":
            raw = await self._classify_gemini(task_description)
        else:
            raw = await self._classify_local(task_description)

        # Strip Qwen3's <think>...</think> tags if present
        cleaned = _THINK_RE.sub("", raw)
        label = _extract_label(cleaned)
        complexity = Complexity(label.lower())
        return TriageResult(
            complexity=complexity,
            litellm_model=effective_routing.get(complexity, MODEL_ROUTING.get(complexity, "medium")),
        )

    async def _classify_gemini(self, task_description: str) -> str:
        """Classify via LiteLLM → Gemini API (no GPU required).

        Uses the LiteLLM proxy with the configured model alias (e.g.
        'gemini-flash-lite'). No guided_choice — Gemini doesn't support
        constrained decoding; label extraction handles free-form output.
        """
        response = await self.client.post(
            f"{self.litellm_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.litellm_key}"},
            json={
                "model": TRIAGE_GEMINI_MODEL,
                "messages": [
                    {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                    {"role": "user", "content": task_description},
                ],
                "max_tokens": 10,
                "temperature": 0.1,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _classify_local(self, task_description: str) -> str:
        """Classify via local vLLM server with guided_choice constrained decoding.

        Uses the triage vLLM container directly (not through LiteLLM).
        guided_choice constrains output to valid tier labels at the token level.
        """
        response = await self.client.post(
            f"{self.triage_url}/chat/completions",
            json={
                "model": TRIAGE_LOCAL_MODEL,
                "messages": [
                    {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                    {"role": "user", "content": task_description},
                ],
                "max_tokens": 10,
                "temperature": 0.1,
                # vLLM constrained decoding — top-level param, NOT inside extra_body
                # (extra_body is an OpenAI Python SDK abstraction)
                "guided_choice": GUIDED_CHOICE_LABELS,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def route_and_execute(
        self, task_description: str, system_prompt: str = ""
    ) -> dict:
        """Classify, route, and execute a task through LiteLLM."""
        triage = await self.classify(task_description)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task_description})

        response = await self.client.post(
            f"{self.litellm_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.litellm_key}"},
            json={
                "model": triage.litellm_model,
                "messages": messages,
                "max_tokens": 65536,  # Full Gemini output limit — prevent truncation bugs
            },
        )
        response.raise_for_status()

        return {
            "triage": {
                "complexity": triage.complexity.value,
                "routed_to": triage.litellm_model,
            },
            "response": response.json(),
        }

    async def close(self):
        await self.client.aclose()
