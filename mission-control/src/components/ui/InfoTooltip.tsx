"use client";

/**
 * InfoTooltip — A beautiful, accessible tooltip for contextual help.
 *
 * Supports both hover (desktop) and click/tap (mobile).
 * Renders a small ⓘ icon that reveals a floating panel with explanation text.
 *
 * @example
 * <InfoTooltip content="Salience measures how relevant this entry is to the debate." />
 */

import React, { useState, useRef, useCallback, useEffect } from "react";
import { Info } from "lucide-react";

interface InfoTooltipProps {
  /** The explanation text shown in the tooltip */
  content: string;
  /** Optional title for the tooltip */
  title?: string;
  /** Icon size. Default: 12 */
  size?: number;
  /** Position preference. Default: "top" */
  position?: "top" | "bottom" | "left" | "right";
  /** Custom icon color */
  iconColor?: string;
}

export function InfoTooltip({
  content,
  title,
  size = 12,
  position = "top",
  iconColor,
}: InfoTooltipProps) {
  const [visible, setVisible] = useState(false);
  const [placed, setPlaced] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const hideTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = useCallback(() => {
    if (hideTimeout.current) {
      clearTimeout(hideTimeout.current);
      hideTimeout.current = null;
    }
    setVisible(true);
  }, []);

  const hide = useCallback(() => {
    hideTimeout.current = setTimeout(() => setVisible(false), 150);
  }, []);

  const toggle = useCallback(() => {
    setVisible((v) => !v);
  }, []);

  // Close on outside click (mobile)
  useEffect(() => {
    if (!visible) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      if (
        triggerRef.current?.contains(e.target as Node) ||
        tooltipRef.current?.contains(e.target as Node)
      ) return;
      setVisible(false);
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [visible]);

  // Position calculation
  useEffect(() => {
    if (!visible || !triggerRef.current || !tooltipRef.current) {
      setPlaced(null);
      return;
    }
    const trig = triggerRef.current.getBoundingClientRect();
    const tip = tooltipRef.current.getBoundingClientRect();
    const gap = 8;

    let top = 0;
    let left = 0;

    switch (position) {
      case "top":
        top = trig.top - tip.height - gap;
        left = trig.left + trig.width / 2 - tip.width / 2;
        break;
      case "bottom":
        top = trig.bottom + gap;
        left = trig.left + trig.width / 2 - tip.width / 2;
        break;
      case "left":
        top = trig.top + trig.height / 2 - tip.height / 2;
        left = trig.left - tip.width - gap;
        break;
      case "right":
        top = trig.top + trig.height / 2 - tip.height / 2;
        left = trig.right + gap;
        break;
    }

    // Clamp to viewport
    left = Math.max(8, Math.min(left, window.innerWidth - tip.width - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - tip.height - 8));

    setPlaced({ top, left });
  }, [visible, position]);

  // Close on escape
  useEffect(() => {
    if (!visible) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setVisible(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [visible]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="info-tooltip__trigger"
        onClick={toggle}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        aria-label={title ?? "More information"}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          background: "none",
          border: "none",
          cursor: "help",
          padding: 2,
          borderRadius: "var(--radius-full)",
          color: iconColor ?? "var(--text-tertiary)",
          transition: "color 200ms ease, background 200ms ease",
        }}
      >
        <Info size={size} />
      </button>

      {visible && (
        <div
          ref={tooltipRef}
          className="info-tooltip__panel"
          onMouseEnter={show}
          onMouseLeave={hide}
          role="tooltip"
          style={{
            position: "fixed",
            top: placed?.top ?? -9999,
            left: placed?.left ?? -9999,
            zIndex: 1000,
            maxWidth: 280,
            padding: "10px 14px",
            borderRadius: "var(--radius-md)",
            background: "var(--surface-raised)",
            border: "1px solid var(--border-default)",
            boxShadow: "0 8px 32px hsl(222 47% 4% / 0.6), 0 2px 8px hsl(222 47% 4% / 0.3)",
            fontSize: "var(--text-xs)",
            lineHeight: 1.5,
            color: "var(--text-secondary)",
            opacity: placed ? 1 : 0,
            transform: placed ? "translateY(0)" : "translateY(4px)",
            transition: "opacity 150ms ease, transform 150ms ease",
            pointerEvents: "auto",
          }}
        >
          {title && (
            <div
              style={{
                fontWeight: "var(--weight-semibold)",
                color: "var(--text-primary)",
                marginBottom: 4,
                fontSize: "var(--text-xs)",
                letterSpacing: "0.02em",
              }}
            >
              {title}
            </div>
          )}
          <div>{content}</div>
        </div>
      )}
    </>
  );
}

export default InfoTooltip;
