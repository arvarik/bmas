import React from "react";
import Link from "next/link";

export default function NotFound() {
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
              alt="Lost Swarm Ant Mascot"
              className="landing__logo animate-float"
              style={{
                filter: "grayscale(20%) opacity(85%) drop-shadow(0 4px 12px hsl(222 47% 4% / 0.4))",
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
            404 — Path Uncharted
          </h1>
          <p
            className="landing__subtitle"
            style={{
              maxWidth: "460px",
              margin: "var(--space-2) auto var(--space-6)",
              lineHeight: "var(--leading-base)",
            }}
          >
            {"The coordinate path you requested does not exist in the swarm's environment. The agents are unable to trace a path here."}
          </p>
          <div style={{ display: "flex", justifyContent: "center" }}>
            <Link
              href="/"
              className="task-sidebar__new-btn"
              style={{
                display: "inline-flex",
                width: "auto",
                padding: "0 var(--space-6)",
                alignItems: "center",
              }}
            >
              Return to Mission Control
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
