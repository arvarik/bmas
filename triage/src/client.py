"""
vLLM HTTP client for the bMAS Semantic Triage Router.

Handles request construction, retry logic, Qwen3 <think> tag stripping,
and response parsing. Zero external dependencies — stdlib only.
"""

import json
import re
import time
import urllib.request
import urllib.error

# ── Model Configuration ─────────────────────────────────────────────────────

MODEL = "Qwen/Qwen3-1.7B"
TIERS = ["SIMPLE", "LIGHT", "MEDIUM", "COMPLEX"]
GUIDED_CHOICE = TIERS

SYSTEM_PROMPT = """You are a task complexity classifier for a distributed AI system.
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

Respond with ONLY the tier name. /no_think"""

# Precompiled regex for stripping Qwen3 thinking tags
_THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)

_VALID_TIERS = set(TIERS)


def _extract_label(text: str) -> str:
    """Extract the first valid tier label from model output.

    Handles Qwen3 responses that may include markdown formatting
    (e.g. '**COMPLEX**') or extra text after the label
    (e.g. 'LIGHT\\n\\nTHE API DIDN\\'T...').

    Falls back to MEDIUM if no valid tier is found — safest routing
    default since all observed misclassifications are over-routing.
    """
    # Strip markdown/formatting characters before scanning
    cleaned = re.sub(r'[*_#\[\]():`]', ' ', text.upper())
    for word in cleaned.split():
        if word in _VALID_TIERS:
            return word
    return "MEDIUM"  # safe fallback: never under-routes


# ── Public API ───────────────────────────────────────────────────────────────

def classify(url: str, task: str, retries: int = 2) -> dict:
    """Send a classification request to the vLLM server with retries.

    Args:
        url: vLLM base URL (e.g. "http://localhost:8001", no /v1 suffix).
        task: The task description to classify.
        retries: Number of retry attempts on failure (default 2).

    Returns:
        dict with keys: label, latency_s, completion_tokens, prompt_tokens,
        total_tokens, finish_reason, error.
    """
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ],
        "max_tokens": 10,
        "temperature": 0.1,
        "guided_choice": GUIDED_CHOICE,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_err = None
    for attempt in range(1, retries + 2):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                elapsed = time.perf_counter() - t0

                usage = data.get("usage", {})
                choice = data["choices"][0]
                raw = choice["message"]["content"]
                cleaned = _THINK_RE.sub("", raw)
                # Extract the first valid tier label from the response.
                # Qwen3 sometimes appends extra text after the label.
                label = _extract_label(cleaned)

                return {
                    "label": label,
                    "latency_s": elapsed,
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "finish_reason": choice.get("finish_reason", "unknown"),
                    "error": None,
                }
        except (urllib.error.HTTPError, urllib.error.URLError, Exception) as e:
            last_err = str(e)
            if attempt <= retries:
                time.sleep(0.5 * attempt)

    return {
        "label": "ERROR",
        "latency_s": 0.0,
        "completion_tokens": 0,
        "prompt_tokens": 0,
        "total_tokens": 0,
        "finish_reason": "error",
        "error": last_err,
    }

