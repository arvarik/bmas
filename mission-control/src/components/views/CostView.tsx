"use client";

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/Skeleton";

const CostTracker = dynamic(() => import("@/components/CostTracker"), {
  ssr: false,
  loading: () => (
    <div style={{ padding: "var(--space-5)", background: "var(--surface-overlay)", borderRadius: "var(--radius-lg)" }}>
      <Skeleton variant="chart" />
    </div>
  ),
});

export default function CostView() {
  return (
    <div className="view-container cost-view">
      <CostTracker />
    </div>
  );
}
