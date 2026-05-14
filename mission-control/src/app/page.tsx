"use client";

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/Skeleton";
import { useAppShell } from "./AppShell";

// ── Dynamic imports for each view ────────────────────────────────────

function ViewSkeleton({ variant }: { variant: "text" | "metric" | "chart" | "list" | "dag" }) {
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

const OverviewView = dynamic(() => import("@/components/views/OverviewView"), {
  ssr: false, loading: () => <ViewSkeleton variant="metric" />,
});
const DAGView = dynamic(() => import("@/components/views/DAGView"), {
  ssr: false, loading: () => <ViewSkeleton variant="dag" />,
});
const LogsView = dynamic(() => import("@/components/views/LogsView"), {
  ssr: false, loading: () => <ViewSkeleton variant="text" />,
});
const OperatorView = dynamic(() => import("@/components/views/OperatorView"), {
  ssr: false, loading: () => <ViewSkeleton variant="text" />,
});
const BlackboardView = dynamic(() => import("@/components/views/BlackboardView"), {
  ssr: false, loading: () => <ViewSkeleton variant="list" />,
});
const CostView = dynamic(() => import("@/components/views/CostView"), {
  ssr: false, loading: () => <ViewSkeleton variant="chart" />,
});
const InfraView = dynamic(() => import("@/components/views/InfraView"), {
  ssr: false, loading: () => <ViewSkeleton variant="metric" />,
});
const SkillsView = dynamic(() => import("@/components/views/SkillsView"), {
  ssr: false, loading: () => <ViewSkeleton variant="list" />,
});

// ── View Router ──────────────────────────────────────────────────────

export default function Home() {
  const { activeNav, setActiveNav } = useAppShell();

  switch (activeNav) {
    case "dag":
      return <DAGView />;
    case "logs":
      return <LogsView />;
    case "operator":
      return <OperatorView />;
    case "blackboard":
      return <BlackboardView />;
    case "cost":
      return <CostView />;
    case "infra":
      return <InfraView />;
    case "skills":
      return <SkillsView />;
    case "overview":
    default:
      return <OverviewView onNavigate={setActiveNav} />;
  }
}
