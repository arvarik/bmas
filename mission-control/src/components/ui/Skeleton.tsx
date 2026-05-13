"use client";

import React from "react";

export interface SkeletonProps {
  variant?: "text" | "metric" | "chart" | "list" | "dag";
  lines?: number;
}

function SkeletonRect({
  width,
  height,
  style,
}: {
  width: string;
  height: string;
  style?: React.CSSProperties;
}) {
  return (
    <div
      className="shimmer"
      style={{
        width,
        height,
        borderRadius: "var(--radius-sm)",
        ...style,
      }}
    />
  );
}

export function Skeleton({ variant = "text", lines = 3 }: SkeletonProps) {
  switch (variant) {
    case "text":
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-3)",
          }}
        >
          {Array.from({ length: lines }).map((_, i) => (
            <SkeletonRect
              key={i}
              width={i === lines - 1 ? "60%" : "100%"}
              height="14px"
            />
          ))}
        </div>
      );

    case "metric":
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-2)",
          }}
        >
          <SkeletonRect width="80px" height="12px" />
          <SkeletonRect width="120px" height="28px" />
          <SkeletonRect width="60px" height="10px" />
        </div>
      );

    case "chart":
      return (
        <div
          style={{
            display: "flex",
            alignItems: "flex-end",
            gap: "var(--space-2)",
            height: 120,
          }}
        >
          {Array.from({ length: 8 }).map((_, i) => (
            <SkeletonRect
              key={i}
              width="100%"
              height={`${30 + Math.sin(i * 0.8) * 40 + 40}%`}
              style={{ borderRadius: "var(--radius-sm) var(--radius-sm) 0 0" }}
            />
          ))}
        </div>
      );

    case "list":
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-3)",
          }}
        >
          {Array.from({ length: lines }).map((_, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "var(--space-3)",
              }}
            >
              <SkeletonRect width="32px" height="32px" />
              <div
                style={{
                  flex: 1,
                  display: "flex",
                  flexDirection: "column",
                  gap: "var(--space-1)",
                }}
              >
                <SkeletonRect width={i % 2 === 0 ? "70%" : "50%"} height="12px" />
                <SkeletonRect width="40%" height="10px" />
              </div>
            </div>
          ))}
        </div>
      );

    case "dag":
      return (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: "var(--space-4)",
            padding: "var(--space-4)",
          }}
        >
          {/* Row 1 — single node centered */}
          <div />
          <SkeletonRect width="100%" height="48px" />
          <div />
          {/* Row 2 — two nodes */}
          <SkeletonRect width="100%" height="48px" />
          <div />
          <SkeletonRect width="100%" height="48px" />
          {/* Row 3 — single node centered */}
          <div />
          <SkeletonRect width="100%" height="48px" />
          <div />
        </div>
      );

    default:
      return <SkeletonRect width="100%" height="40px" />;
  }
}

export default Skeleton;
