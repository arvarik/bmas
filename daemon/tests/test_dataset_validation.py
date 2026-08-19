"""Tests for canonical CSV and JSONL dataset validation."""

from benchmarks.datasets import validate_dataset


def test_csv_maps_rows_and_keeps_unmapped_metadata():
    result = validate_dataset(
        b"case,prompt,answer,topic,notes\n1,What is 1+1?,2,math,small\n",
        filename="sample.csv",
        mapping={
            "id": "case",
            "input": "prompt",
            "expected_output": "answer",
            "subject": "topic",
        },
    )

    assert result.valid is True
    assert result.row_count == 1
    assert result.checksum is not None
    assert result.preview == [{
        "item_key": "1",
        "input": "What is 1+1?",
        "expected_output": "2",
        "subject": "math",
        "split": None,
        "tags": [],
        "metadata": {"notes": "small"},
    }]


def test_jsonl_detects_duplicate_ids_and_empty_expected_output():
    result = validate_dataset(
        b'{"id":"same","question":"One","answer":"1"}\n'
        b'{"id":"same","question":"Two","answer":""}\n',
        filename="sample.jsonl",
        mapping={"id": "id", "input": "question", "expected_output": "answer"},
    )

    assert result.valid is False
    assert result.checksum is None
    assert {(issue.row, issue.field) for issue in result.issues} == {
        (2, "id"),
        (2, "expected_output"),
    }


def test_validation_rejects_unknown_format_and_non_utf8_content():
    unknown = validate_dataset(
        b"question,answer\nOne,1\n",
        filename="sample.txt",
        mapping={"input": "question", "expected_output": "answer"},
    )
    invalid_text = validate_dataset(
        b"\xff\xfe",
        filename="sample.csv",
        mapping={"input": "question", "expected_output": "answer"},
    )

    assert unknown.valid is False
    assert "csv or .jsonl" in unknown.issues[0].message
    assert invalid_text.valid is False
    assert "UTF-8" in invalid_text.issues[0].message


def test_checksum_ignores_generated_item_identifiers():
    content = b"question,answer\nOne,1\n"
    mapping = {"input": "question", "expected_output": "answer"}

    first = validate_dataset(content, filename="sample.csv", mapping=mapping)
    second = validate_dataset(content, filename="sample.csv", mapping=mapping)

    assert first.items[0]["id"] != second.items[0]["id"]
    assert first.checksum == second.checksum
