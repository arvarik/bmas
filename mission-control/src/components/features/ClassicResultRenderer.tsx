"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function extractFencedJson(text: string): unknown | null {
  const fenceMatch = text.match(/```(?:json)?\s*\n([\s\S]+?)\n```/);
  if (!fenceMatch) return null;
  try {
    return JSON.parse(fenceMatch[1]);
  } catch {
    return null;
  }
}

function extractBoardBody(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const entry = value as Record<string, unknown>;
  return typeof entry.body === "string" && entry.body.trim()
    ? entry.body.trim()
    : null;
}

function extractFromArray(values: unknown[]): string | null {
  const solution = values.find(
    (entry) => typeof entry === "object"
      && entry !== null
      && (entry as Record<string, unknown>).type === "solution",
  ) ?? values.at(-1);
  return extractBoardBody(solution);
}

function looksLikeMarkdown(text: string): boolean {
  return /^#{1,6}\s/m.test(text)
    || /^\s*[-*+]\s/m.test(text)
    || /`[^`]+`/.test(text)
    || /^\d+\.\s/m.test(text)
    || /\*\*[^*]+\*\*/.test(text)
    || /\[[^\]]+\]\([^)]+\)/.test(text)
    || /^\s*\|.+\|/m.test(text);
}

function parseJson(text: string): { parsed: true; value: unknown } | { parsed: false } {
  try {
    return { parsed: true, value: JSON.parse(text) as unknown };
  } catch {
    return { parsed: false };
  }
}

function MarkdownResult({ content }: { content: string }) {
  return (
    <div className="result-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}

function PlainResult({ content }: { content: string }) {
  const paragraphs = content.split(/\n\n+/);
  if (paragraphs.length === 1) return <p className="result-plain">{content}</p>;
  return (
    <div className="result-plain-multi">
      {paragraphs.map((paragraph, index) => (
        <p key={`${index}-${paragraph.slice(0, 24)}`} className="result-plain-para">
          {paragraph}
        </p>
      ))}
    </div>
  );
}

function JsonObject({ data, depth }: { data: Record<string, unknown>; depth: number }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  return (
    <div className={`result-json-object ${depth === 0 ? "result-json-object--root" : ""}`}>
      {Object.entries(data).map(([key, value]) => {
        const isComplex = typeof value === "object" && value !== null;
        const isOpen = expanded[key] !== false;
        const header = (
          <>
            {isComplex ? (
              <span className="result-json-row__chevron">
                {isOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
              </span>
            ) : null}
            <span className="result-json-row__key">{key}</span>
            {!isComplex ? (
              <span className="result-json-row__value">
                {typeof value === "string" && looksLikeMarkdown(value)
                  ? <MarkdownResult content={value} />
                  : JSON.stringify(value)}
              </span>
            ) : null}
            {isComplex && !isOpen ? (
              <span className="result-json-row__collapsed-hint">
                {Array.isArray(value) ? `[${value.length} items]` : "{…}"}
              </span>
            ) : null}
          </>
        );
        return (
          <div key={key} className="result-json-row">
            {isComplex ? (
              <button
                type="button"
                className="result-json-row__header result-json-row__header--clickable"
                onClick={() => setExpanded((current) => ({ ...current, [key]: !isOpen }))}
                aria-expanded={isOpen}
              >
                {header}
              </button>
            ) : (
              <div className="result-json-row__header">{header}</div>
            )}
            {isComplex && isOpen ? (
              <div className="result-json-row__children">
                <JsonResult data={value} depth={depth + 1} />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function JsonResult({ data, depth = 0 }: { data: unknown; depth?: number }) {
  if (typeof data === "string") return <PlainResult content={data} />;
  if (Array.isArray(data)) {
    return (
      <div className="result-json-array">
        {data.map((item, index) => (
          <div key={index} className="result-json-array__item">
            <span className="result-json-array__index">{index + 1}</span>
            <span className="result-json-value"><JsonResult data={item} depth={depth + 1} /></span>
          </div>
        ))}
      </div>
    );
  }
  if (typeof data === "object" && data !== null) {
    return <JsonObject data={data as Record<string, unknown>} depth={depth} />;
  }
  return <PlainResult content={String(data)} />;
}

export function ClassicResultRenderer({
  content,
  formats,
}: {
  content: string;
  formats: readonly string[];
}) {
  const trimmed = content.trim();
  const hasClassicAnswer = formats.includes("answer");
  const canRenderJson = hasClassicAnswer
    || formats.includes("json")
    || formats.includes("classic-board-entry");
  const canRenderMarkdown = hasClassicAnswer || formats.includes("markdown");
  const fenced = canRenderJson ? extractFencedJson(trimmed) : null;
  if (fenced !== null) {
    const body = Array.isArray(fenced) ? extractFromArray(fenced) : extractBoardBody(fenced);
    if (body && canRenderMarkdown) return <MarkdownResult content={body} />;
    return <JsonResult data={fenced} />;
  }

  if (canRenderJson) {
    const json = parseJson(trimmed);
    if (json.parsed) {
      const body = Array.isArray(json.value)
        ? extractFromArray(json.value)
        : extractBoardBody(json.value);
      if (body && canRenderMarkdown) return <MarkdownResult content={body} />;
      return <JsonResult data={json.value} />;
    }
  }
  if (canRenderMarkdown && looksLikeMarkdown(trimmed)) {
    return <MarkdownResult content={trimmed} />;
  }
  return <PlainResult content={trimmed} />;
}
