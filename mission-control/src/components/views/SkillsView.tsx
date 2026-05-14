"use client";

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/Skeleton";

const SkillsExplorer = dynamic(() => import("@/components/SkillsExplorer"), {
  ssr: false,
  loading: () => (
    <div style={{ padding: "var(--space-5)", background: "var(--surface-overlay)", borderRadius: "var(--radius-lg)" }}>
      <Skeleton variant="list" />
    </div>
  ),
});

export default function SkillsView() {
  return (
    <div className="view-container skills-view">
      <SkillsExplorer />
    </div>
  );
}
