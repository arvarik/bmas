"""Model-backed judges and simulators wired into the run pipeline.

A rubric judge and a user simulator can both run on a real model
behind the LiteLLM gateway. Both pin the model name, the prompt
digest, the temperature, and the seed, both report the token usage
of every call so the resource ledger records it, and both fail
closed: an unparseable judge reply abstains instead of passing, and
a simulator that cannot produce a turn ends the conversation instead
of inventing one. The judge never sees production secrets and the
simulator sees only the synthetic canaries the executor hands it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from benchmarks.provenance import canonical_json, content_checksum
from core.model_parameters import (
    ModelProfile,
    completion_parameters,
    profile_for_alias,
)

JUDGE_PROMPT_VERSION = "1"
SIMULATOR_PROMPT_VERSION = "1"
DEFAULT_TIMEOUT_SECONDS = 60.0
ABSTAIN = "abstain"

JUDGE_SYSTEM_PROMPT = """You are an evaluation judge. You receive one JSON \
request with a rubric and candidate answers. Score every rubric \
criterion for the candidates. Reply with one JSON object and nothing \
else: {"dimensions": [{"name": string, "value": number between 0 and \
1, "category": string or null}], "passed": true or false or null, \
"explanation": string, "uncertainty": number between 0 and 1 or \
null}. When the request does not contain enough evidence, reply \
{"abstain": true, "explanation": string}."""

LABEL_SYSTEM_PROMPT = """You are an evaluation judge calibrating \
against human labels. You receive one JSON item with an input, the \
reference answer, an optional candidate answer, and the allowed \
labels. Reply with one JSON object and nothing else: {"label": one of \
the allowed labels}. When the item does not contain enough evidence, \
reply {"label": "abstain"}."""

SIMULATOR_SYSTEM_PROMPT = """You simulate one user in a multi-turn \
conversation with an assistant. Follow the persona and the goal. \
Reply with one JSON object and nothing else: {"content": string, \
"stop": true when the goal is reached or the conversation should end}."""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class ModelBackedError(RuntimeError):
    """The model-backed component cannot run with this configuration."""


@dataclass(frozen=True)
class GatewaySettings:
    """The pinned gateway one transport talks to."""

    base_url: str
    api_key: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def gateway_settings_from_environment() -> GatewaySettings | None:
    """Read the gateway settings the daemon already validates at start."""
    key = os.getenv("LITELLM_MASTER_KEY")
    if not key:
        return None
    base_url = os.getenv("BMAS_LITELLM_URL")
    if not base_url:
        host = os.getenv("CP_HOST") or os.getenv("BMAS_CP_HOST") or "127.0.0.1"
        port = os.getenv("BMAS_LITELLM_PORT") or "4000"
        base_url = f"http://{host}:{port}/v1"
    return GatewaySettings(base_url=base_url.rstrip("/"), api_key=key)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


def parse_json_reply(content: str) -> dict[str, Any] | None:
    """Parse one model reply as a JSON object, or None when it is not."""
    text = _strip_fences(content or "")
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


class ModelTransport:
    """One pinned chat-completion transport with usage reporting.

    ``client`` is one callable that receives the request body and
    returns the decoded response body; tests inject a fake, and the
    default posts to the gateway over HTTP.
    """

    def __init__(
        self,
        settings: GatewaySettings,
        *,
        model: str,
        temperature: float = 0.0,
        seed: int = 0,
        max_tokens: int = 1024,
        client: Any = None,
        profile: ModelProfile | None = None,
    ) -> None:
        self.settings = settings
        self.model = str(model)
        self.temperature = float(temperature)
        self.seed = int(seed)
        self.max_tokens = int(max_tokens)
        self._client = client
        self.calls = 0
        self.profile = profile or profile_for_alias(self.model)
        # The provider profile sizes the completion budget for reasoning
        # tokens and drops the parameters the provider rejects.
        self.parameters = completion_parameters(
            self.profile, output_tokens=self.max_tokens,
            temperature=self.temperature, reasoning="low", json_object=True,
        )

    def pins(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
            "gateway": self.settings.base_url,
            "provider": self.profile.provider,
            "provider_model": self.profile.model,
            "effective_parameters": dict(self.parameters),
        }

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            return self._client(body)
        import httpx

        response = httpx.post(
            f"{self.settings.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.api_key}"},
            json=body,
            timeout=self.settings.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Run one completion and return its content, usage, and model."""
        body = {
            "model": self.model,
            "messages": messages,
            "seed": self.seed,
            **self.parameters,
        }
        self.calls += 1
        reply = self._post(body)
        choices = reply.get("choices") or []
        content = ""
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "")
        usage = reply.get("usage") or {}
        return {
            "content": content,
            "usage": {
                key: usage[key]
                for key in ("prompt_tokens", "completion_tokens",
                            "total_tokens")
                if isinstance(usage.get(key), int)
            },
            "model": str(reply.get("model") or self.model),
            "request_digest": content_checksum(body),
        }


