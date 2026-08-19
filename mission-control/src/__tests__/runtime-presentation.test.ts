import { describe, expect, it } from "vitest";
import type { VariantCapability } from "@/lib/capabilities";
import { describeRuntime } from "@/lib/runtime-presentation";

describe("runtime choice presentation", () => {
  it("uses capability counts without unsupported estimates", () => {
    const variant: VariantCapability = {
      id: "classic",
      label: "Classic blackboard",
      available: true,
      contract_version: "1",
      aliases: [],
      configuration_schema_version: "1",
      supports_recovery: true,
      required_agent_features: ["execute", "cancel"],
      features: {
        events: [],
        panels: [],
        graphs: [],
        controls: ["pause", "resume"],
        progress: [],
        result: [],
      },
    };

    const details = describeRuntime(variant);

    expect(details.speed).toContain("does not publish a speed estimate");
    expect(details.cost).toContain("does not publish a cost estimate");
    expect(details.tools).toContain("Required agent features: execute, cancel");
    expect(details.tools).toContain("Operator controls: pause, resume");
  });
});
