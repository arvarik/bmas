# /opt/bmas/daemon/triage_router.py
"""
Semantic Triage Router.
Uses the local Qwen3-1.7B model to classify task complexity and route
to the appropriate LiteLLM model alias via the MODEL_ROUTING config table.

vLLM's guided_choice constrained decoding constrains output tokens.
Hardened label extraction strips markdown formatting and falls back
to MEDIUM if no valid tier is found (safe default: never under-routes).
Qwen3's <think> tags are stripped from responses via regex.
"""

import re

import httpx
from enum import Enum
from dataclasses import dataclass

from config import MODEL_ROUTING as _CONFIG_ROUTING
from config import TRIAGE_ENABLED, TRIAGE_DEFAULT_COMPLEXITY, TRIAGE_MODEL


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
# The config stores {"simple": "edge-node-1", "medium": "gemini-flash", ...}
# and we convert to {Complexity.SIMPLE: "edge-node-1", ...}
MODEL_ROUTING = {
    Complexity(tier): model
    for tier, model in _CONFIG_ROUTING.items()
}


def _extract_label(text: str) -> str:
    """Extract the first valid tier label from model output.

    Handles Qwen3 responses that may include markdown formatting
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

    async def classify(self, task_description: str) -> TriageResult:
        """Classify task complexity using the local triage model with guided_choice.

        If triage is disabled in bmas.yaml, returns the default_complexity
        tier without making any API call.
        """
        if not TRIAGE_ENABLED:
            complexity = Complexity(TRIAGE_DEFAULT_COMPLEXITY)
            return TriageResult(
                complexity=complexity,
                litellm_model=MODEL_ROUTING[complexity],
            )

        response = await self.client.post(
            f"{self.triage_url}/chat/completions",
            json={
                "model": TRIAGE_MODEL,
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

        raw = response.json()["choices"][0]["message"]["content"]
        # Strip Qwen3's <think>...</think> tags if present
        cleaned = _THINK_RE.sub("", raw)
        label = _extract_label(cleaned)
        complexity = Complexity(label.lower())
        return TriageResult(
            complexity=complexity,
            litellm_model=MODEL_ROUTING[complexity],
        )

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
                "max_tokens": 2048,
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
