#!/usr/bin/env python3
"""The deterministic fake nested provider for the complete test stack.

The server speaks the OpenAI-compatible chat-completions protocol the
agent service expects from its model gateway. Every response derives
from the prompt digest, so equal prompts produce equal completions on
every run and no external network exists. The server also exposes a
readiness endpoint, records every request as one structured log line,
and never reads a real credential: the only accepted key is the
test-only key passed on the command line.

Run:

    python3 scripts/fake-provider.py --port 43110 --api-key test-key
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


_FENCE = re.compile(r"```.*?```", re.DOTALL)
_ARITHMETIC_WORDS = ("plus", "add", "sum", "+")


def task_lines(prompt: str) -> list[str]:
    """The prompt lines outside code fences.

    The agent appends the board context as one fenced JSON block, and
    that block carries identifiers, timestamps, and scores. The answer
    must follow the task text, never a number inside the context.
    """
    return [line.strip() for line in _FENCE.sub(" ", prompt).splitlines() if line.strip()]


def deterministic_answer(prompt: str) -> str:
    """Answer arithmetic prompts exactly and everything else stably."""
    for line in task_lines(prompt):
        lowered = line.lower()
        positions = [lowered.find(word) for word in _ARITHMETIC_WORDS if word in lowered]
        if not positions:
            continue
        # The operands sit next to the arithmetic word: the last number
        # before it and the first number after it, else the first two.
        cut = min(positions)
        before = [float(value) for value in _NUMBER.findall(line[:cut])]
        after = [float(value) for value in _NUMBER.findall(line[cut:])]
        if before and after:
            operands = [before[-1], after[0]]
        else:
            operands = (before + after)[:2]
        if len(operands) >= 2:
            total = operands[0] + operands[1]
            rendered = str(int(total)) if total == int(total) else str(total)
            return f"The sum is {rendered}. #### {rendered}"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return f"Deterministic reply {digest}."


# The fake gateway also speaks the Hermes runs contract the agent
# service probes: capabilities, detailed health, run submission, run
# status, one server-sent event stream, and run stop. Every run
# completes deterministically from its prompt digest.
_RUNS: dict[str, dict] = {}
_CAPABILITIES = {
    "features": {
        "run_submission": True,
        "run_status": True,
        "run_events_sse": True,
        "run_stop": True,
    },
    "endpoints": {
        "runs": {"method": "POST", "path": "/v1/runs"},
        "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
        "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
        "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
    },
    "version": "fake-gateway-1",
}


def _run_prompt(payload: dict) -> str:
    parts = []
    for key in ("input", "prompt", "objective", "task", "description"):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
    # The user messages carry the task. A system persona may list
    # numbered instructions, which are not the task's numbers.
    for message in payload.get("messages") or []:
        if isinstance(message, dict) and message.get("role") != "system":
            parts.append(str(message.get("content") or ""))
    return "\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    api_key = ""
    log_stream = sys.stdout

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _emit(self, **fields: object) -> None:
        record = {"ts": time.time(), "component": "fake-provider", **fields}
        self.log_stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.log_stream.flush()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/health/detailed"):
            self._json(200, {"status": "ready", "version": "fake-gateway-1",
                             "readiness": {"ready": True}})
            return
        if self.path.startswith(("/health", "/v1/health", "/readiness")):
            self._json(200, {"status": "ready", "provider": "fake-nested"})
            return
        if self.path in ("/v1/models", "/models"):
            self._json(200, {"data": [{"id": "fake-model", "object": "model"}]})
            return
        if self.path == "/v1/capabilities":
            self._json(200, _CAPABILITIES)
            return
        if self.path in ("/v1/skills", "/v1/toolsets"):
            self._json(200, {"items": []})
            return
        if self.path.startswith("/v1/runs/"):
            parts = self.path.split("/")
            run_id = parts[3] if len(parts) > 3 else ""
            run = _RUNS.get(run_id)
            if run is None:
                self._json(404, {"error": "unknown run"})
                return
            if self.path.endswith("/events"):
                self._events(run)
                return
            self._json(200, run)
            return
        self._json(404, {"error": "not found"})

    def _events(self, run: dict) -> None:
        body = (
            "data: " + json.dumps({"event": "run.started",
                                   "run_id": run["id"]}) + "\n\n"
            + "data: " + json.dumps({
                "event": "run.completed", "run_id": run["id"],
                "output": run["output"], "usage": run["usage"],
            }) + "\n\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._emit(event="run_events", run_id=run["id"])

    def do_POST(self) -> None:
        authorization = self.headers.get("Authorization", "")
        if self.api_key and authorization != f"Bearer {self.api_key}":
            self._emit(event="rejected", reason="bad_key", path=self.path)
            self._json(401, {"error": "invalid api key"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return
        if self.path == "/v1/runs":
            prompt = _run_prompt(request)
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            run_id = f"run-{digest[:16]}"
            output = deterministic_answer(prompt)
            usage = {"prompt_tokens": max(1, len(prompt.split())),
                     "completion_tokens": max(1, len(output.split()))}
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            _RUNS[run_id] = {"id": run_id, "run_id": run_id,
                             "status": "completed", "output": output,
                             "usage": usage, "session_id":
                             request.get("session_id")}
            self._emit(event="run_submitted", run_id=run_id,
                       prompt_digest=digest)
            self._json(200, _RUNS[run_id])
            return
        if self.path.startswith("/v1/runs/") and self.path.endswith("/stop"):
            run_id = self.path.split("/")[3]
            run = _RUNS.get(run_id)
            if run is None:
                self._json(404, {"error": "unknown run"})
                return
            self._emit(event="run_stop", run_id=run_id)
            self._json(200, {**run, "status": "cancelled"})
            return
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        messages = request.get("messages") or []
        prompt = "\n".join(str(m.get("content") or "") for m in messages)
        content = deterministic_answer(prompt)
        prompt_tokens = max(1, len(prompt.split()))
        completion_tokens = max(1, len(content.split()))
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self._emit(event="completion", prompt_digest=digest,
                   completion_tokens=completion_tokens)
        self._json(200, {
            "id": f"chatcmpl-{digest[:12]}",
            "object": "chat.completion",
            "created": 0,
            "model": str(request.get("model") or "fake-model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    Handler.api_key = args.api_key
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"ts": time.time(), "component": "fake-provider",
                      "event": "ready", "port": args.port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
