"use client";

/**
 * RichContent — Shared markdown/rich-text renderer for board entries, logs, etc.
 *
 * Uses ReactMarkdown + remark-gfm for full CommonMark + GFM support
 * (headings, tables, task lists, fenced code, strikethrough, links).
 *
 * Falls back to pre-formatted text if the content doesn't look like markdown.
 */

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Heuristic: does this string contain markdown syntax worth rendering? */
function looksLikeMarkdown(text: string): boolean {
  return (
    /^#{1,6}\s/m.test(text) ||       // headings
    /^\s*[-*+]\s/m.test(text) ||     // bullet lists
    /`[^`]+`/.test(text) ||          // inline code
    /^\d+\.\s/m.test(text) ||        // numbered lists
    /\*\*[^*]+\*\*/.test(text) ||    // bold
    /\[[^\]]+\]\([^)]+\)/.test(text) || // links
    /^\s*\|.+\|/m.test(text) ||      // tables
    /```[\s\S]+```/.test(text)       // fenced code blocks
  );
}

interface RichContentProps {
  content: string;
  /** Additional CSS class name */
  className?: string;
  /** If true, force markdown rendering even if heuristic says no */
  forceMarkdown?: boolean;
  /** Max height before scroll (CSS value). Default: none */
  maxHeight?: string;
}

export function RichContent({
  content,
  className = "",
  forceMarkdown = false,
  maxHeight,
}: RichContentProps) {
  const trimmed = content.trim();
  if (!trimmed) return null;

  const useMarkdown = forceMarkdown || looksLikeMarkdown(trimmed);

  return (
    <div
      className={`rich-content ${useMarkdown ? "rich-content--md" : "rich-content--plain"} ${className}`}
      style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}
    >
      {useMarkdown ? (
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{trimmed}</ReactMarkdown>
      ) : (
        <pre className="rich-content__pre">{trimmed}</pre>
      )}
    </div>
  );
}

export default RichContent;