# ── The model-backed judge ───────────────────────────────────────────


class ModelBackedJudge:
    """A rubric judge transport and an anchor-set labeler on one model."""

    def __init__(
        self,
        transport: ModelTransport,
        *,
        judge_id: str,
        version: str,
    ) -> None:
        self.transport = transport
        self.judge_id = str(judge_id)
        self.version = str(version)

    @property
    def model(self) -> str:
        return self.transport.model

    @property
    def prompt_digest(self) -> str:
        return hashlib.sha256(
            f"{JUDGE_PROMPT_VERSION}\n{JUDGE_SYSTEM_PROMPT}\n"
            f"{canonical_json(self.transport.pins())}".encode(),
        ).hexdigest()

    def pins(self) -> dict[str, Any]:
        return {
            "judge_id": self.judge_id,
            "version": self.version,
            "model": self.model,
            "prompt_digest": self.prompt_digest,
        }

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        """Judge one rubric request; an unparseable reply abstains."""
        completed = self.transport.complete([
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": canonical_json(request)},
        ])
        parsed = parse_json_reply(completed["content"])
        base = {
            "usage": completed["usage"],
            "model": completed["model"],
            "judge_id": self.judge_id,
            "judge_version": self.version,
        }
        if parsed is None or parsed.get("abstain"):
            return {
                **base,
                "dimensions": [],
                "passed": None,
                "explanation": "abstain: " + str(
                    (parsed or {}).get("explanation")
                    or "the judge reply was not a JSON object"
                ),
                "uncertainty": None,
            }
        dimensions = []
        for dimension in parsed.get("dimensions") or []:
            if not isinstance(dimension, dict):
                continue
            value = dimension.get("value")
            dimensions.append({
                "name": str(dimension.get("name") or "rubric"),
                "value": float(value) if isinstance(value, (int, float)) else None,
                "category": dimension.get("category"),
            })
        passed = parsed.get("passed")
        uncertainty = parsed.get("uncertainty")
        return {
            **base,
            "dimensions": dimensions,
            "passed": passed if isinstance(passed, bool) else None,
            "explanation": str(parsed.get("explanation") or "rubric"),
            "uncertainty": (
                float(uncertainty)
                if isinstance(uncertainty, (int, float)) else None
            ),
        }

    def label(self, item: dict[str, Any], vocabulary: list[str]) -> str:
        """Label one anchor item inside the allowed vocabulary."""
        completed = self.transport.complete([
            {"role": "system", "content": LABEL_SYSTEM_PROMPT},
            {"role": "user", "content": canonical_json({
                "item_id": item.get("item_id"),
                "input": item.get("input"),
                "reference_answer": item.get("expected_output"),
                "candidate": item.get("candidate"),
                "allowed_labels": sorted(vocabulary),
            })},
        ])
        parsed = parse_json_reply(completed["content"]) or {}
        label = str(parsed.get("label") or ABSTAIN)
        return label if label in vocabulary else ABSTAIN


