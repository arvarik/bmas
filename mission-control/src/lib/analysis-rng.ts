/**
 * The portable `bmas-analysis-rng` algorithm in TypeScript.
 *
 * This is the second supported implementation of the published
 * derivation. Version 1 derives every candidate through SHA-256 over
 * the exact input; version 2 derives one SHA-256 family key and then
 * mixes one counter word per draw pair under the SplitMix64
 * finalizer. Both reproduce the daemon fixtures byte for byte. The
 * algorithm version is metadata, never part of an identifier.
 */
import { createHash } from "node:crypto";

export const RNG_ALGORITHM = "bmas-analysis-rng";
export const SUPPORTED_ALGORITHM_VERSIONS = [1, 2] as const;
export const RNG_IMPLEMENTATIONS: Record<number, string> = {
  1: "sha-256-rejection",
  2: "keyed-counter-splitmix64",
};

const ZERO = BigInt(0);
const ONE = BigInt(1);
const WORD = BigInt(2) ** BigInt(64);
const HALF_WORD = BigInt(2) ** BigInt(32);
const FULL_WORD_MASK = WORD - ONE;
const HALF_WORD_MASK = HALF_WORD - ONE;
const GOLDEN_GAMMA = BigInt("0x9E3779B97F4A7C15");
const MIX_MULTIPLIER_ONE = BigInt("0xBF58476D1CE4E5B9");
const MIX_MULTIPLIER_TWO = BigInt("0x94D049BB133111EB");
const COUNTER_STEP = BigInt("0xD1B54A32D192ED03");
export const SIGN_FLIP_FAMILY = "sign-flip";

export class AnalysisRngError extends Error {}

function sha256(payload: Buffer): Buffer {
  return createHash("sha256").update(payload).digest();
}

function u32be(value: number): Buffer {
  if (!Number.isInteger(value) || value < 0 || value > 0xffffffff) {
    throw new AnalysisRngError("The value fits one unsigned 32-bit integer");
  }
  const buffer = Buffer.alloc(4);
  buffer.writeUInt32BE(value, 0);
  return buffer;
}

function u64be(value: bigint): Buffer {
  if (value < ZERO || value > FULL_WORD_MASK) {
    throw new AnalysisRngError("The master seed fits one unsigned 64-bit integer");
  }
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64BE(value, 0);
  return buffer;
}

function checkVersion(version: number): void {
  if (!(SUPPORTED_ALGORITHM_VERSIONS as readonly number[]).includes(version)) {
    throw new AnalysisRngError(`Unsupported ${RNG_ALGORITHM} algorithm version ${version}`);
  }
}

export function familyDigest(familyId: string): Buffer {
  return sha256(Buffer.from(familyId, "utf8"));
}

export function splitmixFinalizer(state: bigint): bigint {
  let z = state & FULL_WORD_MASK;
  z = ((z ^ (z >> BigInt(30))) * MIX_MULTIPLIER_ONE) & FULL_WORD_MASK;
  z = ((z ^ (z >> BigInt(27))) * MIX_MULTIPLIER_TWO) & FULL_WORD_MASK;
  return z ^ (z >> BigInt(31));
}

export function counterState(key: bigint, word: bigint, counter: number): bigint {
  return (key + word * GOLDEN_GAMMA + BigInt(counter) * COUNTER_STEP) & FULL_WORD_MASK;
}

export function bootstrapWord(replicateIndex: number, drawIndex: number): bigint {
  return (BigInt(replicateIndex) << BigInt(32)) | BigInt(Math.floor(drawIndex / 2));
}

export function signFlipWord(replicateIndex: number, caseIndex: number): bigint {
  return (BigInt(replicateIndex) << BigInt(32)) | BigInt(Math.floor(caseIndex / 64));
}

export interface DerivationInput {
  masterSeed: bigint;
  inputDigest: Buffer;
  familyIdDigest: Buffer;
  algorithmVersion?: number;
}

export function familyKey(input: DerivationInput): bigint {
  const version = input.algorithmVersion ?? 2;
  checkVersion(version);
  if (input.inputDigest.length !== 32 || input.familyIdDigest.length !== 32) {
    throw new AnalysisRngError("A digest input holds 32 bytes");
  }
  const payload = Buffer.concat([
    Buffer.from(RNG_ALGORITHM, "utf8"),
    Buffer.from([0]),
    u32be(version),
    u64be(input.masterSeed),
    input.inputDigest,
    input.familyIdDigest,
  ]);
  return sha256(payload).readBigUInt64BE(0);
}

export function candidate(
  input: DerivationInput & { replicateIndex: number; drawIndex: number; counter: number },
): bigint {
  const version = input.algorithmVersion ?? 2;
  checkVersion(version);
  if (input.inputDigest.length !== 32 || input.familyIdDigest.length !== 32) {
    throw new AnalysisRngError("A digest input holds 32 bytes");
  }
  if (version === 1) {
    const payload = Buffer.concat([
      Buffer.from(RNG_ALGORITHM, "utf8"),
      Buffer.from([0]),
      u32be(version),
      u64be(input.masterSeed),
      input.inputDigest,
      u32be(input.replicateIndex),
      input.familyIdDigest,
      u32be(input.drawIndex),
      u32be(input.counter),
    ]);
    return sha256(payload).readBigUInt64BE(0);
  }
  const key = familyKey(input);
  const mixed = splitmixFinalizer(counterState(key, bootstrapWord(input.replicateIndex, input.drawIndex), input.counter));
  return input.drawIndex % 2 === 0 ? mixed >> BigInt(32) : mixed & HALF_WORD_MASK;
}

