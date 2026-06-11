# /opt/bmas/daemon/tests/test_sse_smoke.py
"""SSE smoke test — requires a running daemon on localhost:9000.

Skipped by default unless BMAS_LIVE_TESTS=1 is set, since it needs
a live daemon to connect to.
"""

import os
import asyncio

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("BMAS_LIVE_TESTS") != "1",
        reason="Live daemon tests disabled (set BMAS_LIVE_TESTS=1 to enable)",
    ),
]


async def test_sse():
    httpx = pytest.importorskip("httpx")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Submit task
        resp = await client.post("http://localhost:9000/submit", json={"task": "test SSE"})
        resp.raise_for_status()
        data = resp.json()
        task_id = data["task_id"]

        # Get system events (just sample a bit)
        async with client.stream("GET", "http://localhost:9000/events/system") as response:
            count = 0
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    count += 1
                    if count >= 1:
                        break

        # Get task events
        async with client.stream("GET", f"http://localhost:9000/events/{task_id}") as response:
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                    if event_type in ("complete", "error"):
                        break
