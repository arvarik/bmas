"use client";

/**
 * BoardEntryDetail — a docked drawer showing the full content of a selected
 * board entry: complete body, metadata, salience breakdown, and clickable
 * in/out reference links so the operator can walk the debate.
 */

import React, { useMemo } from "react";
import { X } from "lucide-react";
import { authorColor } from "@/lib/design-tokens";
import {
  type MergedBoardEntry,
  typeMeta,
  salienceColor,
  prettyAuthor,
} from "./boardModel";

interface BoardEntryDetailProps {
  entry: MergedBoardEntry;
  allEntries: MergedBoardEntry[];
  onClose: () => void;
  onSelect: (id: string) => void;
}

export function BoardEntryDetail({ entry, allEntries, onClose, onSelect }: BoardEntryDetailProps) {
  const meta = typeMeta(entry.type);
  const Icon = meta.icon;
  const aColor = authorColor(entry.author);

  const byId = useMemo(() => new Map(allEntries.map((e) => [e.id, e])), [allEntries]);
  const refsOut = useMemo(
    () => entry.refs.map((r) => byId.get(r)).filter(Boolean) as MergedBoardEntry[],
    [entry.refs, byId],
  );
  const refsIn = useMemo(
    () => allEntries.filter((e) => e.refs.includes(entry.id)),
    [allEntries, entry.id],
  );

  return (
    <div
      className="bb-detail entry-appear"
      style={{
        position: "absolute",
        top: 0,
        right: 0,
        bottom: 0,
        width: "min(420px, 90%)",
        background: "var(--surface-raised)",
        borderLeft: `1px solid var(--border-default)`,
        display: "flex",
        flexDirection: "column",
        boxShadow: "-12px 0 32px hsl(222 47% 4% / 0.5)",
        zIndex: 20,
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-2)",
          padding: "var(--space-3) var(--space-4)",
          borderBottom: "1px solid var(--border-default)",
          flexShrink: 0,
        }}
      >
        <Icon size={16} style={{ color: meta.color }} />
        <span
          style={{
            fontSize: "var(--text-sm)",
            fontWeight: "var(--weight-semibold)",
            color: meta.color,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
          }}
        >
          {meta.label}
        </span>
        <span style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", color: "var(--text-tertiary)" }}>
          {entry.id}
        </span>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 26,
            height: 26,
            borderRadius: "var(--radius-full)",
            border: "none",
            background: "var(--surface-hover)",
            color: "var(--text-secondary)",
            cursor: "pointer",
          }}
        >
          <X size={14} />
        </button>
      </div>

      {/* Scroll body */}
      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-4)", display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
        {/* Meta grid */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}>
          <Chip>
            <span style={{ width: 8, height: 8, borderRadius: "var(--radius-full)", background: aColor }} />
            {prettyAuthor(entry.author)}
          </Chip>
          <Chip>{entry.round === 0 ? "Genesis" : `Round ${entry.round}`}</Chip>
          <Chip
            color={
              entry.status === "removed"
                ? "var(--status-error)"
                : entry.status === "superseded"
                  ? "var(--text-tertiary)"
                  : "var(--status-success)"
            }
          >
            {entry.status}
          </Chip>
        </div>

        {/* Salience + confidence meters */}
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <Meter label="Salience" value={entry.salience} color={salienceColor(entry.salience)} />
          <Meter label="Confidence" value={entry.confidence} color="var(--accent-primary)" />
        </div>

        {/* Title */}
        {entry.title && (
          <div
            style={{
              fontSize: "var(--text-base)",
              fontWeight: "var(--weight-semibold)",
              color: "var(--text-primary)",
              lineHeight: "var(--leading-base)",
            }}
          >
            {entry.title}
          </div>
        )}

        {/* Full body */}
        <div
          style={{
            fontSize: "var(--text-sm)",
            color: "var(--text-secondary)",
            lineHeight: "var(--leading-base)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {entry.body || "(no body)"}
        </div>

        {/* Refs out */}
        {refsOut.length > 0 && (
          <RefSection title="References" entries={refsOut} onSelect={onSelect} />
        )}
        {/* Refs in */}
        {refsIn.length > 0 && (
          <RefSection title="Referenced by" entries={refsIn} onSelect={onSelect} />
        )}
      </div>
    </div>
  );
}

function Chip({ children, color }: { children: React.ReactNode; color?: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: "var(--text-xs)",
        padding: "3px 10px",
        borderRadius: "var(--radius-full)",
        background: "var(--surface-overlay)",
        border: "1px solid var(--border-subtle)",
        color: color ?? "var(--text-secondary)",
        textTransform: color ? "capitalize" : "none",
      }}
    >
      {children}
    </span>
  );
}

function Meter({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
      <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", width: 76, flexShrink: 0 }}>
        {label}
      </span>
      <div
        style={{
          flex: 1,
          height: 6,
          borderRadius: "var(--radius-full)",
          background: "var(--surface-active)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${Math.min(value * 100, 100)}%`,
            background: color,
            borderRadius: "var(--radius-full)",
            transition: "width 400ms ease",
          }}
        />
      </div>
      <span
        style={{
          fontSize: "var(--text-xs)",
          fontFamily: "var(--font-mono)",
          color: "var(--text-secondary)",
          width: 38,
          textAlign: "right",
          flexShrink: 0,
        }}
      >
        {(value * 100).toFixed(0)}%
      </span>
    </div>
  );
}

function RefSection({
  title,
  entries,
  onSelect,
}: {
  title: string;
  entries: MergedBoardEntry[];
  onSelect: (id: string) => void;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
      <span
        style={{
          fontSize: "var(--text-xs)",
          fontWeight: "var(--weight-semibold)",
          color: "var(--text-tertiary)",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
        }}
      >
        {title}
      </span>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {entries.map((e) => {
          const m = typeMeta(e.type);
          const Icon = m.icon;
          return (
            <button
              key={e.id}
              type="button"
              onClick={() => onSelect(e.id)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "var(--space-2)",
                padding: "var(--space-2)",
                borderRadius: "var(--radius-sm)",
                border: "1px solid var(--border-subtle)",
                background: "var(--surface-overlay)",
                color: "var(--text-secondary)",
                cursor: "pointer",
                textAlign: "left",
                width: "100%",
              }}
            >
              <Icon size={13} style={{ color: m.color, flexShrink: 0 }} />
              <span style={{ fontSize: "10px", fontFamily: "var(--font-mono)", color: "var(--text-tertiary)", flexShrink: 0 }}>
                {e.id}
              </span>
              <span
                style={{
                  fontSize: "var(--text-xs)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {e.title || m.label}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default BoardEntryDetail;
