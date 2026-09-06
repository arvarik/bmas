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
import platform
import resource
import sqlite3
import subprocess
import sys
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
    "scheduler_decision": {"p95_seconds": 0.1,
                           "fixture_runs": 20 if FULL_SCALE else 4,
                           "fixture_attempts_per_run": 5},
    "cancellation": {"seconds": 2.0},
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


def _command_version(*argv: str) -> str | None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else None


def _module_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _package_version(path: Path) -> str | None:
    try:
        return str(json.loads(path.read_text())["version"])
    except (OSError, ValueError, KeyError):
        return None


def _host_provenance() -> dict:
    """Name the host image, kernel, and every component under measurement."""
    uname = os.uname()
    repo_root = Path(__file__).resolve().parents[2]
    return {
        "image_digest": os.getenv("BMAS_HOST_IMAGE_DIGEST"),
        "sysname": uname.sysname,
        "kernel": uname.release,
        "machine": uname.machine,
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "redis_client": _module_version("redis"),
        "redis_server": os.getenv("BMAS_REDIS_SERVER_VERSION"),
        "numpy": _module_version("numpy"),
        "wasmtime": _module_version("wasmtime"),
        "node": _command_version("node", "--version"),
        "browser_runner": _package_version(
            repo_root / "mission-control" / "node_modules" / "@playwright" / "test" / "package.json",
        ),
        "executable": sys.executable,
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
        "host": _host_provenance(),
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


def _summary(name: str, durations: list[float], fixture_digest: str) -> dict:
    return {
        "operation": name,
        "contract_version": CONTRACT_VERSION,
        "scale": "full" if FULL_SCALE else "smoke",
        "warmup": WARMUP,
        "measured": MEASURED,
        "samples": len(durations),
        "median_seconds": _percentile(durations, 0.5),
        "p95_seconds": _percentile(durations, 0.95),
        "max_seconds": max(durations),
        "peak_memory_bytes": _peak_memory_bytes(),
        "fixture_digest": fixture_digest,
    }


async def _queued_runs(count: int, attempts_per_run: int) -> list[str]:
    """Create ``count`` queued runs that each hold ``attempts_per_run`` attempts."""
    from benchmarks import repository
    from benchmarks.provenance import content_checksum

    await db.create_dataset_version(
        dataset_id="dataset-scheduler", version_id="version-scheduler", name="Scheduler",
        description="", source_uri=None, license_name=None, author=None, dataset_metadata={},
        checksum="scheduler-checksum", schema={"version": "1"}, source_filename="scheduler.jsonl",
        source_mime="application/x-ndjson", source_checksum="scheduler-source",
        source_path="/tmp/scheduler.jsonl", version_metadata={}, items=_items(attempts_per_run),
    )
    envelope = {"runtime_id": "classic", "effective_configuration": {"model_routing": {"medium": "model-a"}}}
    await repository.create_test_revision(
        test_id="test-scheduler", revision_id="revision-scheduler", name="scheduler", description="",
        dataset_version_id="version-scheduler",
        configuration={"repetitions": 1, "seed": 1, "max_concurrency": 32},
        arms=[{"id": "arm-scheduler", "name": "Classic", "slug": "classic", "runtime_id": "classic",
               "configuration": envelope, "configuration_checksum": content_checksum(envelope)}],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    run_ids = [f"run-scheduler-{index:03d}" for index in range(count)]
    for run_id in run_ids:
        await repository.create_run(run_id=run_id, revision_id="revision-scheduler", idempotency_key=None)
    return run_ids


@pytest.mark.asyncio
async def test_scheduler_decision_and_cancellation(performance_db):
    """A lease decision stays under its limit at full scale, and a cancel takes effect in time."""
    from benchmarks import repository
    from benchmarks.capacity import CapacityPolicy

    limits = LIMITS["scheduler_decision"]
    policy = CapacityPolicy(global_limit=limits["fixture_runs"] * limits["fixture_attempts_per_run"] + 1)
    run_ids = await _queued_runs(limits["fixture_runs"], limits["fixture_attempts_per_run"])
    digest = hashlib.sha256(json.dumps(run_ids).encode()).hexdigest()
    total = limits["fixture_runs"] * limits["fixture_attempts_per_run"]
    durations: list[float] = []
    leased: list[dict] = []
    for index in range(total):
        started = time.perf_counter()
        attempt = await repository.claim_next_attempt(worker_id=f"worker-{index % 4}", lease_seconds=60, capacity_policy=policy)
        durations.append(time.perf_counter() - started)
        assert attempt is not None, f"lease {index} found no queued attempt"
        leased.append(attempt)
    assert len({attempt["id"] for attempt in leased}) == total
    assert len({attempt["run_id"] for attempt in leased}) == limits["fixture_runs"]
    report = _summary("scheduler_decision", durations, digest)
    _record(report)
    _enforce(report, p95_seconds=limits["p95_seconds"])

    # Release the attempts of one run, then cancel it and measure when
    # the scheduler stops offering that run's work.
    target = run_ids[0]
    for attempt in leased:
        if attempt["run_id"] == target:
            assert await repository.release_attempt(attempt["id"], lease_token=attempt["lease_token"])
    assert (await repository.claim_next_attempt(worker_id="probe", lease_seconds=60, capacity_policy=policy))["run_id"] == target
    started = time.perf_counter()
    await repository.set_run_state(target, "cancel")
    while True:
        run = await repository.get_run(target)
        probe = await repository.claim_next_attempt(worker_id="probe", lease_seconds=60, capacity_policy=policy)
        elapsed = time.perf_counter() - started
        async with db._connect() as connection:  # noqa: SLF001
            row = await (await connection.execute(
                "SELECT COUNT(*) FROM benchmark_attempts attempt "
                "JOIN benchmark_trials trial ON trial.id = attempt.trial_id "
                "WHERE trial.run_id = ? AND attempt.status = 'queued'", (target,),
            )).fetchone()
        settled = (
            run["status"] in {"cancelling", "cancelled"}
            and int(row[0]) == 0
            and (probe is None or probe["run_id"] != target)
        )
        if settled or elapsed > LIMITS["cancellation"]["seconds"] * 5:
            break
    cancellation = _summary("cancellation", [elapsed], digest)
    cancellation["settled"] = settled
    _record(cancellation)
    assert settled, cancellation
    _enforce(cancellation, seconds=LIMITS["cancellation"]["seconds"])


def test_report_records_every_required_field():
    assert REPORTS, "the operations record their reports first"
    for report in REPORTS:
        for field in ("median_seconds", "p95_seconds", "max_seconds",
                      "peak_memory_bytes", "fixture_digest", "warmup",
                      "measured", "contract_version"):
            assert field in report, field
        assert report["measured"] == MEASURED
        assert report["warmup"] == WARMUP
    document = json.loads(Path(os.getenv("BMAS_PERFORMANCE_REPORT",
                                         "../test-results/performance-contract.json")).read_text())
    for field in ("image_digest", "kernel", "python", "sqlite", "redis_client",
                  "numpy", "wasmtime", "node", "browser_runner"):
        assert field in document["host"], field
    assert {report["operation"] for report in document["reports"]} >= {
        "scheduler_decision", "cancellation"}
