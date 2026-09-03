"""The published performance contract as one versioned harness.

Every operation runs five warmup trials and thirty measured trials on
the pinned worker and reports the median, p95, maximum, peak memory,
and fixture digest against its published limit. The smoke scale runs
the same harness with small fixtures and few repetitions on every
worker, so continuous integration proves the machinery, and the
full scale runs only on the pinned performance worker where the
limits are enforced.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import time
from pathlib import Path

import pytest
import pytest_asyncio

import database as db
from benchmarks import frozen_analysis

FULL_SCALE = os.getenv("BMAS_PERFORMANCE_WORKER") == "1"
WARMUP = 5 if FULL_SCALE else 1
MEASURED = 30 if FULL_SCALE else 3
CONTRACT_VERSION = 1

LIMITS = {
    "dataset_import": {"seconds": 60.0, "peak_memory_bytes": 2 * 1024**3,
                       "fixture_cases": 100_000 if FULL_SCALE else 500},
    "dataset_page": {"p95_seconds": 0.25,
                     "fixture_cases": 100_000 if FULL_SCALE else 500},
    "evidence_append": {"p95_seconds": 0.05,
                        "fixture_events": 10_000 if FULL_SCALE else 200},
    "analysis_report": {"seconds": 30.0, "peak_memory_bytes": 4 * 1024**3,
                        "fixture_cases": 100_000 if FULL_SCALE else 200,
                        "replicates": 10_000 if FULL_SCALE else 20},
}


def _peak_memory_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage) if os.uname().sysname == "Darwin" else int(usage) * 1024


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(int(round(fraction * len(ordered) + 0.5)) - 1, 0)
    return ordered[min(index, len(ordered) - 1)]


class Trial:
    """Warmup, repeat, and summarize one operation."""

    def __init__(self, name: str, fixture_digest: str) -> None:
        self.name = name
        self.fixture_digest = fixture_digest
        self.durations: list[float] = []

    async def run(self, operation) -> dict:
        for _ in range(WARMUP):
            await operation()
        for _ in range(MEASURED):
            started = time.perf_counter()
            await operation()
            self.durations.append(time.perf_counter() - started)
        return {
            "operation": self.name,
            "contract_version": CONTRACT_VERSION,
            "scale": "full" if FULL_SCALE else "smoke",
            "warmup": WARMUP,
            "measured": MEASURED,
            "median_seconds": _percentile(self.durations, 0.5),
            "p95_seconds": _percentile(self.durations, 0.95),
            "max_seconds": max(self.durations),
            "peak_memory_bytes": _peak_memory_bytes(),
            "fixture_digest": self.fixture_digest,
        }


REPORTS: list[dict] = []


def _record(report: dict) -> None:
    REPORTS.append(report)
    path = Path(os.getenv("BMAS_PERFORMANCE_REPORT",
                          "../test-results/performance-contract.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_id": "bmas.performance_contract",
        "contract_version": CONTRACT_VERSION,
        "scale": "full" if FULL_SCALE else "smoke",
        "host": {"sysname": os.uname().sysname, "machine": os.uname().machine,
                 "cpu_count": os.cpu_count()},
        "reports": REPORTS,
    }, indent=2, sort_keys=True))


def _enforce(report: dict, *, seconds: float | None = None,
             p95_seconds: float | None = None,
             peak_memory_bytes: int | None = None) -> None:
    if not FULL_SCALE:
        return
    if seconds is not None:
        assert report["max_seconds"] <= seconds, report
    if p95_seconds is not None:
        assert report["p95_seconds"] <= p95_seconds, report
    if peak_memory_bytes is not None:
        assert report["peak_memory_bytes"] <= peak_memory_bytes, report


@pytest_asyncio.fixture
async def performance_db(tmp_path, monkeypatch):
    path = str(tmp_path / "performance.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return path


def _items(count: int) -> list[dict]:
    return [{
        "id": f"item-{index}", "item_key": f"case-{index}",
        "input": f"What is {index} plus 1?", "expected_output": str(index + 1),
        "subject": f"family-{index % 4}", "split": "test", "tags": [],
        "metadata": {},
    } for index in range(count)]


@pytest.mark.asyncio
async def test_dataset_import_and_page(performance_db):
    count = LIMITS["dataset_import"]["fixture_cases"]
    items = _items(count)
    digest = hashlib.sha256(json.dumps(items).encode()).hexdigest()
    counter = {"version": 0}

    async def import_operation():
        counter["version"] += 1
        await db.create_dataset_version(
            dataset_id="dataset-performance",
            version_id=f"version-{counter['version']}",
            name="performance", description="", source_uri=None,
            license_name=None, author=None, dataset_metadata={},
            checksum=f"{digest[:56]}{counter['version']:08d}",
            schema={"version": "1"},
            source_filename="performance.jsonl",
            source_mime="application/x-ndjson", source_checksum=digest,
            source_path="/tmp/performance.jsonl", version_metadata={},
            items=[{**item, "id": f"{item['id']}-{counter['version']}"}
                   for item in items],
        )

    imported = await Trial("dataset_import", digest).run(import_operation)
    _record(imported)
    _enforce(imported, seconds=LIMITS["dataset_import"]["seconds"],
             peak_memory_bytes=LIMITS["dataset_import"]["peak_memory_bytes"])

    async def page_operation():
        rows, total = await db.list_dataset_items(
            "version-1", limit=50, offset=count // 2,
        )
        assert total == count and len(rows) == 50

    paged = await Trial("dataset_page", digest).run(page_operation)
    _record(paged)
    _enforce(paged, p95_seconds=LIMITS["dataset_page"]["p95_seconds"])


@pytest.mark.asyncio
async def test_evidence_append(performance_db, tmp_path):
    from activation_service import persist_protected_artifact
    from benchmarks.provenance import canonical_json
    from core.asset_store import ArtifactStore, DataClass

    events = [{"kind": "action", "index": index}
              for index in range(LIMITS["evidence_append"]["fixture_events"])]
    payload = canonical_json(events).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    store = ArtifactStore(tmp_path / "evidence", "tenant-performance")
    counter = {"append": 0}

    async def append_operation():
        counter["append"] += 1
        persist_protected_artifact(
            store, payload + str(counter["append"]).encode(),
            media_type="application/json",
            access_policy="attempt-evidence-bytes",
            data_class=DataClass.INTERNAL,
            referenced_by=f"attempt-{counter['append']}",
        )

    appended = await Trial("evidence_append", digest).run(append_operation)
    _record(appended)
    _enforce(appended, p95_seconds=LIMITS["evidence_append"]["p95_seconds"])


@pytest.mark.asyncio
async def test_analysis_report():
    from test_frozen_analysis import comparison, run_from_slots

    count = LIMITS["analysis_report"]["fixture_cases"]
    cases = {f"c{index}": [(index % 2, (index + 1) % 2)] for index in range(count)}
    run, families = run_from_slots({"f": cases})
    spec = frozen_analysis.freeze_specification(
        families=families, scorer_id="exact", master_seed=7,
        comparison_family={"family_id": "performance",
                           "comparisons": [comparison()]},
        resample_count=LIMITS["analysis_report"]["replicates"],
    )
    frozen = frozen_analysis.freeze_input(run, spec, planned_repetitions=1)
    digest = frozen["input_digest"]

    async def analysis_operation():
        report = frozen_analysis.compute_report(spec, frozen)
        assert report["results_digest"]

    analyzed = await Trial("analysis_report", digest).run(analysis_operation)
    _record(analyzed)
    _enforce(analyzed, seconds=LIMITS["analysis_report"]["seconds"],
             peak_memory_bytes=LIMITS["analysis_report"]["peak_memory_bytes"])


def test_report_records_every_required_field():
    assert REPORTS, "the operations record their reports first"
    for report in REPORTS:
        for field in ("median_seconds", "p95_seconds", "max_seconds",
                      "peak_memory_bytes", "fixture_digest", "warmup",
                      "measured", "contract_version"):
            assert field in report, field
        assert report["measured"] == MEASURED
        assert report["warmup"] == WARMUP
