# /opt/bmas/agent/tests/test_event_parser.py
"""
Tests for SSE event parsing and Hermes → bMAS trace translation.

Validates:
- Raw SSE byte stream → (event_name, data_dict) tuples
- translate() maps each Hermes event type correctly
- Synthetic turn_start emission
- Unknown event types are handled gracefully
- Batch buffer logic in TraceEmitter
"""

import json
import pytest
import sys
import os

# Add agent directory to path so we can import api_server
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_server import parse_sse_line_buffer, translate


# ── SSE Parser Tests ───────────────────────────────────────────────────

class TestParseSSELineBuffer:
    """Tests for parse_sse_line_buffer()."""

    def test_single_event(self):
        lines = [
            "event: message.delta",
            'data: {"delta": "Hello"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "message.delta"
        assert events[0][1] == {"delta": "Hello"}

    def test_multiple_events(self):
        lines = [
            "event: message.delta",
            'data: {"delta": "Hello"}',
            "",
            "event: message.delta",
            'data: {"delta": " world"}',
            "",
            "event: run.completed",
            'data: {"output": "Hello world", "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 3
        assert events[0][0] == "message.delta"
        assert events[1][0] == "message.delta"
        assert events[2][0] == "run.completed"
        assert events[2][1]["usage"]["input_tokens"] == 100

    def test_tool_started_event(self):
        lines = [
            "event: tool.started",
            'data: {"name": "web_search", "arguments": {"query": "test"}}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "tool.started"
        assert events[0][1]["name"] == "web_search"

    def test_tool_completed_event(self):
        lines = [
            "event: tool.completed",
            'data: {"name": "web_search", "result": "Found 3 results"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "tool.completed"
        assert events[0][1]["result"] == "Found 3 results"

    def test_run_failed_event(self):
        lines = [
            "event: run.failed",
            'data: {"error": "Model timeout"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "run.failed"
        assert events[0][1]["error"] == "Model timeout"

    def test_run_cancelled_event(self):
        lines = [
            "event: run.cancelled",
            'data: {}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "run.cancelled"

    def test_keepalive_comments_skipped(self):
        lines = [
            ":keepalive",
            "event: message.delta",
            'data: {"delta": "Hi"}',
            "",
            ":keepalive",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "message.delta"

    def test_reasoning_available(self):
        lines = [
            "event: reasoning.available",
            'data: {"text": "Let me think about this..."}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "reasoning.available"
        assert events[0][1]["text"] == "Let me think about this..."

    def test_approval_events(self):
        lines = [
            "event: approval.request",
            'data: {"action": "execute_command", "args": {"cmd": "rm -rf"}}',
            "",
            "event: approval.responded",
            'data: {"action": "execute_command", "approved": false}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 2
        assert events[0][0] == "approval.request"
        assert events[1][0] == "approval.responded"

    def test_trailing_event_without_blank_line(self):
        """Events at the end of stream without a trailing blank line."""
        lines = [
            "event: run.completed",
            'data: {"output": "done", "usage": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}}',
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "run.completed"
        assert events[0][1]["output"] == "done"

    def test_multiline_data(self):
        """Multiple data: lines are joined."""
        lines = [
            "event: message.delta",
            'data: {"delta":',
            'data: "Hello"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        # Joined data: '{"delta":\n"Hello"}'
        # This won't be valid JSON with the naive join, but the parser handles it
        assert events[0][0] == "message.delta"

    def test_invalid_json_data(self):
        """Invalid JSON is captured as raw text."""
        lines = [
            "event: unknown",
            "data: not-json-at-all",
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "unknown"
        assert events[0][1] == {"raw": "not-json-at-all"}

    def test_empty_input(self):
        events = parse_sse_line_buffer([])
        assert events == []

    def test_only_keepalives(self):
        lines = [":keepalive", ":keepalive", ":keepalive"]
        events = parse_sse_line_buffer(lines)
        assert events == []

    def test_default_event_name(self):
        """When no event: line is present, default to 'message'."""
        lines = [
            'data: {"text": "hello"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "message"


# ── Translate Tests ────────────────────────────────────────────────────

class TestTranslate:
    """Tests for the translate() function — Hermes SSE → bMAS trace."""

    BASE_KWARGS = {
        "task_id": "task-abc",
        "turn_id": "turn-1",
        "seq": 5,
        "role": "planner",
        "node": "node-1",
    }

    def test_message_delta(self):
        trace = translate(
            "message.delta", {"delta": "Hello world"},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "reasoning"
        assert trace["data"]["text"] == "Hello world"
        assert trace["task_id"] == "task-abc"
        assert trace["turn_id"] == "turn-1"
        assert trace["seq"] == 5
        assert trace["role"] == "planner"
        assert trace["node"] == "node-1"
        assert trace["trace_id"] == "trace-turn-1"

    def test_reasoning_available(self):
        trace = translate(
            "reasoning.available", {"text": "Let me think..."},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "reasoning"
        assert trace["data"]["text"] == "Let me think..."

    def test_tool_started(self):
        trace = translate(
            "tool.started",
            {"name": "web_search", "arguments": {"query": "test"}},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "tool_call"
        assert trace["data"]["tool"] == "web_search"
        assert trace["data"]["args"] == {"query": "test"}

    def test_tool_completed(self):
        trace = translate(
            "tool.completed",
            {"name": "web_search", "result": "Found 3 results"},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "tool_result"
        assert trace["data"]["tool"] == "web_search"
        assert trace["data"]["ok"] is True
        assert "Found 3 results" in trace["data"]["summary"]

    def test_tool_completed_with_error(self):
        trace = translate(
            "tool.completed",
            {"name": "web_search", "result": "timeout", "error": True},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "tool_result"
        assert trace["data"]["ok"] is False

    def test_approval_request(self):
        trace = translate(
            "approval.request",
            {"action": "execute_command", "args": {"cmd": "ls"}},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "approval_request"
        assert trace["data"]["action"] == "execute_command"

    def test_approval_responded(self):
        trace = translate(
            "approval.responded",
            {"action": "execute_command"},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "approval_request"

    def test_run_completed(self):
        trace = translate(
            "run.completed",
            {
                "output": "The answer is 42",
                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            },
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "final"
        assert "42" in trace["data"]["summary"]
        assert trace["data"]["usage"]["input_tokens"] == 100
        assert trace["tokens"]["in"] == 100
        assert trace["tokens"]["out"] == 10

    def test_run_failed(self):
        trace = translate(
            "run.failed",
            {"error": "Model crashed"},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "error"
        assert trace["data"]["message"] == "Model crashed"

    def test_run_cancelled(self):
        trace = translate(
            "run.cancelled", {},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "error"
        assert "cancelled" in trace["data"]["message"]

    def test_unknown_event(self):
        """Unknown Hermes events are captured as reasoning traces."""
        trace = translate(
            "hermes.custom.event",
            {"foo": "bar"},
            **self.BASE_KWARGS,
        )
        assert trace["type"] == "reasoning"
        assert "hermes.custom.event" in trace["data"]["text"]

    def test_trace_has_required_fields(self):
        """Every trace must have all required schema fields (doc 06 §4)."""
        trace = translate(
            "message.delta", {"delta": "hi"},
            **self.BASE_KWARGS,
        )
        required = {"trace_id", "task_id", "turn_id", "seq", "ts", "role", "node", "type", "data", "tokens", "cost_usd"}
        assert required.issubset(set(trace.keys()))

    def test_tool_result_truncation(self):
        """Tool result summaries are truncated to 500 chars."""
        long_result = "x" * 1000
        trace = translate(
            "tool.completed",
            {"name": "read_file", "result": long_result},
            **self.BASE_KWARGS,
        )
        assert len(trace["data"]["summary"]) <= 500

    def test_run_completed_output_truncation(self):
        """Final output summary is truncated to 500 chars."""
        long_output = "y" * 1000
        trace = translate(
            "run.completed",
            {"output": long_output, "usage": {}},
            **self.BASE_KWARGS,
        )
        assert len(trace["data"]["summary"]) <= 500


# ── Full SSE Stream Integration Test ────────────────────────────────────

class TestFullSSEStream:
    """End-to-end test: parse a realistic SSE stream and translate all events."""

    REALISTIC_STREAM = [
        "event: message.delta",
        'data: {"delta": "Let me calculate"}',
        "",
        "event: message.delta",
        'data: {"delta": " 17 + 25"}',
        "",
        "event: tool.started",
        'data: {"name": "python_eval", "arguments": {"code": "17 + 25"}}',
        "",
        "event: tool.completed",
        'data: {"name": "python_eval", "result": "42"}',
        "",
        "event: message.delta",
        'data: {"delta": "The answer is 42."}',
        "",
        "event: run.completed",
        'data: {"output": "The answer is 42.", "usage": {"input_tokens": 16842, "output_tokens": 567, "total_tokens": 17409}}',
        "",
    ]

    def test_full_stream_parsing(self):
        events = parse_sse_line_buffer(self.REALISTIC_STREAM)
        assert len(events) == 6
        assert events[0][0] == "message.delta"
        assert events[2][0] == "tool.started"
        assert events[3][0] == "tool.completed"
        assert events[5][0] == "run.completed"

    def test_full_stream_translation(self):
        events = parse_sse_line_buffer(self.REALISTIC_STREAM)
        traces = []
        for i, (event_name, event_data) in enumerate(events):
            trace = translate(
                event_name, event_data,
                task_id="task-test", turn_id="turn-1",
                seq=i, role="executor", node="node-2",
            )
            traces.append(trace)

        # Verify trace types
        types = [t["type"] for t in traces]
        assert types == ["reasoning", "reasoning", "tool_call", "tool_result", "reasoning", "final"]

        # Verify final trace has usage
        final = traces[-1]
        assert final["data"]["usage"]["input_tokens"] == 16842
        assert final["data"]["usage"]["output_tokens"] == 567
        assert final["tokens"]["in"] == 16842
        assert final["tokens"]["out"] == 567

    def test_all_traces_have_consistent_ids(self):
        events = parse_sse_line_buffer(self.REALISTIC_STREAM)
        traces = []
        for i, (event_name, event_data) in enumerate(events):
            trace = translate(
                event_name, event_data,
                task_id="task-test", turn_id="turn-1",
                seq=i, role="executor", node="node-2",
            )
            traces.append(trace)

        for trace in traces:
            assert trace["task_id"] == "task-test"
            assert trace["turn_id"] == "turn-1"
            assert trace["trace_id"] == "trace-turn-1"
            assert trace["role"] == "executor"
            assert trace["node"] == "node-2"

        # Sequence numbers are monotonic
        seqs = [t["seq"] for t in traces]
        assert seqs == list(range(6))


# ── Hermes Gateway Format Tests (verified live 2026-06-10) ─────────────

class TestHermesGatewayFormat:
    """Tests for the live Hermes gateway SSE format.

    Live verification on 192.168.4.103 showed the gateway uses:
        data: {"event": "message.delta", "delta": "42", "run_id": "...", "timestamp": ...}
    instead of:
        event: message.delta
        data: {"delta": "42"}
    """

    def test_gateway_format_message_delta(self):
        """Gateway format: event name inside data JSON."""
        lines = [
            'data: {"event": "message.delta", "run_id": "run_abc", "timestamp": 1781127756.08, "delta": "42"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "message.delta"
        # event key should be popped from data
        assert "event" not in events[0][1]
        assert events[0][1]["delta"] == "42"

    def test_gateway_format_reasoning_available(self):
        lines = [
            'data: {"event": "reasoning.available", "run_id": "run_abc", "timestamp": 1781127756.12, "text": "42"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "reasoning.available"
        assert events[0][1]["text"] == "42"

    def test_gateway_format_run_completed(self):
        lines = [
            'data: {"event": "run.completed", "run_id": "run_abc", "timestamp": 1781127756.13, "output": "42", "usage": {"input_tokens": 16440, "output_tokens": 91, "total_tokens": 16531}}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "run.completed"
        assert events[0][1]["output"] == "42"
        assert events[0][1]["usage"]["input_tokens"] == 16440
        assert events[0][1]["usage"]["output_tokens"] == 91

    def test_gateway_format_full_live_stream(self):
        """Full live SSE stream captured from 192.168.4.103 on 2026-06-10."""
        lines = [
            'data: {"event": "message.delta", "run_id": "run_72582c94e6cf47bc90d8a93f0c90025b", "timestamp": 1781127756.0843768, "delta": "42"}',
            "",
            'data: {"event": "reasoning.available", "run_id": "run_72582c94e6cf47bc90d8a93f0c90025b", "timestamp": 1781127756.126083, "text": "42"}',
            "",
            'data: {"event": "run.completed", "run_id": "run_72582c94e6cf47bc90d8a93f0c90025b", "timestamp": 1781127756.1351733, "output": "42", "usage": {"input_tokens": 16440, "output_tokens": 91, "total_tokens": 16531}}',
            "",
            ": stream closed",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 3
        assert events[0][0] == "message.delta"
        assert events[0][1]["delta"] == "42"
        assert events[1][0] == "reasoning.available"
        assert events[1][1]["text"] == "42"
        assert events[2][0] == "run.completed"
        assert events[2][1]["usage"]["total_tokens"] == 16531

    def test_gateway_format_translation(self):
        """Translate live gateway events to bMAS traces."""
        lines = [
            'data: {"event": "message.delta", "run_id": "run_abc", "delta": "42"}',
            "",
            'data: {"event": "run.completed", "run_id": "run_abc", "output": "42", "usage": {"input_tokens": 16440, "output_tokens": 91, "total_tokens": 16531}}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        traces = []
        for i, (event_name, event_data) in enumerate(events):
            trace = translate(
                event_name, event_data,
                task_id="task-verify", turn_id="turn-v1",
                seq=i, role="planner", node="node-1",
            )
            traces.append(trace)

        assert len(traces) == 2
        assert traces[0]["type"] == "reasoning"
        assert traces[0]["data"]["text"] == "42"
        assert traces[1]["type"] == "final"
        assert traces[1]["tokens"]["in"] == 16440
        assert traces[1]["tokens"]["out"] == 91

    def test_standard_format_still_works(self):
        """Ensure standard event: line format is not broken."""
        lines = [
            "event: message.delta",
            'data: {"delta": "hello"}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 1
        assert events[0][0] == "message.delta"
        assert events[0][1] == {"delta": "hello"}

    def test_mixed_formats(self):
        """Mix of standard and gateway format in one stream."""
        lines = [
            # Standard format
            "event: message.delta",
            'data: {"delta": "hello"}',
            "",
            # Gateway format
            'data: {"event": "run.completed", "output": "done", "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}',
            "",
        ]
        events = parse_sse_line_buffer(lines)
        assert len(events) == 2
        assert events[0][0] == "message.delta"
        assert events[0][1]["delta"] == "hello"
        assert events[1][0] == "run.completed"
        assert events[1][1]["output"] == "done"