def judge_for(
    configuration: dict[str, Any],
    *,
    settings: GatewaySettings | None = None,
    client: Any = None,
) -> ModelBackedJudge | None:
    """Build the judge one scorer configuration names, if any.

    The configuration names ``judge.judge_id``, ``judge.version``, and
    ``judge.model``; the gateway settings come from the environment
    unless the caller injects them.
    """
    declared = configuration.get("judge")
    if not isinstance(declared, dict) or not declared.get("model"):
        return None
    resolved = settings or gateway_settings_from_environment()
    if resolved is None and client is None:
        raise ModelBackedError(
            "A model-backed judge needs the gateway settings; set "
            "LITELLM_MASTER_KEY and BMAS_LITELLM_URL"
        )
    transport = ModelTransport(
        resolved or GatewaySettings(base_url="fake://gateway", api_key="fake"),
        model=str(declared["model"]),
        temperature=float(declared.get("temperature", 0.0)),
        seed=int(declared.get("seed", configuration.get("seed", 0))),
        client=client,
    )
    return ModelBackedJudge(
        transport,
        judge_id=str(declared.get("judge_id") or "judge-model-backed"),
        version=str(declared.get("version") or "1"),
    )


# ── The model-backed simulator ───────────────────────────────────────


class ModelBackedSimulator:
    """A user simulator that asks the model for every next turn."""

    def __init__(
        self,
        transport: ModelTransport,
        *,
        persona: str,
        max_turns: int,
    ) -> None:
        self._transport = transport
        self._persona = str(persona)
        self._max_turns = int(max_turns)
        self._history: list[dict[str, str]] = []
        self.received_canaries: list[str] = []
        self.usage: list[dict[str, Any]] = []

    def start(self, canaries: list[str]) -> None:
        self.received_canaries = list(canaries)
        self._history = []

    def next_turn(
        self, turn_index: int, agent_message: str | None,
    ) -> dict[str, Any] | None:
        if turn_index >= self._max_turns:
            return None
        if agent_message is not None:
            self._history.append({"role": "assistant",
                                  "content": str(agent_message)})
        completed = self._transport.complete([
            {"role": "system", "content": (
                f"{SIMULATOR_SYSTEM_PROMPT}\nPersona and goal: {self._persona}"
            )},
            *self._history,
            {"role": "user", "content": canonical_json({
                "turn_index": turn_index,
                "instruction": "Produce the next user turn as JSON.",
            })},
        ])
        self.usage.append(completed["usage"])
        parsed = parse_json_reply(completed["content"])
        if parsed is None or not str(parsed.get("content") or "").strip():
            return None
        content = str(parsed["content"])
        self._history.append({"role": "user", "content": content})
        turn: dict[str, Any] = {"content": content}
        if parsed.get("stop") is True:
            turn["stop"] = "goal_reached"
        return turn


def model_backed_simulator_version(
    transport: ModelTransport,
    *,
    persona: str,
    version: str = "1",
    max_turns: int = 12,
) -> Any:
    """Pin one model-backed simulator as a registered simulator version."""
    from benchmarks.interaction_execution import SimulatorVersion

    prompt_digest = hashlib.sha256(
        f"{SIMULATOR_PROMPT_VERSION}\n{SIMULATOR_SYSTEM_PROMPT}\n"
        f"{persona}".encode(),
    ).hexdigest()
    pins = transport.pins()
    return SimulatorVersion(
        implementation_id="simulator-model-backed",
        version=str(version),
        prompt_digest=prompt_digest,
        model=transport.model,
        image_digest=hashlib.sha256(
            f"gateway:{pins['gateway']}".encode(),
        ).hexdigest(),
        dependency_digest=content_checksum(pins),
        random_schedule=f"temperature-{pins['temperature']}-seed-{pins['seed']}",
        factory=lambda: ModelBackedSimulator(
            transport, persona=persona, max_turns=max_turns,
        ),
    )
