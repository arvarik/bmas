"use client";



import type { LucideIcon } from "lucide-react";
import { ActionButton } from "./ActionButton";

export interface EmptyStateProps {
  icon: LucideIcon;
  message: string;
  hint?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
}

export function EmptyState({ icon: Icon, message, hint, action }: EmptyStateProps) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        height: "100%",
        gap: "var(--space-3)",
        padding: "var(--space-8)",
        textAlign: "center",
      }}
    >
      <Icon
        size={48}
        style={{
          color: "var(--text-tertiary)",
          strokeWidth: 1.5,
        }}
      />

      <p
        style={{
          fontSize: "var(--text-base)",
          fontWeight: "var(--weight-medium)",
          color: "var(--text-secondary)",
          lineHeight: "var(--leading-base)",
          maxWidth: 320,
        }}
      >
        {message}
      </p>

      {hint && (
        <p
          style={{
            fontSize: "var(--text-sm)",
            color: "var(--text-tertiary)",
            lineHeight: "var(--leading-sm)",
            maxWidth: 280,
          }}
        >
          {hint}
        </p>
      )}

      {action && (
        <ActionButton variant="primary" onClick={action.onClick}>
          {action.label}
        </ActionButton>
      )}
    </div>
  );
}

export default EmptyState;
