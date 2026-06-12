"use client";

/**
 * GlobalFirehose — Virtualized interleaved trace event list.
 *
 * Shows all trace events across all agents, color-coded by author,
 * with auto-scroll and filter bar.
 *
 * @module Phase 5 (doc 13 §4)
 */

import { useMemo, useRef, useState, useCallback, useEffect } from "react";
import { authorColor } from "@/lib/design-tokens";
import type { TraceEvent } from "@/hooks/useTaskStream";
import { Filter, ArrowDown, XCircle } from "lucide-react";

interface GlobalFirehoseProps {
  events: TraceEvent[];
  maxVisible?: number;
}

export function GlobalFirehose({
  events,
  maxVisible = 500,
}: GlobalFirehoseProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [authorFilter, setAuthorFilter] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const lastLenRef = useRef(0);

  // Unique authors and types for filter bar
  const authors = useMemo(() => {
    const set = new Set<string>();
    for (const e of events) {
      if (e.actor) set.add(e.actor);
    }
    return Array.from(set).sort();
  }, [events]);

  const types = useMemo(() => {
    const set = new Set<string>();
    for (const e of events) {
      set.add(e.type);
    }
    return Array.from(set).sort();
  }, [events]);

  // Filtered events
  const filtered = useMemo(() => {
    let result = events;
    if (authorFilter) {
      result = result.filter((e) => e.actor === authorFilter);
    }
    if (typeFilter) {
      result = result.filter((e) => e.type === typeFilter);
    }
    return result.slice(-maxVisible);
  }, [events, authorFilter, typeFilter, maxVisible]);

  // Auto-scroll logic
  useEffect(() => {
    if (filtered.length > lastLenRef.current) {
      if (autoScroll && containerRef.current) {
        containerRef.current.scrollTop = containerRef.current.scrollHeight;
      } else {
        setNewCount((c) => c + (filtered.length - lastLenRef.current));
      }
    }
    lastLenRef.current = filtered.length;
  }, [filtered.length, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const atBottom = scrollHeight - scrollTop - clientHeight < 40;
    setAutoScroll(atBottom);
    if (atBottom) setNewCount(0);
  }, []);

  const jumpToBottom = useCallback(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
    setAutoScroll(true);
    setNewCount(0);
  }, []);

  return (
    <div className="global-firehose">
      {/* Filter bar */}
      <div className="global-firehose__filters">
        <Filter size={12} style={{ opacity: 0.5 }} />

        <select
          className="global-firehose__select"
          value={authorFilter ?? ""}
          onChange={(e) => setAuthorFilter(e.target.value || null)}
          aria-label="Filter by author"
        >
          <option value="">All agents</option>
          {authors.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>

        <select
          className="global-firehose__select"
          value={typeFilter ?? ""}
          onChange={(e) => setTypeFilter(e.target.value || null)}
          aria-label="Filter by type"
        >
          <option value="">All types</option>
          {types.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>

        {(authorFilter || typeFilter) && (
          <button
            className="global-firehose__clear"
            onClick={() => { setAuthorFilter(null); setTypeFilter(null); }}
            title="Clear filters"
          >
            <XCircle size={12} />
          </button>
        )}
      </div>

      {/* Event list */}
      <div
        ref={containerRef}
        className="global-firehose__list"
        onScroll={handleScroll}
      >
        {filtered.map((event, idx) => (
          <div key={idx} className="global-firehose__event">
            <span
              className="global-firehose__author"
              style={{ color: authorColor(event.actor ?? "unknown") }}
            >
              {event.actor ?? "?"}
            </span>
            <span className="global-firehose__type">{event.type}</span>
            <span className="global-firehose__content">
              {(event.content ?? "").slice(0, 120)}
            </span>
          </div>
        ))}
      </div>

      {/* Jump-to-bottom pill */}
      {!autoScroll && newCount > 0 && (
        <button
          className="global-firehose__jump"
          onClick={jumpToBottom}
        >
          <ArrowDown size={12} />
          {newCount} new
        </button>
      )}
    </div>
  );
}
