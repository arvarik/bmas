"""A small live Hugging Face smoke check for the scheduled profile.

The normal contract tests use recorded fixtures. This module talks to
the real official endpoints through the safe egress broker, so it
runs only through the explicit optional manifest group on a schedule,
never inside the required profiles.
"""

from __future__ import annotations

import os

import pytest

from benchmarks.source_adapters import HuggingFaceAdapter

pytestmark = pytest.mark.skipif(
    os.getenv("BMAS_LIVE_SOURCE_SMOKE") != "1",
    reason="The live smoke runs only through the optional manifest group",
)


@pytest.mark.asyncio
async def test_live_hugging_face_resolves_a_pinned_commit():
    adapter = HuggingFaceAdapter()
    resolution = await adapter.resolve({"repository": "openai/gsm8k"})
    assert len(resolution.pinned_revision) == 40
    assert resolution.trust_level == "public_untrusted"
    options = await adapter.list_options(resolution)
    assert options["configurations"]
    preview = await adapter.preview(
        resolution,
        configuration=options["configurations"][0],
        split="test",
        limit=2,
    )
    assert preview and preview[0]["input"]
