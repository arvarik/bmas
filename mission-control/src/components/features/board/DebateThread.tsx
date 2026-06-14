"use client";

/**
 * DebateThread — renders a ref-linked debate cluster as an indented tree.
 *
 * A thread root (proposal / plan / finding / objective) is followed by the
 * critiques, rebuttals, conflicts and solutions that reference it, nested by
 * their `refs`. Connector rails make the proposal → critique → rebuttal →
 * resolution chain legible.
 */

import React from "react";
import { type ThreadNode } from "./boardModel";
import { BoardEntryCard } from "./BoardEntryCard";

interface DebateThreadProps {
  node: ThreadNode;
  depth?: number;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
}

export function DebateThread({ node, depth = 0, selectedId, onSelect }: DebateThreadProps) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
      <BoardEntryCard
        entry={node.entry}
        selected={selectedId === node.entry.id}
        onSelect={onSelect}
        compact={depth > 0}
      />

      {node.children.length > 0 && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-2)",
            marginLeft: "var(--space-3)",
            paddingLeft: "var(--space-3)",
            borderLeft: "2px solid var(--border-default)",
          }}
        >
          {node.children.map((child) => (
            <DebateThread
              key={child.entry.id}
              node={child}
              depth={depth + 1}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default DebateThread;
