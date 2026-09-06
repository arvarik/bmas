/**
 * The TypeScript implementations of `bmas-transform` and
 * `bmas-analysis-rng` reproduce the published daemon fixtures byte for
 * byte, so every supported implementation has one real second
 * consumer.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  Fraction,
  RNG_ALGORITHM,
  RNG_IMPLEMENTATIONS,
  SUPPORTED_ALGORITHM_VERSIONS,
  draw,
  familyAggregate,
  rejectionLimit,
  replicateDraws,
  replicateSignFlips,
} from "@/lib/analysis-rng";
import {
  KEYED_DIGEST_ALGORITHM,
  exactTextDigestHex,
  hexToBytes,
  keyedDigestHex,
  semanticText,
} from "@/lib/keyed-digest";
import {
  PROFILE_NAME,
  PROFILE_VERSION,
  TransformProfileError,
  applyRecipe,
  caseDigest,
  rankBytes,
  renderNumber,
  strictParse,
  type Case,
} from "@/lib/transform-profile";

const fixtureDirectory = join(__dirname, "..", "..", "..", "daemon", "tests", "fixtures");

function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(join(fixtureDirectory, name), "utf8")) as T;
}

interface TransformFixture {
  profile: string;
  profile_version: number;
  numbers: Array<{ value: number; expected: string }>;
  cases: Case[];
  case_digests: string[];
  rank_vectors: Array<{ case_id: string; counter: number; operation_index: number; rank: string; seed: number }>;
  sample: { seed: number; count: number; selected_case_ids: string[] };
  split: { seed: number; weights: Record<string, number>; assignment: Record<string, string> };
}

interface RngVector {
  seed: string;
  replicate_index: number;
  family_id: string;
  draw_index: number;
  case_count: number;
  limit: string;
  candidates: string[];
  rejections: number;
  index: number;
}

interface RngFixture {
  algorithm: string;
  input_digest: string;
  versions: Array<{
    algorithm_version: number;
    implementation: string;
    rng_vectors: RngVector[];
    sign_flip_vectors: Array<{ seed: string; replicate_index: number; case_count: number; flips: boolean[] }>;
    weighted_bootstrap_oracle: {
      seed: string;
      replicates: number;
      family_weights: Record<string, string>;
      cases: Record<string, { weights: Record<string, number>; slots: Record<string, Array<[number | null, number | null]>> }>;
      reduced_case_deltas: Record<string, Record<string, { delta: string; usable_slots: number }>>;
      point_family_aggregates: Record<string, string>;
      point_estimate: string;
      replicate_records: Array<{
        replicate_index: number;
        draws: Record<string, string[]>;
        family_aggregates: Record<string, string>;
        combined: string;
      }>;
    };
  }>;
}

const transform = fixture<TransformFixture>("transform_profile.json");
const rng = fixture<RngFixture>("analysis_rng.json");
const INPUT_DIGEST = Buffer.from(rng.input_digest, "hex");

function recipe(operations: Array<{ operation: string; parameters: Record<string, unknown> }>, seed: number) {
  return { profile: PROFILE_NAME, profile_version: PROFILE_VERSION, seed, operations } as Parameters<typeof applyRecipe>[1];
}

describe("bmas-transform in TypeScript", () => {
  it("pins the profile identity", () => {
    expect(transform.profile).toBe(PROFILE_NAME);
    expect(transform.profile_version).toBe(PROFILE_VERSION);
  });

  it("renders every published number vector", () => {
    for (const vector of transform.numbers) {
      expect(renderNumber(vector.value)).toBe(vector.expected);
    }
  });

  it("reproduces every case digest", () => {
    expect(transform.cases.map((item) => caseDigest(item))).toEqual(transform.case_digests);
  });

  it("reproduces every rank vector", () => {
    for (const vector of transform.rank_vectors) {
      const item = transform.cases.find((candidate) => candidate.case_id === vector.case_id);
      expect(item).toBeDefined();
      const rank = rankBytes({
        seed: BigInt(vector.seed),
        operationIndex: vector.operation_index,
        caseDigestValue: Buffer.from(caseDigest(item as Case), "hex"),
        counter: vector.counter,
      });
      expect(rank.toString("hex")).toBe(vector.rank);
    }
  });

  it("selects the same sample without replacement", () => {
    const plan = transform.sample;
    const outcome = applyRecipe(transform.cases, recipe([{ operation: "sample", parameters: { count: plan.count } }], plan.seed));
    expect(outcome.cases.map((item) => item.case_id)).toEqual(plan.selected_case_ids);
  });

  it("assigns the same split", () => {
    const plan = transform.split;
    const outcome = applyRecipe(
      transform.cases,
      recipe(
        [
          { operation: "normalize", parameters: { fields: [] } },
          { operation: "split", parameters: { weights: plan.weights } },
        ],
        plan.seed,
      ),
    );
    const assigned: Record<string, string> = {};
    for (const item of outcome.cases) assigned[String(item.case_id)] = String(item.split);
    expect(assigned).toEqual(plan.assignment);
  });

  it("rejects duplicate keys and invalid UTF-8 before construction", () => {
    expect(() => strictParse(Buffer.from('{"a": 1, "a": 2}', "utf8"))).toThrow(TransformProfileError);
    expect(() => strictParse(Buffer.from([0x7b, 0x22, 0x61, 0x22, 0x3a, 0x22, 0xff, 0x22, 0x7d]))).toThrow(
      TransformProfileError,
    );
    expect(strictParse(Buffer.from('{"b": [1, 2.5, "x"], "a": null}', "utf8"))).toEqual({ b: [1, 2.5, "x"], a: null });
  });
});

describe("bmas-analysis-rng in TypeScript", () => {
  it("pins the algorithm identity and every implementation name", () => {
    expect(rng.algorithm).toBe(RNG_ALGORITHM);
    expect(rng.versions.map((entry) => entry.algorithm_version)).toEqual([...SUPPORTED_ALGORITHM_VERSIONS]);
    for (const entry of rng.versions) {
      expect(entry.implementation).toBe(RNG_IMPLEMENTATIONS[entry.algorithm_version]);
    }
  });

  for (const entry of rng.versions) {
    const version = entry.algorithm_version;

    it(`reproduces every candidate vector for version ${version}`, () => {
      for (const vector of entry.rng_vectors) {
        const result = draw({
          masterSeed: BigInt(vector.seed),
          inputDigest: INPUT_DIGEST,
          algorithmVersion: version,
          familyId: vector.family_id,
          replicateIndex: vector.replicate_index,
          drawIndex: vector.draw_index,
          caseCount: vector.case_count,
        });
        expect(result.candidates.map(String)).toEqual(vector.candidates);
        expect(result.index).toBe(vector.index);
        expect(result.rejections).toBe(vector.rejections);
        expect(String(rejectionLimit(vector.case_count, version))).toBe(vector.limit);
      }
    });

    it(`reproduces every sign-flip vector for version ${version}`, () => {
      for (const vector of entry.sign_flip_vectors) {
        const flips = replicateSignFlips({
          masterSeed: BigInt(vector.seed),
          inputDigest: INPUT_DIGEST,
          algorithmVersion: version,
          replicateIndex: vector.replicate_index,
          caseCount: vector.case_count,
        });
        expect(flips).toEqual(vector.flips);
      }
    });

    it(`reproduces every oracle draw and aggregate for version ${version}`, () => {
      const oracle = entry.weighted_bootstrap_oracle;
      const families = Object.keys(oracle.cases).sort();
      const familyWeights: Record<string, Fraction> = {};
      let totalFamilyWeight = new Fraction(BigInt(0));
      for (const family of families) {
        familyWeights[family] = Fraction.of(oracle.family_weights[family]);
        totalFamilyWeight = totalFamilyWeight.add(familyWeights[family]);
      }
      const reduced: Record<string, Record<string, Fraction>> = {};
      for (const family of families) {
        reduced[family] = {};
        for (const [caseId, slots] of Object.entries(oracle.cases[family].slots)) {
          let delta = new Fraction(BigInt(0));
          let usable = 0;
          for (const [left, right] of slots) {
            if (left === null || right === null) continue;
            delta = delta.add(Fraction.of(right - left));
            usable += 1;
          }
          reduced[family][caseId] = delta.divide(Fraction.of(usable));
          expect(reduced[family][caseId].toString()).toBe(oracle.reduced_case_deltas[family][caseId].delta);
        }
      }
      let point = new Fraction(BigInt(0));
      for (const family of families) {
        const members = Object.keys(reduced[family])
          .sort()
          .map((caseId) => ({ weight: Fraction.of(oracle.cases[family].weights[caseId]), delta: reduced[family][caseId] }));
        const aggregate = familyAggregate(members);
        expect(aggregate.toString()).toBe(oracle.point_family_aggregates[family]);
        point = point.add(familyWeights[family].multiply(aggregate));
      }
      expect(point.divide(totalFamilyWeight).toString()).toBe(oracle.point_estimate);

      for (const record of oracle.replicate_records) {
        let combined = new Fraction(BigInt(0));
        for (const family of families) {
          const caseIds = Object.keys(reduced[family]).sort();
          const indexes = replicateDraws({
            masterSeed: BigInt(oracle.seed),
            inputDigest: INPUT_DIGEST,
            algorithmVersion: version,
            familyId: family,
            replicateIndex: record.replicate_index,
            caseCount: caseIds.length,
          });
          const drawn = indexes.map((index) => caseIds[index]);
          expect(drawn).toEqual(record.draws[family]);
          const aggregate = familyAggregate(
            drawn.map((caseId) => ({ weight: Fraction.of(oracle.cases[family].weights[caseId]), delta: reduced[family][caseId] })),
          );
          expect(aggregate.toString()).toBe(record.family_aggregates[family]);
          combined = combined.add(familyWeights[family].multiply(aggregate));
        }
        expect(combined.divide(totalFamilyWeight).toString()).toBe(record.combined);
      }
    });
  }
});

interface KeyedDigestFixture {
  metadata: {
    digest_profile_version: string;
    keyed_algorithm: string;
    key_hex: string;
    key_id: string;
    tenant_id: string;
  };
  semantic_text: Array<{ name: string; input: string; semantic_text: string }>;
  exact_bytes: Array<{ name: string; domain: string; input: string; sha256: string }>;
  keyed: Array<{ name: string; domain: string; value: string; key_id: string; hmac_sha256: string }>;
}

describe("keyed digest fixtures", () => {
  const keyed = fixture<KeyedDigestFixture>("keyed_digest.json");
  const key = hexToBytes(keyed.metadata.key_hex);

  it("matches the frozen algorithm", () => {
    expect(keyed.metadata.keyed_algorithm).toBe(KEYED_DIGEST_ALGORITHM);
    expect(keyed.semantic_text.length).toBeGreaterThanOrEqual(6);
    expect(keyed.keyed.length).toBeGreaterThanOrEqual(4);
  });

  for (const vector of keyed.semantic_text) {
    it(`transforms semantic text ${vector.name}`, () => {
      expect(semanticText(vector.input)).toBe(vector.semantic_text);
    });
  }

  for (const vector of keyed.exact_bytes) {
    it(`digests exact bytes ${vector.name}`, () => {
      expect(exactTextDigestHex(vector.domain, vector.input)).toBe(vector.sha256);
    });
  }

  for (const vector of keyed.keyed) {
    it(`reproduces the keyed digest ${vector.name}`, () => {
      expect(keyedDigestHex(key, vector.domain, vector.value)).toBe(vector.hmac_sha256);
    });
  }

  it("separates exact digests that semantic text joins", () => {
    const nfd = keyed.exact_bytes.find((vector) => vector.name === "nfd-differs-from-nfc");
    const nfc = keyed.exact_bytes.find((vector) => vector.name === "nfc-differs-from-nfd");
    expect(nfd && nfc && nfd.sha256 !== nfc.sha256).toBe(true);
    expect(semanticText(nfd!.input)).toBe(semanticText(nfc!.input));
  });
});
