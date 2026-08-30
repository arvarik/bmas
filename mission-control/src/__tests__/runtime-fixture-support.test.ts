/**
 * runtime-fixture-support.test.ts — Foundation Stage 0A adapter freeze.
 *
 * Mission Control must adapt exactly the runtime pairs that the golden
 * fixture `conformance/runtime_fixtures/ui-adapter-support.json`
 * records. A mismatch means the adapter identity changed without a
 * reviewed fixture update.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CLASSIC_CONTRACT_VERSIONS,
  PATCHBOARD_CONTRACT_VERSIONS,
  STIGMERGIC_CONTRACT_VERSIONS,
  hasMissionControlAdapter,
} from "@/lib/variant-support";

interface AdapterFixtureVariant {
  id: string;
  contract_versions: string[];
  panels: string[];
  graphs: string[];
  result_fields: string[];
}

const fixturePath = join(
  __dirname,
  "../../../conformance/runtime_fixtures/ui-adapter-support.json",
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8")) as {
  record: { variants: AdapterFixtureVariant[] };
};

const frozenVariants = new Map(
  fixture.record.variants.map((variant) => [variant.id, variant]),
);

describe("frozen runtime adapter identity", () => {
  it("adapts every fixture runtime and nothing else", () => {
    const supported = ["classic", "patchboard", "stigmergic"];
    expect([...frozenVariants.keys()].sort()).toEqual(supported);
    for (const id of supported) {
      expect(hasMissionControlAdapter(id)).toBe(true);
    }
    expect(hasMissionControlAdapter("surprise-runtime")).toBe(false);
  });

  it("supports exactly the frozen contract versions", () => {
    expect([...CLASSIC_CONTRACT_VERSIONS]).toEqual(
      frozenVariants.get("classic")?.contract_versions,
    );
    expect([...PATCHBOARD_CONTRACT_VERSIONS]).toEqual(
      frozenVariants.get("patchboard")?.contract_versions,
    );
    expect([...STIGMERGIC_CONTRACT_VERSIONS]).toEqual(
      frozenVariants.get("stigmergic")?.contract_versions,
    );
  });
});
