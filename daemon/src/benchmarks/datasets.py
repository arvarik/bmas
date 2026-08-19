"""Validate CSV and JSONL benchmark datasets into one canonical item format."""

from __future__ import annotations

import csv
import io
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from benchmarks.provenance import content_checksum

MAX_DATASET_ROWS = 100_000
PREVIEW_ROWS = 10


@dataclass(frozen=True)
class DatasetIssue:
    row: int
    field: str
    message: str


@dataclass(frozen=True)
class DatasetValidation:
    valid: bool
    format: str
    columns: list[str]
    row_count: int
    checksum: str | None
    preview: list[dict[str, Any]]
    issues: list[DatasetIssue]
    items: list[dict[str, Any]]

    def public_dict(self) -> dict[str, Any]:
        """Return validation data without the complete canonical item list."""
        return {
            "valid": self.valid,
            "format": self.format,
            "columns": self.columns,
            "row_count": self.row_count,
            "checksum": self.checksum,
            "preview": self.preview,
            "issues": [asdict(issue) for issue in self.issues],
        }


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _tags(value: Any) -> list[str]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = []
    return list(dict.fromkeys(text for item in candidates if (text := _string(item))))


def _records_from_jsonl(text: str) -> tuple[list[dict[str, Any]], list[DatasetIssue]]:
    records: list[dict[str, Any]] = []
    issues: list[DatasetIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(records) >= MAX_DATASET_ROWS:
            issues.append(
                DatasetIssue(line_number, "file", f"The dataset exceeds {MAX_DATASET_ROWS:,} rows")
            )
            break
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            issues.append(DatasetIssue(line_number, "file", f"Invalid JSON: {error.msg}"))
            continue
        if not isinstance(value, dict):
            issues.append(
                DatasetIssue(line_number, "file", "Each JSONL row must contain an object")
            )
            continue
        records.append(value)
    return records, issues


def _records_from_csv(text: str) -> tuple[list[dict[str, Any]], list[DatasetIssue]]:
    issues: list[DatasetIssue] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], [DatasetIssue(1, "file", "The CSV file has no header row")]
    records: list[dict[str, Any]] = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if len(records) >= MAX_DATASET_ROWS:
                issues.append(
                    DatasetIssue(
                        row_number, "file", f"The dataset exceeds {MAX_DATASET_ROWS:,} rows"
                    )
                )
                break
            records.append(dict(row))
    except csv.Error as error:
        issues.append(DatasetIssue(reader.line_num, "file", f"Invalid CSV: {error}"))
    return records, issues


def validate_dataset(
    content: bytes,
    *,
    filename: str,
    mapping: dict[str, str],
) -> DatasetValidation:
    """Parse one upload and map it to canonical benchmark items."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return DatasetValidation(
            False,
            "unknown",
            [],
            0,
            None,
            [],
            [DatasetIssue(1, "file", "The file must use UTF-8 text encoding")],
            [],
        )

    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension == "jsonl":
        records, issues = _records_from_jsonl(text)
        source_format = "jsonl"
    elif extension == "csv":
        records, issues = _records_from_csv(text)
        source_format = "csv"
    else:
        return DatasetValidation(
            False,
            "unknown",
            [],
            0,
            None,
            [],
            [DatasetIssue(1, "file", "Choose a .csv or .jsonl file")],
            [],
        )

    columns = sorted({str(key) for record in records for key in record})
    required = {
        "input": mapping.get("input", ""),
        "expected_output": mapping.get("expected_output", ""),
    }
    for field, column in required.items():
        if not column:
            issues.append(
                DatasetIssue(1, field, f"Select a source column for {field.replace('_', ' ')}")
            )
        elif column not in columns:
            issues.append(DatasetIssue(1, field, f"The source column '{column}' does not exist"))

    canonical: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    mapped_columns = {column for column in mapping.values() if column}
    for index, record in enumerate(records):
        row_number = index + (2 if source_format == "csv" else 1)
        item_key = _string(record.get(mapping.get("id", ""))) or f"item-{index + 1}"
        item_input = _string(record.get(mapping.get("input", "")))
        expected = _string(record.get(mapping.get("expected_output", "")))
        if item_key in seen_keys:
            issues.append(DatasetIssue(row_number, "id", f"Duplicate item identifier '{item_key}'"))
        seen_keys.add(item_key)
        if not item_input:
            issues.append(DatasetIssue(row_number, "input", "The input is empty"))
        if not expected:
            issues.append(
                DatasetIssue(row_number, "expected_output", "The expected output is empty")
            )
        metadata = {
            str(key): value
            for key, value in record.items()
            if key not in mapped_columns and value not in (None, "")
        }
        canonical.append(
            {
                "id": f"dsi-{uuid.uuid4().hex}",
                "item_key": item_key,
                "input": item_input,
                "expected_output": expected,
                "subject": _string(record.get(mapping.get("subject", ""))) or None,
                "split": _string(record.get(mapping.get("split", ""))) or None,
                "tags": _tags(record.get(mapping.get("tags", ""))),
                "metadata": metadata,
            }
        )

    valid = bool(canonical) and not issues
    checksum_items = [
        {key: value for key, value in item.items() if key != "id"} for item in canonical
    ]
    return DatasetValidation(
        valid=valid,
        format=source_format,
        columns=columns,
        row_count=len(records),
        checksum=content_checksum(checksum_items) if valid else None,
        preview=checksum_items[:PREVIEW_ROWS],
        issues=issues[:200],
        items=canonical if valid else [],
    )
