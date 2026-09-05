"""Provider-aware completion parameters for control-plane model calls.

Every control-plane call (agent selection, answer generation, the
solution evaluator, triage, and the model-backed judge) asks the
gateway for a short structured reply. A reasoning model spends
completion tokens on reasoning before it writes that reply, so a
small ``max_tokens`` truncates the reply and the caller reads an empty
or partial message. This module resolves one model alias to its
provider and model name from the configuration, decides whether the
model reasons by default, asks for a low reasoning effort where the
gateway maps it, gives the completion budget enough headroom for the
reasoning tokens, and omits the sampling parameters a provider rejects
or deprecates.

The gateway (LiteLLM) maps ``reasoning_effort`` to the Gemini thinking
level or budget, to the OpenAI reasoning models natively, and to the
Anthropic adaptive effort. Providers that do not reason by default
receive plain sampling parameters, so an unknown provider still works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REASONING_LEVELS = ("minimal", "low", "medium", "high")
REASONING_SETTINGS = ("provider_default", "off", *REASONING_LEVELS)

# Models that reason by default and count reasoning tokens inside the
# completion budget. The patterns match the provider's model name.
_REASONING_BY_DEFAULT: dict[str, tuple[re.Pattern[str], ...]] = {
    "gemini": (
        re.compile(r"^gemini-2\.5"),
        re.compile(r"^gemini-([3-9]|\d{2,})"),
        re.compile(r"^gemini-(flash|pro)-latest$"),
        re.compile(r"^gemini-flash-lite-latest$"),
    ),
    "openai": (
        re.compile(r"^o[1-9]"),
        re.compile(r"^gpt-([5-9]|\d{2,})"),
    ),
}
# Models whose provider rejects or deprecates ``temperature``.
_OMIT_TEMPERATURE: dict[str, tuple[re.Pattern[str], ...]] = {
    "gemini": (
        re.compile(r"^gemini-([3-9]|\d{2,})"),
        re.compile(r"^gemini-(flash|pro)-latest$"),
        re.compile(r"^gemini-flash-lite-latest$"),
    ),
    "openai": (
        re.compile(r"^o[1-9]"),
        re.compile(r"^gpt-([5-9]|\d{2,})"),
    ),
}
# Providers whose gateway mapping of ``reasoning_effort`` is safe to
# send whenever the model reasons by default or the operator asks.
_EFFORT_PROVIDERS = frozenset({"gemini", "openai", "anthropic", "vertex_ai"})
# The reasoning headroom for a reasoning model: the budget grows to at
# least this many tokens and to at least this multiple of the output.
_MIN_REASONING_BUDGET = 2048
_REASONING_MULTIPLIER = 4


@dataclass(frozen=True)
class ModelProfile:
    """One model alias resolved to its provider, model, and reasoning setting."""

    alias: str
    provider: str
    model: str
    reasoning: str = "provider_default"

    @property
    def reasons_by_default(self) -> bool:
        patterns = _REASONING_BY_DEFAULT.get(self.provider.lower(), ())
        return any(pattern.search(self.model) for pattern in patterns)

    @property
    def omits_temperature(self) -> bool:
        patterns = _OMIT_TEMPERATURE.get(self.provider.lower(), ())
        return any(pattern.search(self.model) for pattern in patterns)

    @property
    def accepts_effort(self) -> bool:
        return self.provider.lower() in _EFFORT_PROVIDERS


def profile_from_configuration(
    alias: str, configuration: dict[str, Any] | None,
) -> ModelProfile:
    """Build one profile from a ``models`` entry; an unknown alias is opaque."""
    if not configuration:
        return ModelProfile(alias=alias, provider="unknown", model=alias)
    reasoning = str(configuration.get("reasoning") or "provider_default")
    if reasoning not in REASONING_SETTINGS:
        raise ValueError(
            f"reasoning must be one of {', '.join(REASONING_SETTINGS)}"
        )
    return ModelProfile(
        alias=alias,
        provider=str(configuration.get("provider") or "unknown"),
        model=str(configuration.get("model") or alias),
        reasoning=reasoning,
    )


def profile_for_alias(alias: str) -> ModelProfile:
    """Resolve one alias through the loaded daemon configuration."""
    try:
        from config import MODEL_PROFILES
    except Exception:  # noqa: BLE001 - configuration absent in some tests
        return ModelProfile(alias=alias, provider="unknown", model=alias)
    return MODEL_PROFILES.get(alias) or ModelProfile(
        alias=alias, provider="unknown", model=alias,
    )


def completion_parameters(
    profile: ModelProfile,
    *,
    output_tokens: int,
    temperature: float | None = None,
    reasoning: str = "low",
    json_object: bool = False,
) -> dict[str, Any]:
    """Build the sampling and budget parameters for one structured call.

    ``output_tokens`` is the budget the visible reply needs. ``reasoning``
    is the effort a control-plane call asks for when the model reasons
    by default; the operator's ``reasoning`` setting on the model
    overrides it, and ``off`` never sends an effort. ``temperature`` is
    omitted for a provider that rejects or deprecates it.
    """
    parameters: dict[str, Any] = {}
    effort = _effective_effort(profile, reasoning)
    reasons = profile.reasons_by_default or effort is not None
    budget = int(output_tokens)
    if reasons:
        budget = max(_MIN_REASONING_BUDGET, budget * _REASONING_MULTIPLIER)
    parameters["max_tokens"] = budget
    if temperature is not None and not profile.omits_temperature:
        parameters["temperature"] = float(temperature)
    if effort is not None and profile.accepts_effort:
        parameters["reasoning_effort"] = effort
    if json_object:
        parameters["response_format"] = {"type": "json_object"}
    return parameters


def _effective_effort(profile: ModelProfile, requested: str) -> str | None:
    setting = profile.reasoning
    if setting == "off":
        return None
    if setting in REASONING_LEVELS:
        return setting
    # provider_default: ask for the requested effort only when the model
    # reasons by default, so a plain model never gains a reasoning pass.
    if profile.reasons_by_default and requested in REASONING_LEVELS:
        return requested
    return None


def truncated(response: dict[str, Any]) -> dict[str, Any] | None:
    """Report the usage of one reply the model cut at the token limit."""
    choices = response.get("choices") or []
    if not choices:
        return None
    if str(choices[0].get("finish_reason") or "") != "length":
        return None
    usage = response.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "text_tokens": details.get("text_tokens"),
    }


def retry_budget(parameters: dict[str, Any]) -> dict[str, Any]:
    """The same parameters with a completion budget four times larger."""
    return {
        **parameters,
        "max_tokens": int(parameters.get("max_tokens") or _MIN_REASONING_BUDGET)
        * _REASONING_MULTIPLIER,
    }


def message_content(response: dict[str, Any]) -> str:
    """The visible text of one reply, or an empty string when absent."""
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content if isinstance(part, dict)
        )
    return str(content or "")
