"use client";

import React from "react";
import { Skeleton } from "./Skeleton";
import { EmptyState } from "./EmptyState";
import { ActionButton } from "./ActionButton";
import type { LucideIcon } from "lucide-react";
import { AlertCircle } from "lucide-react";

export interface PanelProps {
  title: string;
  subtitle?: string;
  headerExtra?: React.ReactNode;
  actions?: React.ReactNode;
  status?: "loading" | "error" | "empty";
  errorMessage?: string;
  emptyIcon?: LucideIcon;
  emptyMessage?: string;
  emptyHint?: string;
  onRetry?: () => void;
  children?: React.ReactNode;
  className?: string;
}

export function Panel({
  title,
  subtitle,
  headerExtra,
  actions,
  status,
  errorMessage,
  emptyIcon,
  emptyMessage,
  emptyHint,
  onRetry,
  children,
  className = "",
}: PanelProps) {
  return (
    <div
      className={className}
      style={{
        background: "var(--surface-overlay)",
        borderRadius: "var(--radius-lg)",
        padding: "var(--space-5)",
        display: "flex",
        flexDirection: "column",
        height: "100%",
        overflow: "hidden",
      }}
    >
      {/* ── Header ──────────────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexShrink: 0,
        }}
      >
        <div>
          <h2
            style={{
              fontSize: "var(--text-lg)",
              fontWeight: "var(--weight-semibold)",
              lineHeight: "var(--leading-lg)",
              color: "var(--text-primary)",
              margin: 0,
            }}
          >
            {title}
          </h2>
          {subtitle && (
            <p
              style={{
                fontSize: "var(--text-sm)",
                color: "var(--text-secondary)",
                lineHeight: "var(--leading-sm)",
                marginTop: "var(--space-1)",
              }}
            >
              {subtitle}
            </p>
          )}
        </div>
        {headerExtra}
        {actions && (
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            {actions}
          </div>
        )}
      </div>

      {/* ── Divider ─────────────────────────────────────────────── */}
      <div
        style={{
          height: 1,
          background: "var(--border-default)",
          margin: `var(--space-4) 0`,
          flexShrink: 0,
        }}
      />

      {/* ── Body ────────────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          overflow: "auto",
          position: "relative",
        }}
      >
        {status === "loading" && <Skeleton variant="list" lines={4} />}

        {status === "error" && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              gap: "var(--space-3)",
              borderLeft: "3px solid var(--status-error)",
              paddingLeft: "var(--space-4)",
            }}
          >
            <AlertCircle
              size={32}
              style={{ color: "var(--status-error)" }}
            />
            <p
              style={{
                fontSize: "var(--text-sm)",
                color: "var(--text-secondary)",
                textAlign: "center",
              }}
            >
              {errorMessage || "An error occurred"}
            </p>
            {onRetry && (
              <ActionButton variant="secondary" onClick={onRetry}>
                Retry
              </ActionButton>
            )}
          </div>
        )}

        {status === "empty" && (
          <EmptyState
            icon={emptyIcon || AlertCircle}
            message={emptyMessage || "No data available"}
            hint={emptyHint}
            action={
              onRetry ? { label: "Retry", onClick: onRetry } : undefined
            }
          />
        )}

        {!status && children}
      </div>
    </div>
  );
}

export default Panel;
