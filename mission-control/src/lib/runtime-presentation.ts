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
