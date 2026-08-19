"use client";

import { useCallback, useEffect, useState } from "react";

export interface ReadinessCheck {
  id: string;
  label: string;
  ready: boolean;
  detail: string;
  fix: string;
}

export interface ReadinessDocument {
  status: "ready" | "not_ready";
  checks: ReadinessCheck[];
}

interface ReadinessPanelProps {
  onReadyChange: (ready: boolean) => void;
  showReadyGuide?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseReadiness(value: unknown): ReadinessDocument {
  if (!isRecord(value) || (value.status !== "ready" && value.status !== "not_ready")) {
    throw new Error("The daemon returned an invalid readiness status.");
  }
  if (!Array.isArray(value.checks)) {
    throw new Error("The daemon returned an invalid readiness checklist.");
  }
  const checks = value.checks.map((check): ReadinessCheck => {
    if (
      !isRecord(check)
      || typeof check.id !== "string"
      || typeof check.label !== "string"
      || typeof check.ready !== "boolean"
      || typeof check.detail !== "string"
      || typeof check.fix !== "string"
    ) {
      throw new Error("The daemon returned an invalid readiness check.");
    }
    return {
      id: check.id,
      label: check.label,
      ready: check.ready,
      detail: check.detail,
      fix: check.fix,
    };
  });
  return { status: value.status, checks };
}

async function requestReadiness(): Promise<ReadinessDocument> {
  const response = await fetch("/api/readiness", { cache: "no-store" });
  const raw: unknown = await response.json();
  if (!response.ok) {
    const message = isRecord(raw) && typeof raw.error === "string"
      ? raw.error
      : `Readiness returned HTTP ${response.status}`;
    throw new Error(message);
  }
  return parseReadiness(raw);
}

export function ReadinessPanel({
  onReadyChange,
  showReadyGuide = false,
}: ReadinessPanelProps) {
  const [document, setDocument] = useState<ReadinessDocument | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    onReadyChange(false);
    try {
      const next = await requestReadiness();
      setDocument(next);
      onReadyChange(next.status === "ready");
    } catch (caught) {
      setDocument(null);
      setError(caught instanceof Error ? caught.message : "Readiness is unavailable.");
    } finally {
      setLoading(false);
    }
  }, [onReadyChange]);

  useEffect(() => {
    let cancelled = false;
    requestReadiness()
      .then((next) => {
        if (cancelled) return;
        setDocument(next);
        setLoading(false);
        onReadyChange(next.status === "ready");
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(caught instanceof Error ? caught.message : "Readiness is unavailable.");
        setLoading(false);
        onReadyChange(false);
      });
    return () => {
      cancelled = true;
    };
  }, [onReadyChange]);

  if (loading) {
    return (
      <section className="readiness readiness--loading" aria-live="polite">
        <span className="readiness__dot readiness__dot--loading" />
        Checking the starter services…
      </section>
    );
  }

  if (error) {
    return (
      <section className="readiness readiness--error" role="alert">
        <div>
          <strong>Mission Control cannot reach the daemon.</strong>
          <p>{error} Run <code>./scripts/bmas doctor</code> for exact checks.</p>
        </div>
        <button className="readiness__retry" type="button" onClick={() => void load()}>
          Retry
        </button>
      </section>
    );
  }

  if (document?.status === "ready") {
    return (
      <section className="readiness readiness--ready" aria-live="polite">
        <span className="readiness__dot readiness__dot--ready" />
        <div>
          <strong>The classic starter is ready.</strong>
          {showReadyGuide ? <p>Describe one task below, then select the send button.</p> : null}
        </div>
      </section>
    );
  }

  return (
    <section className="readiness readiness--error" role="alert">
      <div className="readiness__body">
        <strong>Complete these checks before you submit a task.</strong>
        <ul className="readiness__checks">
          {document?.checks.map((check) => (
            <li key={check.id} className="readiness__check">
              <span
                className={`readiness__dot ${check.ready ? "readiness__dot--ready" : "readiness__dot--failed"}`}
              />
              <div>
                <span className="readiness__check-label">{check.label}</span>
                <span className="readiness__check-detail">{check.detail}</span>
                {!check.ready ? <code className="readiness__fix">{check.fix}</code> : null}
              </div>
            </li>
          ))}
        </ul>
      </div>
      <button className="readiness__retry" type="button" onClick={() => void load()}>
        Retry
      </button>
    </section>
  );
}
