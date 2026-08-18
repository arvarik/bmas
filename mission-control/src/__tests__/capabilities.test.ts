import { describe, expect, it } from "vitest";
import {
  CapabilityContractError,
  findVariantCapability,
  parseCapabilities,
} from "@/lib/capabilities";

function variant(overrides: Record<string, unknown> = {}) {
  return {
    id: "classic",
    label: "Classic blackboard",
    available: true,
    contract_version: "1",
    configuration_schema_version: "1",
    supports_recovery: true,
    required_agent_features: ["execute"],
    aliases: ["traditional"],
    features: {
      events: ["initial_state", "phase", "complete", "error"],
      panels: ["mission"],
      graphs: ["turns"],
      controls: ["pause"],
      progress: ["phase"],
      result: ["answer"],
    },
    ...overrides,
  };
}

describe("daemon capability contract", () => {
  it("parses the complete version 1 contract", () => {
    const document = parseCapabilities({ api_version: "1", variants: [variant()] });
    expect(document.variants[0]).toMatchObject({
      id: "classic",
      configuration_schema_version: "1",
      supports_recovery: true,
      required_agent_features: ["execute"],
    });
  });

  it("preserves additive feature values and unknown fields", () => {
    const document = parseCapabilities({
      api_version: "1",
      variants: [variant({
        future_field: { enabled: true },
        features: {
          events: ["future.event"],
          panels: ["future-panel"],
          graphs: ["future-graph"],
          controls: ["future-control"],
          progress: ["future-progress"],
          result: ["future-result"],
        },
      })],
    });
    expect(document.variants[0].features.events).toEqual(["future.event"]);
  });

  it("rejects an unsupported API version", () => {
    expect(() => parseCapabilities({ api_version: "2", variants: [] }))
      .toThrowError(CapabilityContractError);
    try {
      parseCapabilities({ api_version: "2", variants: [] });
    } catch (error) {
      expect((error as CapabilityContractError).code).toBe("unsupported-api-version");
    }
  });

  it("rejects a malformed feature array", () => {
    const malformed = variant({
      features: {
        events: "phase",
        panels: [],
        graphs: [],
        controls: [],
        progress: [],
        result: [],
      },
    });
    expect(() => parseCapabilities({ api_version: "1", variants: [malformed] }))
      .toThrow(/events/);
  });

  it("rejects a duplicate feature value", () => {
    const malformed = variant({
      features: {
        events: ["phase", "phase"],
        panels: [],
        graphs: [],
        controls: [],
        progress: [],
        result: [],
      },
    });
    expect(() => parseCapabilities({ api_version: "1", variants: [malformed] }))
      .toThrow(/duplicate/);
  });

  it("rejects duplicate variant IDs", () => {
    expect(() => parseCapabilities({
      api_version: "1",
      variants: [variant(), variant({ aliases: ["legacy"] })],
    })).toThrow(/conflicts/);
  });

  it("rejects duplicate aliases in one variant", () => {
    expect(() => parseCapabilities({
      api_version: "1",
      variants: [variant({ aliases: ["traditional", "traditional"] })],
    })).toThrow(/duplicate/);
  });

  it("rejects aliases shared by two variants", () => {
    expect(() => parseCapabilities({
      api_version: "1",
      variants: [
        variant(),
        variant({ id: "other", aliases: ["traditional"] }),
      ],
    })).toThrow(/conflicts/);
  });

  it("rejects an ID that collides with an earlier alias", () => {
    expect(() => parseCapabilities({
      api_version: "1",
      variants: [
        variant(),
        variant({ id: "traditional", aliases: ["legacy"] }),
      ],
    })).toThrow(/conflicts/);
  });

  it("resolves the compatibility alias to the canonical descriptor", () => {
    const document = parseCapabilities({ api_version: "1", variants: [variant()] });
    expect(findVariantCapability(document, "traditional")?.id).toBe("classic");
  });
});
