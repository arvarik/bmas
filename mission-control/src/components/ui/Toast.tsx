"use client";

import React, {
  createContext,
  useCallback,
  useRef,
  useState,
} from "react";
import { CheckCircle2, XCircle, Info, X } from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────

export type ToastType = "success" | "error" | "info";

export interface ToastOptions {
  type: ToastType;
  message: string;
}

interface ToastItem extends ToastOptions {
  id: string;
  exiting: boolean;
}

interface ToastContextValue {
  toast: (options: ToastOptions) => void;
}

// ── Context ──────────────────────────────────────────────────────────

export const ToastContext = createContext<ToastContextValue>({
  toast: () => {},
});

// ── Icons per type ───────────────────────────────────────────────────

const TOAST_ICONS: Record<ToastType, React.ElementType> = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
};

const TOAST_BORDER_COLORS: Record<ToastType, string> = {
  success: "var(--status-success)",
  error: "var(--status-error)",
  info: "var(--accent-primary)",
};

const TOAST_ICON_COLORS: Record<ToastType, string> = {
  success: "var(--status-success)",
  error: "var(--status-error)",
  info: "var(--accent-primary)",
};

// ── Provider ─────────────────────────────────────────────────────────

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const idCounter = useRef(0);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) =>
      prev.map((t) => (t.id === id ? { ...t, exiting: true } : t))
    );
    // Remove after exit animation
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 150);
  }, []);

  const toast = useCallback(
    (options: ToastOptions) => {
      const id = `toast-${++idCounter.current}`;
      const newToast: ToastItem = { ...options, id, exiting: false };

      setToasts((prev) => {
        const next = [newToast, ...prev];
        // Max 3 visible — dismiss oldest
        if (next.length > 3) {
          const oldest = next[next.length - 1];
          setTimeout(() => dismissToast(oldest.id), 0);
        }
        return next.slice(0, 4); // Keep 4 temporarily for exit animation
      });

      // Auto-dismiss success/info after 3s
      if (options.type !== "error") {
        setTimeout(() => dismissToast(id), 3000);
      }
    },
    [dismissToast]
  );

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}

      {/* ── Toast Container ─────────────────────────────────────── */}
      <div
        aria-live="polite"
        aria-label="Notifications"
        style={{
          position: "fixed",
          bottom: "var(--space-6)",
          right: "var(--space-6)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          zIndex: 9999,
          pointerEvents: "none",
          width: "var(--toast-width)",
          maxWidth: "calc(100vw - 48px)",
        }}
      >
        {toasts.map((t) => {
          const Icon = TOAST_ICONS[t.type];
          return (
            <div
              key={t.id}
              className={t.exiting ? "toast-exit" : "toast-enter"}
              style={{
                background: "var(--surface-overlay)",
                boxShadow: "var(--shadow-md)",
                borderRadius: "var(--radius-md)",
                borderLeft: `3px solid ${TOAST_BORDER_COLORS[t.type]}`,
                padding: "var(--space-3) var(--space-4)",
                display: "flex",
                alignItems: "center",
                gap: "var(--space-3)",
                pointerEvents: "auto",
              }}
            >
              <Icon
                size={18}
                style={{
                  color: TOAST_ICON_COLORS[t.type],
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  flex: 1,
                  fontSize: "var(--text-sm)",
                  color: "var(--text-primary)",
                  lineHeight: "var(--leading-sm)",
                }}
              >
                {t.message}
              </span>
              <button
                onClick={() => dismissToast(t.id)}
                aria-label="Dismiss notification"
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: 24,
                  height: 24,
                  border: "none",
                  background: "transparent",
                  color: "var(--text-tertiary)",
                  cursor: "pointer",
                  borderRadius: "var(--radius-sm)",
                  flexShrink: 0,
                  padding: 0,
                  transition: "color 150ms ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.color = "var(--text-primary)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.color = "var(--text-tertiary)";
                }}
              >
                <X size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export default ToastProvider;
