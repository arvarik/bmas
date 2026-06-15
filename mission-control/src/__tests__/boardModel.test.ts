/**
 * boardModel.test.ts — Tests for board model pure functions.
 *
 * Covers:
 * - normalizeBody() — JSON normalization, markdown fencing, fallback key-value
 * - bodyPreview() — truncation, markdown stripping, JSON normalization chain
 * - prettyAuthor() — display name formatting
 * - typeMeta() — entry type metadata lookup
 * - salienceColor() — boundary color mapping
 */

import { describe, it, expect } from "vitest";
import {
  normalizeBody,
  bodyPreview,
  prettyAuthor,
  typeMeta,
  salienceColor,
} from "@/components/features/board/boardModel";

// ── normalizeBody ─────────────────────────────────────────────────────

describe("normalizeBody", () => {
  it("returns empty string for empty input", () => {
    expect(normalizeBody("")).toBe("");
  });

  it("returns plain text as-is", () => {
    expect(normalizeBody("Hello, world!")).toBe("Hello, world!");
  });

  it("returns markdown as-is", () => {
    const md = "## Heading\n\n- item 1\n- item 2";
    expect(normalizeBody(md)).toBe(md);
  });

  it("extracts body from bare JSON object with body field", () => {
    const json = JSON.stringify({ title: "Plan", body: "Step 1: Do X" });
    expect(normalizeBody(json)).toBe("**Plan**\n\nStep 1: Do X");
  });

  it("extracts content from JSON object with content field", () => {
    const json = JSON.stringify({ content: "This is the content" });
    expect(normalizeBody(json)).toBe("This is the content");
  });

  it("extracts text from JSON object with text field", () => {
    const json = JSON.stringify({ text: "Text field value" });
    expect(normalizeBody(json)).toBe("Text field value");
  });

  it("extracts message from JSON object", () => {
    const json = JSON.stringify({ message: "A message" });
    expect(normalizeBody(json)).toBe("A message");
  });

  it("extracts description from JSON object", () => {
    const json = JSON.stringify({ description: "A description" });
    expect(normalizeBody(json)).toBe("A description");
  });

  it("extracts summary from JSON object", () => {
    const json = JSON.stringify({ summary: "A summary" });
    expect(normalizeBody(json)).toBe("A summary");
  });

  it("prepends title when both title and body exist", () => {
    const json = JSON.stringify({
      title: "My Title",
      body: "My Body",
    });
    expect(normalizeBody(json)).toBe("**My Title**\n\nMy Body");
  });

  it("falls back to key-value rendering when no known body fields", () => {
    const json = JSON.stringify({ key1: "val1", key2: "val2" });
    const result = normalizeBody(json);
    expect(result).toContain("**Key1:** val1");
    expect(result).toContain("**Key2:** val2");
  });

  it("extracts from markdown-fenced JSON", () => {
    const fenced = '```json\n{"body": "Fenced content"}\n```';
    expect(normalizeBody(fenced)).toBe("Fenced content");
  });

  it("extracts from markdown-fenced JSON without json label", () => {
    const fenced = '```\n{"body": "Unlabeled fence"}\n```';
    expect(normalizeBody(fenced)).toBe("Unlabeled fence");
  });

  it("handles JSON array — extracts solution type if present", () => {
    const arr = JSON.stringify([
      { type: "finding", body: "Finding text" },
      { type: "solution", body: "Solution text" },
    ]);
    expect(normalizeBody(arr)).toBe("Solution text");
  });

  it("handles JSON array — falls back to last element", () => {
    const arr = JSON.stringify([
      { type: "finding", body: "First" },
      { type: "critique", body: "Last" },
    ]);
    expect(normalizeBody(arr)).toBe("Last");
  });

  it("handles empty JSON object", () => {
    expect(normalizeBody("{}")).toBe("{}");
  });

  it("handles JSON with null/undefined body field values", () => {
    const json = JSON.stringify({ body: null, content: null, name: "test" });
    const result = normalizeBody(json);
    expect(result).toContain("**Name:** test");
  });

  it("passes through invalid JSON as plain text", () => {
    const invalid = "{not valid json}";
    // Starts with { and ends with } but can't parse — falls through as-is
    expect(normalizeBody(invalid)).toBe(invalid);
  });

  it("handles nested JSON values in fallback mode", () => {
    const json = JSON.stringify({
      config: { nested: true },
      count: 42,
    });
    const result = normalizeBody(json);
    expect(result).toContain("**Config:** {\"nested\":true}");
    expect(result).toContain("**Count:** 42");
  });

  it("trims whitespace from input", () => {
    expect(normalizeBody("  hello  ")).toBe("hello");
  });

  it("does not parse JSON-encoded plain strings (no object/array wrapper)", () => {
    // JSON.stringify("hello world") produces '"hello world"' which doesn't
    // start with { or [ so normalizeBody treats it as plain text.
    const jsonStr = JSON.stringify("hello world");
    expect(normalizeBody(jsonStr)).toBe('"hello world"');
  });
});

