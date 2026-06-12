"use client";

/**
 * Agents Page — /agents
 *
 * Displays agent node cards with profiles, skills, and SOUL.md per node.
 */

import dynamic from "next/dynamic";
import Link from "next/link";
import { Skeleton } from "@/components/ui/Skeleton";
import { ArrowLeft } from "lucide-react";

const SkillsExplorer = dynamic(() => import("@/components/features/SkillsExplorer"), {
  ssr: false,
  loading: () => (
    <div style={{
      padding: "var(--space-5)", background: "var(--surface-overlay)",
      borderRadius: "var(--radius-lg)",
    }}>
      <Skeleton variant="list" />
    </div>
  ),
});

export default function AgentsPage() {
  return (
    <div className="view-container agents-view">
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--space-2)",
        paddingBottom: "var(--space-2)",
      }}>
        <Link href="/" style={{
          display: "flex", alignItems: "center", gap: "var(--space-1)",
          color: "var(--text-tertiary)", fontSize: "var(--text-sm)",
          textDecoration: "none",
        }}>
          <ArrowLeft size={14} />
          <span>Home</span>
        </Link>
        <span style={{ color: "var(--text-tertiary)", fontSize: "var(--text-sm)" }}>/</span>
        <span style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)", fontWeight: "var(--weight-medium)" }}>
          Agents
        </span>
      </div>
      <SkillsExplorer />
    </div>
  );
}
