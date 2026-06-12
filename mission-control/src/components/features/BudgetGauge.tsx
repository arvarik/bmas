"use client";

/**
 * BudgetGauge — Circular SVG progress ring for budget visualization.
 *
 * Shows spent/ceiling with animated fill and status-aware coloring:
 *   - Green (< 50%) → Amber (50-75%) → Red (> 75%)
 *
 * Used in the Cost tab and Mission cockpit TopBar.
 *
 * @module Phase 5 (doc 09 §5)
 */

import { useMemo } from "react";
import { STATUS_COLORS } from "@/lib/design-tokens";

interface BudgetGaugeProps {
  spent: number;
  ceiling: number;
  /** Size in px (default 120) */
  size?: number;
  /** Stroke width (default 8) */
  strokeWidth?: number;
  /** Compact mode hides the label */
  compact?: boolean;
}

export function BudgetGauge({
  spent,
  ceiling,
  size = 120,
  strokeWidth = 8,
  compact = false,
}: BudgetGaugeProps) {
  const percentage = useMemo(() => {
    if (ceiling <= 0) return 0;
    return Math.min(100, (spent / ceiling) * 100);
  }, [spent, ceiling]);

  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (percentage / 100) * circumference;
  const center = size / 2;

  // Status-aware color: green → amber → red
  const color = useMemo(() => {
    if (percentage >= 75) return STATUS_COLORS.error;
    if (percentage >= 50) return STATUS_COLORS.paused;
    return STATUS_COLORS.success;
  }, [percentage]);

  const trackColor = "hsl(222, 36%, 16%)";

  return (
    <div
      className="budget-gauge"
      style={{
        width: size,
        height: compact ? size : size + 32,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 4,
      }}
    >
      <div style={{ position: "relative", width: size, height: size }}>
        <svg
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
          style={{ transform: "rotate(-90deg)" }}
        >
          {/* Track */}
          <circle
            cx={center}
            cy={center}
            r={radius}
            fill="none"
            stroke={trackColor}
            strokeWidth={strokeWidth}
          />
          {/* Progress */}
          <circle
            cx={center}
            cy={center}
            r={radius}
            fill="none"
            stroke={color}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            style={{
              transition: "stroke-dashoffset 0.6s ease, stroke 0.3s ease",
            }}
          />
        </svg>
        {/* Center text */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 0,
          }}
        >
          <span
            style={{
              fontSize: size * 0.16,
              fontWeight: 700,
              color: "hsl(210, 20%, 96%)",
              fontVariantNumeric: "tabular-nums",
              lineHeight: 1.2,
            }}
          >
            ${spent.toFixed(4)}
          </span>
          <span
            style={{
              fontSize: size * 0.1,
              color: "hsl(215, 15%, 65%)",
              lineHeight: 1.2,
            }}
          >
            / ${ceiling.toFixed(2)}
          </span>
        </div>
      </div>

      {!compact && (
        <span
          style={{
            fontSize: 11,
            color: color,
            fontWeight: 600,
            letterSpacing: "0.03em",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {percentage.toFixed(0)}% used
        </span>
      )}
    </div>
  );
}
