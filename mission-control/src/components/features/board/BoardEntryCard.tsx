"use client";

/**
 * BoardEntryCard — a single board entry rendered as a rich, scannable card.
 *
 * Encodes everything an operator needs at a glance: entry type (color +
 * glyph), author identity (color dot), round, lifecycle status, salience
 * (heat bar) and confidence. Used by both the Timeline and Threads views.
 */

import React from "react";
import { authorColor } from "@/lib/design-tokens";
import {
  type MergedBoardEntry,
  typeMeta,
  salienceColor,
  prettyAuthor,
  bodyPreview,
} from "./boardModel";

interface BoardEntryCardProps {
  entry: MergedBoardEntry;
  selected?: boolean;
  compact?: boolean;
  onSelect?: (id: string) => void;
}

export function BoardEntryCard({ entry, selected, compact, onSelect }: BoardEntryCardProps) {
  const meta = typeMeta(entry.type);
  const Icon = meta.icon;
  const aColor = authorColor(entry.author);
  const isRemoved = entry.status === "removed";
  const isSuperseded = entry.status === "superseded";
  const dimmed = isRemoved || isSuperseded;

  return (
    <button
      type="button"
      onClick={() => onSelect?.(entry.id)}
      className="bb-entry-card entry-appear"
      style={{
        position: "relative",
        textAlign: "left",
        width: "100%",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        padding: "var(--space-3)",
        paddingLeft: "var(--space-4)",
        background: selected ? "var(--surface-hover)" : "var(--surface-overlay)",
        border: `1px solid ${selected ? meta.color : "var(--border-subtle)"}`,
        borderRadius: "var(--radius-md)",
        cursor: "pointer",
        opacity: isRemoved ? 0.45 : isSuperseded ? 0.7 : 1,
        transition: "background 150ms ease, border-color 150ms ease, opacity 200ms ease",
        overflow: "hidden",
      }}
    >
      {/* Type accent stripe */}
      <span
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          bottom: 0,
          width: 3,
          background: meta.color,
        }}
      />

      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", minWidth: 0 }}>
        <Icon size={14} style={{ color: meta.color, flexShrink: 0 }} />
        <span
          style={{
            fontSize: "var(--text-xs)",
            fontWeight: "var(--weight-semibold)",
            color: meta.color,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            flexShrink: 0,
          }}
        >
          {meta.label}
        </span>

        {/* author chip */}
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: "var(--text-xs)",
            color: "var(--text-secondary)",
            minWidth: 0,
            overflow: "hidden",
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: "var(--radius-full)",
              background: aColor,
              flexShrink: 0,
            }}
          />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {prettyAuthor(entry.author)}
          </span>
        </span>

        <span style={{ flex: 1 }} />

        {/* status badge */}
        {dimmed && (
          <span
            style={{
              fontSize: "10px",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              padding: "1px 6px",
              borderRadius: "var(--radius-full)",
              color: isRemoved ? "var(--status-error)" : "var(--text-tertiary)",
              border: `1px solid ${isRemoved ? "var(--status-error)" : "var(--border-default)"}`,
              flexShrink: 0,
            }}
          >
            {entry.status}
          </span>
        )}

        {/* round badge */}
        <span
          style={{
            fontSize: "10px",
            fontFamily: "var(--font-mono)",
            color: "var(--text-tertiary)",
            flexShrink: 0,
          }}
        >
          {entry.round === 0 ? "GEN" : `R${entry.round}`}
        </span>
      </div>

      {/* Title */}
      {entry.title && (
        <div
          style={{
            fontSize: "var(--text-sm)",
            fontWeight: "var(--weight-semibold)",
            color: "var(--text-primary)",
            lineHeight: "var(--leading-sm)",
            textDecoration: isRemoved ? "line-through" : "none",
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {entry.title}
        </div>
      )}

      {/* Body preview */}
      {!compact && (
        <div
          style={{
            fontSize: "var(--text-xs)",
            color: "var(--text-secondary)",
            lineHeight: "var(--leading-sm)",
            display: "-webkit-box",
            WebkitLineClamp: 3,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
            whiteSpace: "pre-wrap",
          }}
        >
          {bodyPreview(entry.body)}
        </div>
      )}

      {/* Footer: salience + confidence + refs */}
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
        {/* salience heat bar */}
        <div
          title={`Salience ${(entry.salience * 100).toFixed(0)}% — How relevant this entry is to the debate (based on confidence, recency, citations, and open critiques)`}
          style={{ display: "flex", alignItems: "center", gap: 5, flex: 1, minWidth: 0 }}
        >
          <span style={{ fontSize: "10px", color: "var(--text-tertiary)", flexShrink: 0 }}>SAL</span>
          <div
            style={{
              flex: 1,
              height: 4,
              borderRadius: "var(--radius-full)",
              background: "var(--surface-active)",
              overflow: "hidden",
              minWidth: 28,
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${Math.min(entry.salience * 100, 100)}%`,
                background: salienceColor(entry.salience),
                borderRadius: "var(--radius-full)",
                transition: "width 400ms ease",
              }}
            />
          </div>
        </div>

        {/* confidence */}
        <span
          title={`Confidence ${(entry.confidence * 100).toFixed(0)}% — The agent's self-assessed certainty in this entry`}
          style={{
            fontSize: "10px",
            fontFamily: "var(--font-mono)",
            color: "var(--text-tertiary)",
            flexShrink: 0,
          }}
        >
          {(entry.confidence * 100).toFixed(0)}% conf
        </span>

        {/* refs out */}
        {entry.refs.length > 0 && (
          <span
            style={{
              fontSize: "10px",
              fontFamily: "var(--font-mono)",
              color: "var(--text-tertiary)",
              flexShrink: 0,
            }}
          >
            ↳ {entry.refs.length}
          </span>
        )}

        <span
          style={{
            fontSize: "10px",
            fontFamily: "var(--font-mono)",
            color: "var(--text-tertiary)",
            flexShrink: 0,
          }}
        >
          {entry.id}
        </span>
      </div>
    </button>
  );
}

export default BoardEntryCard;
