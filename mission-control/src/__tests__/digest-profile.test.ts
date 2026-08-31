/**
 * Foundation Stage 0C: the TypeScript bmas-digest implementation
 * reproduces the frozen cross-language fixture vectors byte for byte.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  DIGEST_PROFILE_VERSION,
  DigestInputError,
  canonicalize,
  digestInputBytes,
} from "@/lib/digest-profile";

interface FixtureVector {
  name: string;
  domain: string;
  input: unknown;
  canonical: string;
  sha256: string;
}

const fixturePath = join(
  __dirname,
  "..",
  "..",
  "..",
  "conformance",
  "digest_profile",
  "fixtures.json",
);
const fixture = JSON.parse(readFileSync(fixturePath, "ascii")) as {
  metadata: { digest_profile_version: string };
  vectors: FixtureVector[];
};

function sha256Hex(payload: Uint8Array): string {
  return createHash("sha256").update(payload).digest("hex");
}

describe("cross-language digest fixtures", () => {
  it("matches the frozen profile version", () => {
    expect(fixture.metadata.digest_profile_version).toBe(
      DIGEST_PROFILE_VERSION,
    );
    expect(fixture.vectors.length).toBeGreaterThanOrEqual(10);
  });

  for (const vector of fixture.vectors) {
    it(`reproduces ${vector.name}`, () => {
      expect(canonicalize(vector.input)).toBe(vector.canonical);
      const framed = digestInputBytes(vector.domain, vector.input);
      expect(sha256Hex(framed)).toBe(vector.sha256);
    });
  }
});

describe("profile rejections", () => {
  it("rejects non-integer numbers", () => {
    expect(() => canonicalize(1.5)).toThrow(DigestInputError);
    expect(() => canonicalize(Number.MAX_SAFE_INTEGER + 2)).toThrow(
      DigestInputError,
    );
    expect(() => canonicalize(Number.NaN)).toThrow(DigestInputError);
    expect(() => canonicalize(Number.POSITIVE_INFINITY)).toThrow(
      DigestInputError,
    );
  });

  it("rejects invalid Unicode", () => {
    expect(() => canonicalize("\ud800")).toThrow(DigestInputError);
    expect(() => canonicalize({ "\udfff": 1 })).toThrow(DigestInputError);
  });

  it("rejects unsupported values", () => {
    expect(() => canonicalize(undefined)).toThrow(DigestInputError);
    expect(() => canonicalize(new Date(0))).toThrow(DigestInputError);
    expect(() => digestInputBytes("Invalid Domain", {})).toThrow(
      DigestInputError,
    );
  });

  it("never normalizes Unicode", () => {
    const decomposed = "é";
    const precomposed = "é";
    expect(canonicalize(decomposed)).not.toBe(canonicalize(precomposed));
  });
});
