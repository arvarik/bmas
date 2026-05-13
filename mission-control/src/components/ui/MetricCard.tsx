"use client";

import React, { useEffect, useRef, useCallback } from "react";

export interface MetricCardProps {
  label: string;
  value: string | number;
  previousValue?: number;
  format?: "currency" | "number" | "percent";
}

function formatValue(
  value: number,
  format: "currency" | "number" | "percent"
): string {
  switch (format) {
    case "currency":
      return `$${value.toFixed(4)}`;
    case "percent":
      return `${value.toFixed(1)}%`;
    case "number":
    default:
      return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
}

export function MetricCard({
  label,
  value,
  previousValue,
  format = "number",
}: MetricCardProps) {
  const displayRef = useRef<HTMLSpanElement>(null);
  const animatingRef = useRef<number | null>(null);
  const currentValueRef = useRef<number>(
    typeof value === "number" ? value : 0
  );

  const animateValue = useCallback(
    (from: number, to: number) => {
      if (animatingRef.current) {
        cancelAnimationFrame(animatingRef.current);
      }

      const duration = 400;
      const startTime = performance.now();

      function tick(now: number) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // Ease-out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = from + (to - from) * eased;

        if (displayRef.current) {
          displayRef.current.textContent = formatValue(current, format);
        }
        currentValueRef.current = current;

        if (progress < 1) {
          animatingRef.current = requestAnimationFrame(tick);
        } else {
          animatingRef.current = null;
          // Add pop animation
          displayRef.current?.classList.remove("metric-pop");
          // Trigger reflow for re-animation
          void displayRef.current?.offsetWidth;
          displayRef.current?.classList.add("metric-pop");
        }
      }

      animatingRef.current = requestAnimationFrame(tick);
    },
    [format]
  );

  useEffect(() => {
    if (typeof value === "number") {
      const prev = previousValue ?? currentValueRef.current;
      if (prev !== value) {
        animateValue(prev, value);
      } else {
        currentValueRef.current = value;
      }
    }

    return () => {
      if (animatingRef.current) {
        cancelAnimationFrame(animatingRef.current);
      }
    };
  }, [value, previousValue, animateValue]);

  const displayValue =
    typeof value === "number" ? formatValue(value, format) : value;

  // Delta indicator
  const delta =
    typeof value === "number" && previousValue !== undefined
      ? value - previousValue
      : null;

  return (
    <div
      style={{
        background: "var(--surface-overlay)",
        borderRadius: "var(--radius-lg)",
        padding: "var(--space-4)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
        transition: "background 150ms ease",
        cursor: "default",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "var(--surface-hover)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "var(--surface-overlay)";
      }}
    >
      {/* Label */}
      <span
        style={{
          fontSize: "var(--text-sm)",
          color: "var(--text-secondary)",
          lineHeight: "var(--leading-sm)",
          fontWeight: "var(--weight-medium)",
        }}
      >
        {label}
      </span>

      {/* Value */}
      <span
        ref={displayRef}
        style={{
          fontSize: "var(--text-metric)",
          fontFamily: "var(--font-mono)",
          fontWeight: "var(--weight-semibold)",
          lineHeight: "var(--leading-metric)",
          color: "var(--text-primary)",
        }}
      >
        {displayValue}
      </span>

      {/* Delta */}
      {delta !== null && delta !== 0 && (
        <span
          style={{
            fontSize: "var(--text-xs)",
            fontWeight: "var(--weight-medium)",
            color:
              delta > 0
                ? "var(--status-success)"
                : "var(--status-error)",
          }}
        >
          {delta > 0 ? "↑" : "↓"}{" "}
          {Math.abs(delta).toLocaleString("en-US", {
            maximumFractionDigits: 2,
          })}
        </span>
      )}
    </div>
  );
}

export default MetricCard;
