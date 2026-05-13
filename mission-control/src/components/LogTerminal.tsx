"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { TerminalPane } from "@/components/ui/TerminalPane";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";

interface LogTerminalProps { role: AgentRole; }

const ACCENT_RGB: Record<AgentRole, string> = {
  planner: "153;102;204", executor: "46;184;163", auditor: "224;156;63",
};

function fmtTs(ts: string): string {
  try {
    const ms = parseInt(ts.split("-")[0], 10);
    const d = new Date(ms);
    if (isNaN(d.getTime())) throw 0;
    return `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}.${String(d.getMilliseconds()).padStart(3,"0")}`;
  } catch { return new Date().toISOString().slice(11, 23); }
}

function fmtLevel(l?: string): string {
  if (!l) return "";
  switch (l.toLowerCase()) {
    case "error": return "\x1b[1;31m ERR \x1b[0m ";
    case "warn": case "warning": return "\x1b[1;33m WRN \x1b[0m ";
    case "info": return "\x1b[1;36m INF \x1b[0m ";
    case "debug": return "\x1b[2m DBG \x1b[0m ";
    default: return `\x1b[2m ${l.slice(0,3).toUpperCase()} \x1b[0m `;
  }
}

export default function LogTerminal({ role }: LogTerminalProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const [connected, setConnected] = useState(true);
  const [reconnecting, setReconnecting] = useState(false);

  const handleClear = useCallback(() => { termRef.current?.clear(); }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const rgb = ACCENT_RGB[role];
    const color = AGENT_COLORS[role];

    const term = new Terminal({
      cursorBlink: true, cursorStyle: "bar",
      fontFamily: "var(--font-mono), 'JetBrains Mono', monospace",
      fontSize: 13, lineHeight: 1.4, scrollback: 5000, convertEol: true, disableStdin: true,
      theme: {
        background: "hsl(222, 47%, 6%)", foreground: "hsl(210, 20%, 96%)",
        cursor: color, cursorAccent: color, selectionBackground: `${color}44`,
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

    term.writeln(`\x1b[1;38;2;${rgb}m  ${role.charAt(0).toUpperCase()+role.slice(1)} Agent Log\x1b[0m`);
    term.writeln(`\x1b[2m${"─".repeat(Math.max(term.cols-1,40))}\x1b[0m`);
    term.writeln("");
    term.writeln("\x1b[2m  Waiting for agent output…\x1b[0m");

    const ro = new ResizeObserver(() => { requestAnimationFrame(() => { try { fit.fit(); } catch {} }); });
    ro.observe(el);

    const es = new EventSource("/api/logs");
    let gotFirst = false;
    es.addEventListener("log", (ev: MessageEvent) => {
      try {
        const d = JSON.parse(ev.data) as { agent: string; ts: string; line?: string; level?: string };
        if (d.agent !== role) return;
        if (!gotFirst) { term.clear(); gotFirst = true; }
        term.writeln(`\x1b[2m${fmtTs(d.ts)}\x1b[0m ${fmtLevel(d.level)}${d.line ?? JSON.stringify(d)}`);
      } catch { term.writeln(`\x1b[31m[parse error]\x1b[0m ${ev.data}`); }
    });
    es.addEventListener("open", () => { setConnected(true); setReconnecting(false); });
    es.addEventListener("error", () => { setConnected(false); setReconnecting(true); term.writeln("\x1b[33m⚠ Connection lost — reconnecting…\x1b[0m"); });

    return () => { es.close(); ro.disconnect(); term.dispose(); termRef.current = null; };
  }, [role]);

  return (
    <TerminalPane role={role} connected={connected} reconnecting={reconnecting} onClear={handleClear}>
      <div ref={containerRef} style={{ width: "100%", height: "100%", padding: 2 }} />
    </TerminalPane>
  );
}
