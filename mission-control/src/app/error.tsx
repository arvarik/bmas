"use client";

import React, { useEffect } from "react";
import Link from "next/link";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Swarm Exception caught by boundary:", error);
  }, [error]);

  return (
    <div className="landing">
      <div
        className="landing__container"
        style={{
          textAlign: "center",
          justifyContent: "center",
          minHeight: "75vh",
        }}
      >
        <div className="landing__hero">
          <div className="landing__logo-container">
            <img
              src="/ant-head.png"
              alt="Swarm Error Mascot"
              className="landing__logo animate-float"
              style={{
                filter: "hue-rotate(320deg) saturate(1.2) drop-shadow(0 4px 12px hsl(222 47% 4% / 0.4))",
              }}
              width={110}
              height={110}
            />
          </div>
          <h1
            className="landing__title"
            style={{
              fontSize: "var(--text-xl)",
              marginTop: "var(--space-4)",
            }}
          >
            500 — Swarm Disruption
          </h1>
          <p
            className="landing__subtitle"
            style={{
              maxWidth: "460px",
              margin: "var(--space-2) auto var(--space-6)",
              lineHeight: "var(--leading-base)",
            }}
          >
            An unexpected error occurred while communicating with the agents. Swarm operations have been interrupted.
          </p>
          <div
            style={{
              display: "flex",
              justifyContent: "center",
              gap: "var(--space-3)",
            }}
          >
            <button
              onClick={() => reset()}
              className="task-sidebar__new-btn"
              style={{
                display: "inline-flex",
                width: "auto",
                padding: "0 var(--space-6)",
                alignItems: "center",
                cursor: "pointer",
              }}
            >
              Retry Connection
            </button>
            <Link
              href="/"
              className="landing__attach-btn"
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: "auto",
                height: "36px",
                padding: "0 var(--space-4)",
                borderRadius: "var(--radius-md)",
                textDecoration: "none",
              }}
            >
              Go Home
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
