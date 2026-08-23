"use client";

/**
 * Browser-local preferences.
 *
 * These settings live in localStorage of one browser. They never reach the
 * daemon. Components read them with `usePreference`, which re-renders when
 * another tab or the Settings page changes a value.
 */

import { useCallback, useSyncExternalStore } from "react";

export interface Preferences {
  /** Runtime the composer selects on load. */
  defaultRuntime: string;
  /** Key that sends a task from the composer. */
  sendKey: "enter" | "mod-enter";
  /** Sidebar starts collapsed on wide screens. */
  sidebarCollapsed: boolean;
  /** Turn off decorative motion (pulses, floats, fades). */
  reducedMotion: boolean;
}

export const PREFERENCE_DEFAULTS: Preferences = {
  defaultRuntime: "classic",
  sendKey: "enter",
  sidebarCollapsed: false,
  reducedMotion: false,
};

export const PREFERENCES_STORAGE_KEY = "bmas:preferences:v1";
const PREFERENCES_EVENT = "bmas-preferences-changed";

export const LOCAL_DATA_KEYS = {
  preferences: PREFERENCES_STORAGE_KEY,
  savedViews: "bmas:task-views:v2",
  pins: "bmas:pinned-tasks:v1",
} as const;

function readRaw(): string {
  try {
    return window.localStorage.getItem(PREFERENCES_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function parsePreferences(raw: string): Preferences {
  if (!raw) return { ...PREFERENCE_DEFAULTS };
  try {
    const parsed = JSON.parse(raw) as Partial<Preferences>;
    return {
      defaultRuntime: typeof parsed.defaultRuntime === "string" && parsed.defaultRuntime
        ? parsed.defaultRuntime
        : PREFERENCE_DEFAULTS.defaultRuntime,
      sendKey: parsed.sendKey === "mod-enter" ? "mod-enter" : "enter",
      sidebarCollapsed: parsed.sidebarCollapsed === true,
      reducedMotion: parsed.reducedMotion === true,
    };
  } catch {
    return { ...PREFERENCE_DEFAULTS };
  }
}

export function readPreferences(): Preferences {
  return parsePreferences(readRaw());
}

export function writePreferences(update: Partial<Preferences>): Preferences {
  const next = { ...readPreferences(), ...update };
  try {
    window.localStorage.setItem(PREFERENCES_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage can be unavailable (private mode). The in-memory value still applies.
  }
  window.dispatchEvent(new Event(PREFERENCES_EVENT));
  return next;
}

function subscribe(callback: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === PREFERENCES_STORAGE_KEY) callback();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(PREFERENCES_EVENT, callback);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(PREFERENCES_EVENT, callback);
  };
}

const SERVER_RAW = "";

/** Read every preference and get a setter for partial updates. */
export function usePreferences(): [Preferences, (update: Partial<Preferences>) => void] {
  const raw = useSyncExternalStore(subscribe, readRaw, () => SERVER_RAW);
  const preferences = parsePreferences(raw);
  const update = useCallback((patch: Partial<Preferences>) => {
    writePreferences(patch);
  }, []);
  return [preferences, update];
}

/** Remove every browser-local record this app writes. */
export function clearLocalData(): void {
  for (const key of Object.values(LOCAL_DATA_KEYS)) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // ignore
    }
  }
  window.dispatchEvent(new Event(PREFERENCES_EVENT));
  window.dispatchEvent(new Event("bmas-task-views-changed"));
  window.dispatchEvent(new Event("bmas-pins-changed"));
}
