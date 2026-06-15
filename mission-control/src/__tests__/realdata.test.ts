import { describe, it, expect } from "vitest";
import { normalizeBody, bodyPreview } from "@/components/features/board/boardModel";

describe("normalizeBody — literal escape handling (daemon data)", () => {
  // Actual pattern from daemon board entry e-6
  const fencedWithLiteralEscapes = '```json\n[\n  {\n    "id": "e-6",\n    "type": "solution",\n    "author": "decider",\n    "body": "To excel in an API design interview.\\n\\n### 1. Master the Domain Model\\nEvaluation systems are observability platforms.",\n    "refs": ["e-3"],\n    "confidence": 0.95\n  }\n]\n```';

  it("extracts body from fenced JSON with literal \\n in body field", () => {
    const result = normalizeBody(fencedWithLiteralEscapes);
    expect(result).not.toContain("```json");
    expect(result).not.toContain("```");
    expect(result).toContain("To excel in an API design interview.");
    expect(result).toContain("### 1. Master the Domain Model");
  });

  it("unescapes literal \\n in extracted body to real newlines", () => {
    const result = normalizeBody(fencedWithLiteralEscapes);
    // Should have real newlines, not literal \\n
    expect(result).toContain("\n");
    expect(result).not.toContain("\\n");
  });

  it("bodyPreview strips ### headings from extracted body", () => {
    const result = bodyPreview(fencedWithLiteralEscapes);
    expect(result).not.toContain("```");
    expect(result).not.toContain("###");
    expect(result).toContain("To excel");
  });

  // Double-escaped fence markers
  const fullyEscaped = '```json\\n[\\n  {\\n    \\"id\\": \\"e-6\\",\\n    \\"type\\": \\"solution\\",\\n    \\"body\\": \\"Hello world.\\",\\n    \\"confidence\\": 0.95\\n  }\\n]\\n```';

  it("handles fully literal-escaped fenced JSON", () => {
    const result = normalizeBody(fullyEscaped);
    expect(result).not.toContain("```");
    expect(result).toContain("Hello world.");
  });

  // Bare JSON with literal escapes in body field
  const bareJsonLiteralEscapes = '{"body": "Line one.\\nLine two.\\n\\n### Section\\nContent here."}';

  it("unescapes literal \\n in bare JSON body field", () => {
    const result = normalizeBody(bareJsonLiteralEscapes);
    expect(result).toContain("Line one.\nLine two.");
    expect(result).toContain("### Section\nContent here.");
  });

  // Bare JSON with literal \\t
  const jsonWithTabs = '{"summary": "Col1\\tCol2\\tCol3"}';

  it("unescapes literal \\t in body fields", () => {
    const result = normalizeBody(jsonWithTabs);
    expect(result).toContain("Col1\tCol2\tCol3");
  });

  // Critic entry with fenced JSON array
  const criticEntry = '```json\n[\n  {\n    "type": "critique",\n    "body": "The plan lacks specificity.\\nConsider adding concrete steps.",\n    "refs": ["e-2"]\n  }\n]\n```';

  it("handles critique entry with literal escapes", () => {
    const result = normalizeBody(criticEntry);
    expect(result).not.toContain("```");
    expect(result).toContain("The plan lacks specificity.");
    expect(result).toContain("Consider adding concrete steps.");
  });
});
