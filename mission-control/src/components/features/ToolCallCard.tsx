"use client";

/**
 * ToolCallCard — collapsible tool-call + result card (doc 09 §3).
 *
 * Collapsed: one-line summary (tool name + status + duration).
 * Expanded: full args + result body.
 */

import React, { useState, useMemo } from "react";

interface ToolCallData {
  tool?: string;
  status?: string;
  args?: Record<string, unknown>;
  result?: string;
  duration_ms?: number;
  ok?: boolean;
}

interface ToolCallCardProps {
  content: string;
}

export function ToolCallCard({ content }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);

  const parsed = useMemo<ToolCallData | null>(() => {
    try {
      return JSON.parse(content) as ToolCallData;
    } catch {
      return null;
    }
  }, [content]);

  if (!parsed) {
    // Fallback: render raw content
    return (
      <span style={{ color: "var(--text-secondary)", wordBreak: "break-word" }}>
        {content.length > 200 ? content.slice(0, 200) + "…" : content}
      </span>
    );
  }

  const isError = parsed.ok === false || parsed.status === "error";
  const autoExpand = isError && !expanded;

  return (
    <div
      className="tool-card"
      style={{
        background: "var(--surface-overlay)",
        borderRadius: "var(--radius-md)",
        border: `1px solid ${isError ? "var(--status-error)" : "var(--border-default)"}`,
        borderLeft: isError ? "3px solid var(--status-error)" : undefined,
        overflow: "hidden",
      }}
    >
      {/* Collapsed summary */}
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-2)",
          padding: "var(--space-1) var(--space-2)",
          width: "100%",
          background: "none",
          border: "none",
          cursor: "pointer",
          fontFamily: "var(--font-mono)",
          fontSize: "var(--text-xs)",
          color: "var(--text-secondary)",
          textAlign: "left",
        }}
      >
        <span style={{ color: "var(--text-tertiary)" }}>
          {expanded || autoExpand ? "▾" : "▸"}
        </span>
        <span style={{ fontWeight: "var(--weight-semibold)", color: "var(--text-primary)" }}>
          {parsed.tool ?? "tool_call"}
        </span>
        {parsed.status && (
          <span
            style={{
              padding: "0 4px",
              borderRadius: "var(--radius-sm)",
              background: isError ? "hsl(0 84% 60% / 0.15)" : "var(--surface-active)",
              color: isError ? "var(--status-error)" : "var(--text-tertiary)",
            }}
          >
            {parsed.status}
          </span>
        )}
        {parsed.duration_ms != null && (
          <span
            style={{
              marginLeft: "auto",
              fontVariantNumeric: "tabular-nums",
              color: "var(--text-tertiary)",
            }}
          >
            {parsed.duration_ms}ms
          </span>
        )}
      </button>

      {/* Expanded details */}
      {(expanded || autoExpand) && (
        <div
          style={{
            padding: "var(--space-2)",
            borderTop: "1px solid var(--border-subtle)",
            fontSize: "var(--text-xs)",
            fontFamily: "var(--font-mono)",
          }}
        >
          {parsed.args && (
            <div style={{ marginBottom: "var(--space-2)" }}>
              <div style={{ color: "var(--text-tertiary)", marginBottom: 2 }}>args:</div>
              <pre
                style={{
                  color: "var(--text-secondary)",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  margin: 0,
                  maxHeight: 200,
                  overflow: "auto",
                }}
              >
                {JSON.stringify(parsed.args, null, 2)}
              </pre>
            </div>
          )}
          {parsed.result && (
            <div>
              <div style={{ color: "var(--text-tertiary)", marginBottom: 2 }}>result:</div>
              <pre
                style={{
                  color: isError ? "var(--status-error)" : "var(--text-secondary)",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  margin: 0,
                  maxHeight: 200,
                  overflow: "auto",
                }}
              >
                {parsed.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ToolCallCard;
