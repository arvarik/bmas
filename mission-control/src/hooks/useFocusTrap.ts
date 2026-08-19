"use client";

import type { RefObject } from "react";
import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function isVisible(element: HTMLElement): boolean {
  if (
    element.hidden
    || element.tabIndex < 0
    || element.closest("[hidden], [inert], [aria-hidden='true']")
  ) {
    return false;
  }

  const style = window.getComputedStyle(element);
  return style.display !== "none"
    && style.visibility !== "hidden"
    && element.getClientRects().length > 0;
}

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter(isVisible);
}

interface UseFocusTrapOptions {
  active: boolean;
  containerRef: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  returnFocusRef?: RefObject<HTMLElement | null>;
  onEscape?: () => void;
}

/**
 * Keeps keyboard focus inside an open dialog or drawer.
 * The hook restores focus to the control that opened the surface.
 */
export function useFocusTrap({
  active,
  containerRef,
  initialFocusRef,
  returnFocusRef,
  onEscape,
}: UseFocusTrapOptions): void {
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  }, [onEscape]);

  useEffect(() => {
    if (!active) return;

    const currentContainer = containerRef.current;
    if (!currentContainer) return;
    const container: HTMLElement = currentContainer;

    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const explicitReturnFocus = returnFocusRef?.current;

    const focusInitialControl = () => {
      const target = initialFocusRef?.current
        ?? getFocusableElements(container)[0]
        ?? container;
      target.focus();
    };

    const animationFrame = window.requestAnimationFrame(focusInitialControl);

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && onEscapeRef.current) {
        event.preventDefault();
        event.stopPropagation();
        onEscapeRef.current();
        return;
      }

      if (event.key !== "Tab") return;

      const focusableElements = getFocusableElements(container);
      if (focusableElements.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }

      const first = focusableElements[0];
      const last = focusableElements[focusableElements.length - 1];
      const current = document.activeElement;

      if (event.shiftKey && (current === first || !container.contains(current))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (current === last || !container.contains(current))) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      document.removeEventListener("keydown", handleKeyDown, true);
      const returnTarget = explicitReturnFocus ?? previousFocus;
      if (returnTarget?.isConnected) returnTarget.focus();
    };
  }, [active, containerRef, initialFocusRef, returnFocusRef]);
}
