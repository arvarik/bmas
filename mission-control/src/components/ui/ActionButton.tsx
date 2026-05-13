"use client";

import React from "react";

export interface ActionButtonProps {
  variant?: "primary" | "secondary" | "danger";
  loading?: boolean;
  disabled?: boolean;
  children: React.ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  className?: string;
  style?: React.CSSProperties;
}

export function ActionButton({
  variant = "primary",
  loading = false,
  disabled = false,
  children,
  onClick,
  type = "button",
  className = "",
  style: styleProp,
}: ActionButtonProps) {
  const isDisabled = disabled || loading;

  const baseStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "var(--space-2)",
    height: 36,
    padding: "0 var(--space-4)",
    fontSize: "var(--text-sm)",
    fontWeight: "var(--weight-medium)",
    fontFamily: "var(--font-sans)",
    lineHeight: "var(--leading-sm)",
    borderRadius: "var(--radius-md)",
    cursor: isDisabled ? "not-allowed" : "pointer",
    opacity: isDisabled ? 0.4 : 1,
    transition: "background 150ms ease, transform 50ms ease, opacity 150ms ease",
    border: "none",
    whiteSpace: "nowrap",
    position: "relative",
    minWidth: loading ? undefined : undefined,
    ...styleProp,
  };

  const variantStyles: Record<string, React.CSSProperties> = {
    primary: {
      background: "var(--accent-primary)",
      color: "var(--text-inverse)",
    },
    secondary: {
      background: "transparent",
      color: "var(--text-secondary)",
      border: "1px solid var(--border-default)",
    },
    danger: {
      background: "color-mix(in srgb, var(--status-error) 15%, transparent)",
      color: "var(--status-error)",
    },
  };

  return (
    <button
      type={type}
      className={className}
      disabled={isDisabled}
      onClick={isDisabled ? undefined : onClick}
      style={{ ...baseStyle, ...variantStyles[variant] }}
      onMouseEnter={(e) => {
        if (!isDisabled) {
          e.currentTarget.style.filter = "brightness(1.1)";
        }
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.filter = "none";
        e.currentTarget.style.transform = "scale(1)";
      }}
      onMouseDown={(e) => {
        if (!isDisabled) {
          e.currentTarget.style.transform = "scale(0.98)";
        }
      }}
      onMouseUp={(e) => {
        e.currentTarget.style.transform = "scale(1)";
      }}
    >
      {loading ? (
        <>
          {/* Spinner — preserves button width by keeping children invisible */}
          <span
            className="spin"
            style={{
              width: 16,
              height: 16,
              border: "2px solid currentColor",
              borderTopColor: "transparent",
              borderRadius: "var(--radius-full)",
              flexShrink: 0,
            }}
          />
          <span style={{ visibility: "hidden", height: 0, overflow: "hidden" }}>
            {children}
          </span>
        </>
      ) : (
        children
      )}
    </button>
  );
}

export default ActionButton;
