"use client";

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/Skeleton";

const Telemetry = dynamic(() => import("@/components/Telemetry"), {
  ssr: false,
  loading: () => (
    <div style={{ padding: "var(--space-5)", background: "var(--surface-overlay)", borderRadius: "var(--radius-lg)" }}>
      <Skeleton variant="metric" />
    </div>
  ),
});

export default function InfraView() {
  return (
    <div className="view-container infra-view">
      <Telemetry />
    </div>
  );
}
