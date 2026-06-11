"use client";

/**
 * TaskLogTerminal — task-scoped xterm.js terminal component.
 *
 * Unlike LogTerminal (which opens its own EventSource to /api/logs),
 * this component receives log entries via props from useTaskData().
 * New entries are piped to xterm.js via terminal.writeln() on each
 * React update.
 *
 * Implements smart auto-scroll: if the user is at the bottom of the
 * scrollback buffer, new output auto-scrolls. If the user has scrolled
 * up, auto-scroll is suspended and a "N new lines" pill appears.
 *
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { TerminalPane } from "@/components/ui/TerminalPane";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";
import type { LogEntry } from "@/hooks/useTaskStream";

// ── ANSI color accents per role ───────────────────────────────────────

const ACCENT_RGB: Record<AgentRole, string> = {
  planner: "153;102;204",
  executor: "46;184;163",
  auditor: "224;156;63",
};

function fmtLevel(l?: string): string {
  if (!l) return "";
  switch (l.toLowerCase()) {
    case "error": return "\x1b[1;31m ERR \x1b[0m ";
    case "warn": case "warning": return "\x1b[1;33m WRN \x1b[0m ";
    case "info": return "\x1b[1;36m INF \x1b[0m ";
    case "debug": return "\x1b[2m DBG \x1b[0m ";
    default: return `\x1b[2m ${l.slice(0, 3).toUpperCase()} \x1b[0m `;
  }
}

function fmtTs(ts: string): string {
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) throw 0;
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
  } catch {
    return new Date().toISOString().slice(11, 19);
  }
}

// ── Component ─────────────────────────────────────────────────────────

interface TaskLogTerminalProps {
  role: AgentRole;
  logs: LogEntry[];
}

export default function TaskLogTerminal({ role, logs }: TaskLogTerminalProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const lastIndexRef = useRef(0);
  const [newLineCount, setNewLineCount] = useState(0);
  const [isAtBottom, setIsAtBottom] = useState(true);

  const handleClear = useCallback(() => {
    termRef.current?.clear();
    lastIndexRef.current = logs.length;
    setNewLineCount(0);
  }, [logs.length]);

  // ── Initialize xterm.js ──────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const rgb = ACCENT_RGB[role];
    const color = AGENT_COLORS[role];

    const term = new Terminal({
      cursorBlink: false,
      cursorStyle: "bar",
      fontFamily: "var(--font-mono), 'JetBrains Mono', monospace",
      fontSize: 13,
      lineHeight: 1.4,
      scrollback: 5000,
      convertEol: true,
      disableStdin: true,
      theme: {
        background: "hsl(222, 47%, 6%)",
        foreground: "hsl(210, 20%, 96%)",
        cursor: color,
        cursorAccent: color,
        selectionBackground: `${color}44`,
        black: "#1e293b", red: "#f87171", green: "#4ade80", yellow: "#facc15",
        blue: "#60a5fa", magenta: "#c084fc", cyan: "#22d3ee", white: "#f1f5f9",
        brightBlack: "#475569", brightRed: "#fca5a5", brightGreen: "#86efac",
        brightYellow: "#fde68a", brightBlue: "#93c5fd", brightMagenta: "#d8b4fe",
        brightCyan: "#67e8f9", brightWhite: "#f8fafc",
      },
    });
    termRef.current = term;
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    fit.fit();

    // Header
    term.writeln(`\x1b[1;38;2;${rgb}m  ${role.charAt(0).toUpperCase() + role.slice(1)} Agent Log\x1b[0m`);
    term.writeln(`\x1b[2m${"─".repeat(Math.max(term.cols - 1, 40))}\x1b[0m`);
    term.writeln("");

    // Smart auto-scroll detection
    const checkScroll = () => {
      const buf = term.buffer.active;
      const atBottom = buf.baseY + term.rows >= buf.length;
      setIsAtBottom(atBottom);
      if (atBottom) setNewLineCount(0);
    };
    term.onScroll(checkScroll);

    const ro = new ResizeObserver(() => {
      requestAnimationFrame(() => { try { fit.fit(); } catch {} });
    });
    ro.observe(el);

    lastIndexRef.current = 0;

    return () => {
      ro.disconnect();
      term.dispose();
      termRef.current = null;
    };
  }, [role]);

  // ── Pipe new log entries to xterm ────────────────────────────────
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;

    const start = lastIndexRef.current;
    if (logs.length <= start) return;

    let newCount = 0;
    for (let i = start; i < logs.length; i++) {
      const entry = logs[i];
      const ts = fmtTs(entry.timestamp);
      const level = fmtLevel(entry.level);
      term.writeln(`\x1b[2m${ts}\x1b[0m ${level}${entry.message}`);
      newCount++;
    }
    lastIndexRef.current = logs.length;

    if (!isAtBottom) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- updating count based on new streamed data
      setNewLineCount((prev) => prev + newCount);
    } else {
      // Auto-scroll to bottom
      term.scrollToBottom();
    }
  }, [logs, isAtBottom]);

  const handleScrollToBottom = useCallback(() => {
    termRef.current?.scrollToBottom();
    setNewLineCount(0);
    setIsAtBottom(true);
  }, []);

  return (
    <TerminalPane role={role} connected={true} reconnecting={false} onClear={handleClear}>
      <div ref={containerRef} style={{ width: "100%", height: "100%", padding: 2 }} />
      {/* "New lines" pill */}
      {newLineCount > 0 && (
        <button
          className="new-output-pill"
          onClick={handleScrollToBottom}
        >
          ↓ {newLineCount} new line{newLineCount !== 1 ? "s" : ""}
        </button>
      )}
    </TerminalPane>
  );
}
