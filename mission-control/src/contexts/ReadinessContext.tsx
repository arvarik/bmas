"use client";

/**
 * ReadinessContext — one shared readiness document for the shell.
 *
 * The top-bar system button shows the state. The task composer uses
 * `ready` to gate submission. `refresh()` asks the daemon for a new document.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { requestReadiness, type ReadinessDocument } from "@/lib/readiness";

export interface ReadinessState {
  document: ReadinessDocument | null;
  error: string;
  loading: boolean;
  ready: boolean;
  checkedAt: string | null;
  refresh: () => Promise<void>;
}

const ReadinessContext = createContext<ReadinessState | null>(null);

const AUTO_REFRESH_MS = 60_000;

export function ReadinessProvider({ children }: { children: React.ReactNode }) {
  const [document, setDocument] = useState<ReadinessDocument | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback(async () => {
    if (inFlight.current) return inFlight.current;
    setLoading(true);
    const request = (async () => {
      try {
        const next = await requestReadiness();
        setDocument(next);
        setError("");
      } catch (caught) {
        setDocument(null);
        setError(caught instanceof Error ? caught.message : "Readiness is unavailable.");
      } finally {
        setCheckedAt(new Date().toISOString());
        setLoading(false);
        inFlight.current = null;
      }
    })();
    inFlight.current = request;
    return request;
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (window.document.visibilityState === "visible") void refresh();
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const value = useMemo<ReadinessState>(() => ({
    document,
    error,
    loading,
    ready: document?.status === "ready",
    checkedAt,
    refresh,
  }), [checkedAt, document, error, loading, refresh]);

  return <ReadinessContext.Provider value={value}>{children}</ReadinessContext.Provider>;
}

export function useReadiness(): ReadinessState {
  const context = useContext(ReadinessContext);
  if (!context) throw new Error("useReadiness must be used within ReadinessProvider");
  return context;
}
