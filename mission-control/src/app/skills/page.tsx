"use client";

/**
 * Skills Page — /skills
 *
 * System-level skills explorer page. Wraps the existing SkillsExplorer
 * component in a proper route page with a back link.
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

export default function SkillsPage() {
  return (
    <div className="view-container skills-view">
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
          Skills
        </span>
      </div>
      <SkillsExplorer />
    </div>
  );
}