// ── bodyPreview ───────────────────────────────────────────────────────

describe("bodyPreview", () => {
  it("returns empty string for empty input", () => {
    expect(bodyPreview("", 100)).toBe("");
  });

  it("strips markdown headings", () => {
    expect(bodyPreview("## Hello World")).toBe("Hello World");
  });

  it("strips bold markdown", () => {
    expect(bodyPreview("This is **bold** text")).toBe("This is bold text");
  });

  it("strips inline code", () => {
    expect(bodyPreview("Use `npm install`")).toBe("Use npm install");
  });

  it("truncates long text with ellipsis", () => {
    const long = "A".repeat(300);
    const result = bodyPreview(long, 240);
    expect(result.length).toBeLessThanOrEqual(241); // 240 + ellipsis
    expect(result).toContain("…");
  });

  it("does not truncate short text", () => {
    expect(bodyPreview("Short text", 240)).toBe("Short text");
  });

  it("normalizes JSON before preview", () => {
    const json = JSON.stringify({ body: "The solution is X" });
    expect(bodyPreview(json)).toBe("The solution is X");
  });

  it("collapses multiple newlines", () => {
    expect(bodyPreview("Line 1\n\n\n\nLine 2")).toBe("Line 1\nLine 2");
  });

  it("strips h1-h6 headings", () => {
    expect(bodyPreview("# H1\n## H2\n### H3")).toBe("H1\nH2\nH3");
  });
});

// ── prettyAuthor ──────────────────────────────────────────────────────

describe("prettyAuthor", () => {
  it("capitalizes single-word author", () => {
    expect(prettyAuthor("planner")).toBe("Planner");
  });

  it("handles underscore-separated authors", () => {
    expect(prettyAuthor("conflict_resolver")).toBe("Conflict Resolver");
  });

  it("strips expert. prefix and capitalizes", () => {
    expect(prettyAuthor("expert.data_analyst")).toBe("Data Analyst");
  });

  it("strips expert. prefix with single word", () => {
    expect(prettyAuthor("expert.researcher")).toBe("Researcher");
  });

  it("handles already capitalized input", () => {
    expect(prettyAuthor("Planner")).toBe("Planner");
  });
});

// ── typeMeta ──────────────────────────────────────────────────────────

describe("typeMeta", () => {
  it("returns metadata for known type 'finding'", () => {
    const meta = typeMeta("finding");
    expect(meta.label).toBe("Finding");
    expect(meta.color).toBeDefined();
    expect(meta.icon).toBeDefined();
  });

  it("returns metadata for known type 'solution'", () => {
    const meta = typeMeta("solution");
    expect(meta.label).toBe("Solution");
  });

  it("returns metadata for known type 'critique'", () => {
    const meta = typeMeta("critique");
    expect(meta.label).toBe("Critique");
  });

  it("returns metadata for known type 'objective'", () => {
    const meta = typeMeta("objective");
    expect(meta.label).toBe("Objective");
  });

  it("returns default metadata for unknown type", () => {
    const meta = typeMeta("unknown_type");
    // Unknown types use the raw type string as the label
    expect(meta.label).toBe("unknown_type");
    expect(meta.color).toBeDefined();
    expect(meta.icon).toBeDefined();
  });

  it("uses raw type string as label for unknown types", () => {
    const meta = typeMeta("my_custom_type");
    expect(meta.label).toBe("my_custom_type");
  });
});

// ── salienceColor ─────────────────────────────────────────────────────

describe("salienceColor", () => {
  it("returns a color string for low salience (0)", () => {
    const color = salienceColor(0);
    expect(color).toBeTruthy();
    expect(typeof color).toBe("string");
  });

  it("returns a color string for mid salience (0.5)", () => {
    const color = salienceColor(0.5);
    expect(color).toBeTruthy();
  });

  it("returns a color string for high salience (1)", () => {
    const color = salienceColor(1);
    expect(color).toBeTruthy();
  });

  it("returns consistent colors for same input", () => {
    expect(salienceColor(0.7)).toBe(salienceColor(0.7));
  });
});
