import type { VariantCapability } from "@/lib/capabilities";

export interface RuntimePresentation {
  speed: string;
  cost: string;
  tools: string;
}

export function describeRuntime(
  variant: VariantCapability,
): RuntimePresentation {
  const requiredFeatures = variant.required_agent_features.length > 0
    ? variant.required_agent_features.join(", ")
    : "none";
  const controls = variant.features.controls.length > 0
    ? variant.features.controls.join(", ")
    : "none";

  return {
    speed: "The daemon does not publish a speed estimate. Speed follows the selected models, active agents, and coordination turns.",
    cost: "The daemon does not publish a cost estimate. Cost follows model routing, token use, and agent turns.",
    tools: `The daemon does not publish a tool list. Required agent features: ${requiredFeatures}. Operator controls: ${controls}.`,
  };
}


// ── Stop reason ──────────────────────────────────────────────────────

export interface StopReason {
  label: string;
  tone: "verified" | "unverified" | "limit" | "cancelled";
  detail: string;
}

const LIMIT_LABELS: Record<string, string> = {
  max_rounds: "round limit",
  budget: "budget ceiling",
  duration: "time limit",
  stalled: "no further progress",
  no_available_agents: "no available agents",
};

/**
 * Explain why a finished task stopped and how trustworthy the answer is.
 * This is the feedback loop for the effort lever: the operator sees what
 * the chosen effort actually bought.
 */
export function describeStopReason(meta: {
  status?: string;
  terminal_kind?: string | null;
  terminated_by?: string;
  answer_source?: string;
}): StopReason | null {
  if (meta.status !== "completed" && meta.status !== "failed") return null;
  if (meta.terminal_kind === "cancelled") {
    return { label: "Stopped by operator", tone: "cancelled", detail: "The task was cancelled before it finished." };
  }
  const reason = meta.terminated_by ?? "";
  const source = meta.answer_source ?? "";
  if (reason === "solution" || source === "decider") {
    return {
      label: "Solution verified",
      tone: "verified",
      detail: "An independent critic reviewed and approved the answer.",
    };
  }
  const limit = LIMIT_LABELS[reason] ?? (reason ? reason.replace(/_/g, " ") : "");
  if (source === "decider_unverified") {
    return {
      label: limit ? `Stopped: ${limit} — unverified` : "Stopped unverified",
      tone: "unverified",
      detail: "The decider produced an answer, but no critic review approved it before the stop.",
    };
  }
  if (source === "sole_unverified") {
    return {
      label: limit ? `Stopped: ${limit} — fallback vote` : "Fallback vote",
      tone: "unverified",
      detail: "No solution reached the board. The answer comes from a similarity vote across the roster.",
    };
  }
  if (meta.status === "failed") {
    return { label: "Failed", tone: "limit", detail: "The task did not produce an answer." };
  }
  if (limit) {
    return { label: `Stopped: ${limit}`, tone: "limit", detail: "A resource limit ended the task." };
  }
  return null;
}
