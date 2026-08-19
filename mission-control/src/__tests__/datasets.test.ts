import { describe, expect, it } from "vitest";

import {
  formatDatasetBytes,
  inferDatasetColumns,
  suggestDatasetMapping,
} from "@/lib/datasets";

describe("dataset import presentation", () => {
  it("suggests canonical fields from common source names", () => {
    expect(suggestDatasetMapping([
      "question_id",
      "prompt",
      "ground_truth",
      "topic",
      "partition",
      "labels",
    ])).toEqual({
      id: "question_id",
      input: "prompt",
      expected_output: "ground_truth",
      subject: "topic",
      split: "partition",
      tags: "labels",
    });
  });

  it("reads quoted CSV headers", () => {
    expect(inferDatasetColumns(
      "sample.csv",
      '"case,id",question,"expected,output"\n1,Two,2',
    )).toEqual(["case,id", "question", "expected,output"]);
  });

  it("sorts JSONL columns and rejects malformed first rows", () => {
    expect(inferDatasetColumns(
      "sample.jsonl",
      '\n{"question":"One","answer":"1","id":"one"}\n',
    )).toEqual(["answer", "id", "question"]);
    expect(inferDatasetColumns("sample.jsonl", "not-json\n")).toEqual([]);
  });

  it("formats the upload limit with stable units", () => {
    expect(formatDatasetBytes(512)).toBe("512 B");
    expect(formatDatasetBytes(1_536)).toBe("1.5 KB");
    expect(formatDatasetBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