export function candidateWidth(version: number): bigint {
  checkVersion(version);
  return version === 1 ? WORD : HALF_WORD;
}

export function rejectionLimit(caseCount: number, version: number): bigint {
  if (caseCount <= 0) {
    throw new AnalysisRngError("A draw needs at least one case");
  }
  const width = candidateWidth(version);
  return width - (width % BigInt(caseCount));
}

export interface DrawResult {
  index: number;
  candidates: bigint[];
  rejections: number;
}

export function draw(
  input: Omit<DerivationInput, "familyIdDigest"> & {
    familyId: string;
    replicateIndex: number;
    drawIndex: number;
    caseCount: number;
  },
): DrawResult {
  const version = input.algorithmVersion ?? 2;
  const limit = rejectionLimit(input.caseCount, version);
  const digest = familyDigest(input.familyId);
  const candidates: bigint[] = [];
  let counter = 0;
  for (;;) {
    const value = candidate({
      masterSeed: input.masterSeed,
      inputDigest: input.inputDigest,
      familyIdDigest: digest,
      algorithmVersion: version,
      replicateIndex: input.replicateIndex,
      drawIndex: input.drawIndex,
      counter,
    });
    candidates.push(value);
    if (value < limit) {
      return { index: Number(value % BigInt(input.caseCount)), candidates, rejections: counter };
    }
    counter += 1;
  }
}

export function replicateDraws(
  input: Omit<DerivationInput, "familyIdDigest"> & {
    familyId: string;
    replicateIndex: number;
    caseCount: number;
  },
): number[] {
  const indexes: number[] = [];
  for (let drawIndex = 0; drawIndex < input.caseCount; drawIndex += 1) {
    indexes.push(draw({ ...input, drawIndex }).index);
  }
  return indexes;
}

export function signFlip(
  input: Omit<DerivationInput, "familyIdDigest"> & { replicateIndex: number; caseIndex: number },
): boolean {
  const version = input.algorithmVersion ?? 2;
  checkVersion(version);
  if (version === 1) {
    return draw({
      ...input,
      familyId: SIGN_FLIP_FAMILY,
      drawIndex: input.caseIndex,
      caseCount: 2,
    }).index === 1;
  }
  const key = familyKey({
    masterSeed: input.masterSeed,
    inputDigest: input.inputDigest,
    familyIdDigest: familyDigest(SIGN_FLIP_FAMILY),
    algorithmVersion: version,
  });
  const mixed = splitmixFinalizer(counterState(key, signFlipWord(input.replicateIndex, input.caseIndex), 0));
  return ((mixed >> BigInt(input.caseIndex % 64)) & ONE) === ONE;
}

export function replicateSignFlips(
  input: Omit<DerivationInput, "familyIdDigest"> & { replicateIndex: number; caseCount: number },
): boolean[] {
  const flips: boolean[] = [];
  for (let caseIndex = 0; caseIndex < input.caseCount; caseIndex += 1) {
    flips.push(signFlip({ ...input, caseIndex }));
  }
  return flips;
}

// ── Exact rational arithmetic for the bootstrap oracle ─────────────

function gcd(a: bigint, b: bigint): bigint {
  let x = a < ZERO ? -a : a;
  let y = b < ZERO ? -b : b;
  while (y !== ZERO) {
    [x, y] = [y, x % y];
  }
  return x;
}

/** One exact fraction with the same text form as Python's Fraction. */
export class Fraction {
  readonly numerator: bigint;
  readonly denominator: bigint;

  constructor(numerator: bigint, denominator: bigint = ONE) {
    if (denominator === ZERO) {
      throw new AnalysisRngError("A fraction never divides by zero");
    }
    let n = numerator;
    let d = denominator;
    if (d < ZERO) {
      n = -n;
      d = -d;
    }
    const divisor = gcd(n, d) || ONE;
    this.numerator = n / divisor;
    this.denominator = d / divisor;
  }

  static of(value: number | bigint | string): Fraction {
    if (typeof value === "bigint") return new Fraction(value);
    if (typeof value === "number") {
      if (!Number.isInteger(value)) throw new AnalysisRngError("The oracle uses integers");
      return new Fraction(BigInt(value));
    }
    const [n, d] = value.split("/");
    return new Fraction(BigInt(n), d === undefined ? ONE : BigInt(d));
  }

  add(other: Fraction): Fraction {
    return new Fraction(
      this.numerator * other.denominator + other.numerator * this.denominator,
      this.denominator * other.denominator,
    );
  }

  multiply(other: Fraction): Fraction {
    return new Fraction(this.numerator * other.numerator, this.denominator * other.denominator);
  }

  divide(other: Fraction): Fraction {
    return new Fraction(this.numerator * other.denominator, this.denominator * other.numerator);
  }

  toString(): string {
    return this.denominator === ONE ? `${this.numerator}` : `${this.numerator}/${this.denominator}`;
  }
}

/** Aggregate weighted member deltas exactly, as the oracle does. */
export function familyAggregate(members: Array<{ weight: Fraction; delta: Fraction }>): Fraction {
  let numerator = new Fraction(ZERO);
  let denominator = new Fraction(ZERO);
  for (const member of members) {
    numerator = numerator.add(member.weight.multiply(member.delta));
    denominator = denominator.add(member.weight);
  }
  return numerator.divide(denominator);
}
