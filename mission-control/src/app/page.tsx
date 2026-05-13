"use client";

import dynamic from "next/dynamic";
import { useState, useCallback } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { ActionButton } from "@/components/ui/ActionButton";
import { useToast } from "@/hooks/useToast";

// ── Dynamic imports ───────────────────────────────────────────────────

const DAGVisualizer = dynamic(() => import("@/components/DAGVisualizer"), {
  ssr: false, loading: () => <PanelSkeleton variant="dag" />,
});
const LogTerminal = dynamic(() => import("@/components/LogTerminal"), {
  ssr: false, loading: () => <PanelSkeleton variant="text" />,
});
const BlackboardInspector = dynamic(() => import("@/components/BlackboardInspector"), {
  ssr: false, loading: () => <PanelSkeleton variant="list" />,
});
const Telemetry = dynamic(() => import("@/components/Telemetry"), {
  ssr: false, loading: () => <PanelSkeleton variant="metric" />,
});
const HITLControls = dynamic(() => import("@/components/HITLControls"), {
  ssr: false, loading: () => <PanelSkeleton variant="text" />,
});
const CostTracker = dynamic(() => import("@/components/CostTracker"), {
  ssr: false, loading: () => <PanelSkeleton variant="chart" />,
});
const SkillsExplorer = dynamic(() => import("@/components/SkillsExplorer"), {
  ssr: false, loading: () => <PanelSkeleton variant="list" />,
});

// ── Task Submission Form ──────────────────────────────────────────────

function TaskSubmitForm() {
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const { toast } = useToast();

  const handleSubmit = useCallback(async () => {
    if (!task.trim() || submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: task.trim() }),
      });
      if (res.ok) {
        const data = (await res.json()) as { task_id?: string; result?: string };
        toast({ type: "success", message: `Task ${data.task_id ?? ""} submitted successfully.` });
        setTask("");
      } else {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        toast({ type: "error", message: body.error ?? `HTTP ${res.status}` });
      }
    } catch (err) {
      toast({ type: "error", message: err instanceof Error ? err.message : "Network error" });
    } finally {
      setSubmitting(false);
    }
  }, [task, submitting, toast]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  }, [handleSubmit]);

  return (
    <div
      id="task-submit-form"
      style={{
        display: "flex",
        gap: "var(--space-3)",
        padding: "var(--space-3) var(--space-4)",
        background: "var(--surface-overlay)",
        borderRadius: "var(--radius-lg)",
        border: "1px solid var(--border-default)",
        alignItems: "center",
        flexShrink: 0,
        transition: "border-color 200ms ease, box-shadow 200ms ease",
      }}
      onFocus={(e) => {
        e.currentTarget.style.borderColor = "var(--border-focus)";
        e.currentTarget.style.boxShadow = "0 0 0 3px hsl(217 91% 60% / 0.1)";
      }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) {
          e.currentTarget.style.borderColor = "var(--border-default)";
          e.currentTarget.style.boxShadow = "none";
        }
      }}
    >
      <span style={{
        fontSize: "var(--text-lg)",
        lineHeight: 1,
        flexShrink: 0,
        filter: "grayscale(0.2)",
      }}>🚀</span>

      <input
        id="task-input"
        type="text"
        value={task}
        onChange={(e) => setTask(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Describe a task for the swarm to execute…"
        disabled={submitting}
        style={{
          flex: 1,
          background: "transparent",
          border: "none",
          outline: "none",
          fontSize: "var(--text-base)",
          fontFamily: "var(--font-sans)",
          color: "var(--text-primary)",
          lineHeight: "var(--leading-base)",
        }}
      />

      <ActionButton
        variant="primary"
        loading={submitting}
        disabled={!task.trim()}
        onClick={handleSubmit}
        style={{ flexShrink: 0 }}
      >
        Submit Task
      </ActionButton>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────

export default function Home() {
  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0, gap: "var(--space-3)" }}>
      {/* ── Task Submission ─────────────────────────────────────────── */}
      <TaskSubmitForm />

      {/* ── CSS Grid Dashboard ─────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr minmax(200px, 280px) minmax(200px, 280px)",
          gridTemplateRows: "1fr 1fr",
          gridTemplateAreas: `
            "dag dag dag inspector telemetry"
            "term1 term2 term3 hitl cost"
          `,
          gap: "var(--space-3)",
          minHeight: 0,
        }}
      >
        {/* Row 1 */}
        <div style={{ gridArea: "dag", minHeight: 0, minWidth: 0 }}>
          <DAGVisualizer />
        </div>
        <div style={{ gridArea: "inspector", minHeight: 0, minWidth: 0 }}>
          <BlackboardInspector />
        </div>
        <div style={{ gridArea: "telemetry", minHeight: 0, minWidth: 0 }}>
          <Telemetry />
        </div>

        {/* Row 2 */}
        <div style={{ gridArea: "term1", minHeight: 0, minWidth: 0 }}>
          <LogTerminal role="planner" />
        </div>
        <div style={{ gridArea: "term2", minHeight: 0, minWidth: 0 }}>
          <LogTerminal role="executor" />
        </div>
        <div style={{ gridArea: "term3", minHeight: 0, minWidth: 0 }}>
          <LogTerminal role="auditor" />
        </div>
        <div style={{
          gridArea: "hitl", minHeight: 0, minWidth: 0,
          display: "grid", gridTemplateRows: "1fr 1fr", gap: "var(--space-3)",
        }}>
          <HITLControls />
          <SkillsExplorer />
        </div>
        <div style={{ gridArea: "cost", minHeight: 0, minWidth: 0 }}>
          <CostTracker />
        </div>
      </div>

      {/* ── Responsive Styles (injected as inline <style>) ─────────── */}
      <style>{`
        @media (max-width: 1439px) {
          #main-content > div > div:nth-child(2) {
            grid-template-columns: 1fr 1fr 1fr !important;
            grid-template-rows: auto auto auto !important;
            grid-template-areas:
              "dag dag dag"
              "term1 term2 term3"
              "inspector hitl cost" !important;
          }
        }
        @media (max-width: 1023px) {
          #main-content > div > div:nth-child(2) {
            grid-template-columns: 1fr !important;
            grid-template-rows: auto !important;
            grid-template-areas:
              "dag"
              "inspector"
              "term1"
              "term2"
              "term3"
              "hitl"
              "cost"
              "telemetry" !important;
          }
        }
      `}</style>
    </div>
  );
}

// ── Skeleton placeholder ──────────────────────────────────────────────

function PanelSkeleton({ variant }: { variant: "text" | "metric" | "chart" | "list" | "dag" }) {
  return (
    <div style={{
      height: "100%", width: "100%", background: "var(--surface-overlay)",
      borderRadius: "var(--radius-lg)", padding: "var(--space-5)",
      display: "flex", flexDirection: "column", justifyContent: "center",
    }}>
      <Skeleton variant={variant} />
    </div>
  );
}

