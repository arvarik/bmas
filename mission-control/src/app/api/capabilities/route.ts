import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/capabilities
 *
 * Returns the list of coordination variants supported by the daemon.
 * Until the daemon ships its own /capabilities endpoint (Phase 6),
 * this proxy builds a static response from the daemon's /config/active.
 *
 * Shape (doc 07 §4):
 * {
 *   variants: [
 *     { id, label, available, reason? }
 *   ]
 * }
 */

interface ConfigActiveResponse {
  variant: string;
  blackboard_v2: boolean;
}

interface VariantDescriptor {
  id: string;
  label: string;
  available: boolean;
  reason?: string;
}

const KNOWN_VARIANTS: Omit<VariantDescriptor, "available" | "reason">[] = [
  { id: "traditional", label: "Blackboard (bMAS)" },
  { id: "patchboard", label: "PatchBoard" },
  { id: "stigmergic", label: "Stigmergic" },
];

export async function GET(): Promise<NextResponse> {
  try {
    const upstream = await fetch(`${DAEMON_BASE_URL}/config/active`, {
      signal: AbortSignal.timeout(3_000),
      cache: "no-store",
    });

    let activeVariant = "traditional";
    if (upstream.ok) {
      const data = (await upstream.json()) as ConfigActiveResponse;
      activeVariant = data.variant ?? "traditional";
    }

    const variants: VariantDescriptor[] = KNOWN_VARIANTS.map((v) => ({
      ...v,
      // Traditional is always available; other variants only if they are the active variant
      available: v.id === "traditional" || v.id === activeVariant,
      ...(v.id !== "traditional" && v.id !== activeVariant ? { reason: "coming soon" } : {}),
    }));

    return NextResponse.json({ variants }, {
      headers: { "Cache-Control": "public, max-age=60" },
    });
  } catch {
    // Daemon unreachable — return static default
    const variants: VariantDescriptor[] = KNOWN_VARIANTS.map((v) => ({
      ...v,
      available: v.id === "traditional",
      ...(v.id !== "traditional" ? { reason: "coming soon" } : {}),
    }));

    return NextResponse.json({ variants }, {
      headers: { "Cache-Control": "public, max-age=30" },
    });
  }
}
