/**
 * The portable `bmas-transform` profile in TypeScript.
 *
 * This is the second supported implementation of the profile. It
 * reproduces the daemon fixtures byte for byte: strict UTF-8 JSON with
 * duplicate keys rejected, Unicode NFC for transformation keys and
 * generated strings, the stable default order over normalized case
 * identifiers and source ordinals, RFC 8785 number rendering, SHA-256
 * counter ranking for sampling and split assignment, and RFC 8785
 * canonicalization of recipe inputs before digest calculation.
 */
import { createHash } from "node:crypto";

export const PROFILE_NAME = "bmas-transform";
export const PROFILE_VERSION = 1;
export const ENGINE_VERSION = "1";
export const SAFE_INTEGER_LIMIT = Number.MAX_SAFE_INTEGER;
const SEED_LIMIT = BigInt(2) ** BigInt(64) - BigInt(1);

export const OPERATIONS = [
  "select",
  "rename",
  "filter",
  "map_template",
  "normalize",
  "deduplicate",
  "sample",
  "split",
  "attach_rubric",
] as const;

const FILTER_OPERATORS = ["eq", "ne", "contains", "exists", "absent"];
const NORMALIZE_FORMS = ["nfc", "trim", "collapse_whitespace", "lower"];

export class TransformProfileError extends Error {}

/** The one sentinel that keeps a missing value distinct from null. */
export const MISSING: unique symbol = Symbol("MISSING");
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type Case = { [key: string]: JsonValue };
type Resolved = JsonValue | typeof MISSING;

export function nfc(text: string): string {
  return text.normalize("NFC");
}

// ── Strict parsing ─────────────────────────────────────────────────

class StrictParser {
  private position = 0;

  constructor(private readonly text: string) {}

  parse(): JsonValue {
    this.skipWhitespace();
    const value = this.value();
    this.skipWhitespace();
    if (this.position !== this.text.length) {
      throw new TransformProfileError("The input is not valid JSON: trailing content");
    }
    return value;
  }

  private fail(message: string): never {
    throw new TransformProfileError(`The input is not valid JSON: ${message} at ${this.position}`);
  }

  private skipWhitespace(): void {
    while (this.position < this.text.length && " \t\n\r".includes(this.text[this.position])) {
      this.position += 1;
    }
  }

  private value(): JsonValue {
    const character = this.text[this.position];
    if (character === "{") return this.object();
    if (character === "[") return this.array();
    if (character === '"') return this.string();
    if (this.text.startsWith("true", this.position)) {
      this.position += 4;
      return true;
    }
    if (this.text.startsWith("false", this.position)) {
      this.position += 5;
      return false;
    }
    if (this.text.startsWith("null", this.position)) {
      this.position += 4;
      return null;
    }
    return this.number();
  }

  private object(): JsonValue {
    this.position += 1;
    const result: { [key: string]: JsonValue } = {};
    this.skipWhitespace();
    if (this.text[this.position] === "}") {
      this.position += 1;
      return result;
    }
    for (;;) {
      this.skipWhitespace();
      if (this.text[this.position] !== '"') this.fail("expected a string key");
      const key = this.string();
      if (Object.prototype.hasOwnProperty.call(result, key)) {
        throw new TransformProfileError(`Duplicate object key before construction: ${JSON.stringify(key)}`);
      }
      this.skipWhitespace();
      if (this.text[this.position] !== ":") this.fail("expected ':'");
      this.position += 1;
      this.skipWhitespace();
      result[key] = this.value();
      this.skipWhitespace();
      if (this.text[this.position] === ",") {
        this.position += 1;
        continue;
      }
      if (this.text[this.position] === "}") {
        this.position += 1;
        return result;
      }
      this.fail("expected ',' or '}'");
    }
  }

  private array(): JsonValue {
    this.position += 1;
    const result: JsonValue[] = [];
    this.skipWhitespace();
    if (this.text[this.position] === "]") {
      this.position += 1;
      return result;
    }
    for (;;) {
      this.skipWhitespace();
      result.push(this.value());
      this.skipWhitespace();
      if (this.text[this.position] === ",") {
        this.position += 1;
        continue;
      }
      if (this.text[this.position] === "]") {
        this.position += 1;
        return result;
      }
      this.fail("expected ',' or ']'");
    }
  }

  private string(): string {
    const start = this.position;
    this.position += 1;
    while (this.position < this.text.length) {
      const character = this.text[this.position];
      if (character === "\\") {
        this.position += 2;
        continue;
      }
      if (character === '"') {
        this.position += 1;
        try {
          return JSON.parse(this.text.slice(start, this.position)) as string;
        } catch {
          this.fail("invalid string escape");
        }
      }
      this.position += 1;
    }
    return this.fail("unterminated string");
  }

  private number(): JsonValue {
    const match = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(this.text.slice(this.position));
    if (match === null) this.fail("unexpected token");
    this.position += match[0].length;
    const literal = match[0];
    if (!/[.eE]/.test(literal) && Math.abs(Number(literal)) > SAFE_INTEGER_LIMIT) {
      throw new TransformProfileError(
        "Integers stay inside the safe range; represent higher-precision numeric content as strings",
      );
    }
    return Number(literal);
  }
}

/** Parse strict UTF-8 JSON and reject duplicate object keys. */
export function strictParse(payload: Buffer): JsonValue {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(payload);
  } catch (error) {
    throw new TransformProfileError(`The input is not strict UTF-8: ${String(error)}`);
  }
  return new StrictParser(text).parse();
}

// ── Numbers and canonical JSON ─────────────────────────────────────

function validatedNumber(value: number): number {
  if (!Number.isFinite(value)) {
    throw new TransformProfileError("Only finite binary64 numbers are representable");
  }
  return value === 0 ? 0 : value;
}

/** Render one number with RFC 8785 rules: the ECMAScript algorithm. */
export function renderNumber(value: number): string {
  return String(validatedNumber(value));
}

function canonicalString(text: string): string {
  const encoded: string[] = ['"'];
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0;
    if (character === '"') encoded.push('\\"');
    else if (character === "\\") encoded.push("\\\\");
    else if (code < 0x20) {
      const named: Record<number, string> = { 8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r" };
      encoded.push(named[code] ?? `\\u${code.toString(16).padStart(4, "0")}`);
    } else encoded.push(character);
  }
  encoded.push('"');
  return encoded.join("");
}

function compareCodeUnits(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/** Serialize one value with the profile's RFC 8785 rules. */
export function canonicalJson(value: Resolved): string {
  if (value === MISSING) {
    throw new TransformProfileError("A missing value never serializes; it is not JSON null");
  }
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return renderNumber(value);
  if (typeof value === "string") return canonicalString(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  const entries = Object.entries(value).sort((a, b) => compareCodeUnits(a[0], b[0]));
  return `{${entries.map(([key, item]) => `${canonicalString(key)}:${canonicalJson(item)}`).join(",")}}`;
}

function sha256(payload: Buffer): Buffer {
  return createHash("sha256").update(payload).digest();
}

export function caseDigestBytes(item: Case): Buffer {
  return sha256(Buffer.from(canonicalJson(item), "utf8"));
}

export function caseDigest(item: Case): string {
  return caseDigestBytes(item).toString("hex");
}

export function recipeDigest(recipe: JsonValue): string {
  return sha256(Buffer.from(canonicalJson(recipe), "utf8")).toString("hex");
}

export function datasetDigest(cases: Case[]): string {
  return sha256(Buffer.concat(cases.map((item) => caseDigestBytes(item)))).toString("hex");
}

export interface RankInput {
  seed: bigint;
  operationIndex: number;
  caseDigestValue: Buffer;
  counter: number;
  profileVersion?: number;
}

/** Compute one SHA-256 counter rank with the exact pinned input. */
export function rankBytes(input: RankInput): Buffer {
  if (input.seed < BigInt(0) || input.seed > SEED_LIMIT) {
    throw new TransformProfileError("The seed fits one unsigned 64-bit integer");
  }
  if (input.caseDigestValue.length !== 32) {
    throw new TransformProfileError("A case digest holds 32 bytes");
  }
  const version = Buffer.alloc(4);
  version.writeUInt32BE(input.profileVersion ?? PROFILE_VERSION, 0);
  const seed = Buffer.alloc(8);
  seed.writeBigUInt64BE(input.seed, 0);
  const operation = Buffer.alloc(4);
  operation.writeUInt32BE(input.operationIndex, 0);
  const counter = Buffer.alloc(4);
  counter.writeUInt32BE(input.counter, 0);
  return sha256(
    Buffer.concat([
      Buffer.from(PROFILE_NAME, "utf8"),
      Buffer.from([0]),
      version,
      seed,
      operation,
      input.caseDigestValue,
      counter,
    ]),
  );
}

// ── Pointers and templates ─────────────────────────────────────────

export function resolvePointer(value: JsonValue, pointer: string): Resolved {
  if (pointer === "") return value;
  if (!pointer.startsWith("/")) {
    throw new TransformProfileError(`A binding pointer starts with '/': ${JSON.stringify(pointer)}`);
  }
  let current: JsonValue = value;
  for (const rawToken of pointer.split("/").slice(1)) {
    const token = rawToken.replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(current)) {
      if (!/^\d+$/.test(token) || Number(token) >= current.length) return MISSING;
      current = current[Number(token)];
    } else if (current !== null && typeof current === "object") {
      if (!Object.prototype.hasOwnProperty.call(current, token)) return MISSING;
      current = current[token];
    } else {
      return MISSING;
    }
  }
  return current;
}

const TEMPLATE_PATTERN = /\$\$\{|\$\{([A-Za-z0-9_-]+)\}/g;

export function renderTemplate(
  template: string,
  bindings: Record<string, { pointer?: string; default?: JsonValue }>,
  item: Case,
): string {
  const output: string[] = [];
  let position = 0;
  for (const match of template.matchAll(TEMPLATE_PATTERN)) {
    output.push(template.slice(position, match.index));
    position = (match.index ?? 0) + match[0].length;
    if (match[0] === "$${") {
      output.push("${");
      continue;
    }
    const name = match[1];
    const binding = bindings[name];
    if (binding === undefined) {
      throw new TransformProfileError(`The template references an unbound name: ${JSON.stringify(name)}`);
    }
    let value = resolvePointer(item, binding.pointer ?? "");
    if (value === MISSING) {
      if (!("default" in binding)) {
        throw new TransformProfileError(`The binding ${JSON.stringify(name)} resolved no value and supplies no default`);
      }
      value = binding.default as JsonValue;
    }
    if (typeof value === "string") output.push(nfc(value));
    else if (value === null) output.push("null");
    else if (typeof value === "boolean") output.push(value ? "true" : "false");
    else if (typeof value === "number") output.push(renderNumber(value));
    else output.push(canonicalJson(value));
  }
  output.push(template.slice(position));
  return nfc(output.join(""));
}

// ── Operations ─────────────────────────────────────────────────────

type Parameters = { [key: string]: JsonValue };

function stableKey(ordinal: number, item: Case): [Buffer, number] {
  const identity = nfc(String(item.case_id ?? item.item_key ?? ""));
  return [Buffer.from(identity, "utf8"), ordinal];
}

function fieldValue(item: Case, field: string): Resolved {
  if (field.startsWith("/")) return resolvePointer(item, field);
  const key = nfc(field);
  return Object.prototype.hasOwnProperty.call(item, key) ? item[key] : MISSING;
}

function normalizedComparable(value: Resolved): string {
  if (value === MISSING) return "m:missing";
  if (typeof value === "string") return `s:${nfc(value)}`;
  return `j:${canonicalJson(value)}`;
}

function stringList(value: JsonValue | undefined): string[] {
  return Array.isArray(value) ? value.map((name) => nfc(String(name))) : [];
}

function applySelect(cases: Case[], parameters: Parameters): Case[] {
  const fields = stringList(parameters.fields);
  if (fields.length === 0) throw new TransformProfileError("select names at least one field");
  return cases.map((item) => {
    const result: Case = {};
    for (const name of fields) if (name in item) result[name] = item[name];
    return result;
  });
}

function applyRename(cases: Case[], parameters: Parameters): Case[] {
  const raw = parameters.mapping;
  const mapping = new Map<string, string>();
  if (raw !== null && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [source, target] of Object.entries(raw)) mapping.set(nfc(source), nfc(String(target)));
  }
  if (mapping.size === 0) throw new TransformProfileError("rename names at least one mapping");
  return cases.map((item) => {
    const result: Case = {};
    for (const [key, value] of Object.entries(item)) result[mapping.get(nfc(key)) ?? nfc(key)] = value;
    return result;
  });
}

function applyFilter(cases: Case[], parameters: Parameters): Case[] {
  const field = String(parameters.field ?? "");
  const operator = String(parameters.operator ?? "");
  if (!FILTER_OPERATORS.includes(operator)) {
    throw new TransformProfileError(`Unknown filter operator: ${JSON.stringify(operator)}`);
  }
  const expected = parameters.value ?? null;
  return cases.filter((item) => {
    const value = fieldValue(item, field);
    if (operator === "exists") return value !== MISSING;
    if (operator === "absent") return value === MISSING;
    if (value === MISSING) return false;
    if (operator === "eq") return normalizedComparable(value) === normalizedComparable(expected);
    if (operator === "ne") return normalizedComparable(value) !== normalizedComparable(expected);
    return typeof value === "string" && typeof expected === "string" && nfc(value).includes(nfc(expected));
  });
}

function applyMapTemplate(cases: Case[], parameters: Parameters): Case[] {
  const target = nfc(String(parameters.target ?? ""));
  const template = String(parameters.template ?? "");
  const bindings = (parameters.bindings ?? {}) as Record<string, { pointer?: string; default?: JsonValue }>;
  if (!target) throw new TransformProfileError("map_template names its target field");
  return cases.map((item) => ({ ...item, [target]: renderTemplate(template, bindings, item) }));
}

function applyNormalize(cases: Case[], parameters: Parameters): Case[] {
  const fields = stringList(parameters.fields);
  const forms = Array.isArray(parameters.forms) ? parameters.forms.map(String) : ["nfc"];
  for (const form of forms) {
    if (!NORMALIZE_FORMS.includes(form)) throw new TransformProfileError(`Unknown normalize form: ${JSON.stringify(form)}`);
  }
  return cases.map((item) => {
    const result: Case = { ...item };
    for (const name of fields) {
      let value = result[name];
      if (typeof value !== "string") continue;
      for (const form of forms) {
        if (form === "nfc") value = nfc(value);
        else if (form === "trim") value = value.trim();
        else if (form === "collapse_whitespace") value = value.split(/\s+/).filter((piece) => piece.length > 0).join(" ");
        else if (form === "lower") value = value.toLowerCase();
      }
      result[name] = value;
    }
    return result;
  });
}

function applyDeduplicate(cases: Case[], parameters: Parameters): Case[] {
  const fields = stringList(parameters.fields);
  if (fields.length === 0) throw new TransformProfileError("deduplicate names at least one field");
  const seen = new Set<string>();
  return cases.filter((item) => {
    const key = canonicalJson(fields.map((name) => normalizedComparable(fieldValue(item, name))));
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function compareBuffers(left: Buffer, right: Buffer): number {
  return Buffer.compare(left, right);
}

function ranked(cases: Case[], seed: bigint, operationIndex: number): Array<{ rank: Buffer; key: [Buffer, number]; item: Case }> {
  return cases.map((item, ordinal) => ({
    rank: rankBytes({ seed, operationIndex, caseDigestValue: caseDigestBytes(item), counter: 0 }),
    key: stableKey(ordinal, item),
    item,
  }));
}

function applySample(cases: Case[], parameters: Parameters, seed: bigint, operationIndex: number): Case[] {
  const count = parameters.count;
  if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
    throw new TransformProfileError("sample requires one nonnegative integer count");
  }
  const chosen = ranked(cases, seed, operationIndex).sort((a, b) => {
    const byRank = compareBuffers(a.rank, b.rank);
    if (byRank !== 0) return byRank;
    const byIdentity = compareBuffers(a.key[0], b.key[0]);
    if (byIdentity !== 0) return byIdentity;
    return a.key[1] - b.key[1];
  });
  const selected = new Set(chosen.slice(0, count).map((entry) => entry.item));
  return cases.filter((item) => selected.has(item));
}

function applySplit(cases: Case[], parameters: Parameters, seed: bigint, operationIndex: number): Case[] {
  const rawWeights = parameters.weights;
  const target = nfc(String(parameters.target ?? "split"));
  const names: string[] = [];
  const values: bigint[] = [];
  if (rawWeights !== null && typeof rawWeights === "object" && !Array.isArray(rawWeights)) {
    for (const name of Object.keys(rawWeights).sort(compareCodeUnits)) {
      const weight = rawWeights[name];
      if (typeof weight !== "number" || !Number.isInteger(weight) || weight <= 0) {
        throw new TransformProfileError("split weights are positive integers");
      }
      names.push(nfc(name));
      values.push(BigInt(weight));
    }
  }
  if (names.length === 0) throw new TransformProfileError("split names at least one weight");
  const total = values.reduce((sum, weight) => sum + weight, BigInt(0));
  const boundaries: Array<[bigint, string]> = [];
  let running = BigInt(0);
  names.forEach((name, index) => {
    running += values[index];
    boundaries.push([running, name]);
  });
  return cases.map((item) => {
    const rank = rankBytes({ seed, operationIndex, caseDigestValue: caseDigestBytes(item), counter: 0 });
    const remainder = BigInt(`0x${rank.toString("hex")}`) % total;
    const assigned = boundaries.find(([boundary]) => remainder < boundary)?.[1] ?? names[names.length - 1];
    return { ...item, [target]: assigned };
  });
}

function applyAttachRubric(cases: Case[], parameters: Parameters): Case[] {
  const rubricId = String(parameters.rubric_id ?? "");
  if (!rubricId) throw new TransformProfileError("attach_rubric names one rubric");
  return cases.map((item) => ({ ...item, rubric_id: rubricId }));
}

export interface Recipe {
  profile: string;
  profile_version: number;
  seed?: number | string;
  operations: Array<{ operation: string; parameters?: Parameters }>;
}

export function validateRecipe(recipe: Recipe): Recipe {
  if (recipe === null || typeof recipe !== "object") throw new TransformProfileError("A recipe is one JSON object");
  if (recipe.profile !== PROFILE_NAME) throw new TransformProfileError(`The recipe declares the ${PROFILE_NAME} profile`);
  if (recipe.profile_version !== PROFILE_VERSION) {
    throw new TransformProfileError("The recipe metadata pins the supported profile version");
  }
  const seed = BigInt(recipe.seed ?? 0);
  if (seed < BigInt(0) || seed > SEED_LIMIT) throw new TransformProfileError("The seed fits one unsigned 64-bit integer");
  if (!Array.isArray(recipe.operations) || recipe.operations.length === 0) {
    throw new TransformProfileError("A recipe lists at least one operation");
  }
  for (const operation of recipe.operations) {
    if (!(OPERATIONS as readonly string[]).includes(operation.operation)) {
      throw new TransformProfileError(`Unknown operation: ${JSON.stringify(operation)}`);
    }
  }
  return recipe;
}

export interface RecipeOutcome {
  cases: Case[];
  case_digests: string[];
  dataset_digest: string;
  recipe_digest: string;
  engine: { profile: string; profile_version: number; engine_version: string };
}

/** Apply one recipe deterministically and report every digest. */
export function applyRecipe(cases: Case[], recipe: Recipe): RecipeOutcome {
  validateRecipe(recipe);
  const seed = BigInt(recipe.seed ?? 0);
  let current = cases.map((item) => ({ ...item }));
  recipe.operations.forEach((operation, index) => {
    const parameters = (operation.parameters ?? {}) as Parameters;
    switch (operation.operation) {
      case "select":
        current = applySelect(current, parameters);
        break;
      case "rename":
        current = applyRename(current, parameters);
        break;
      case "filter":
        current = applyFilter(current, parameters);
        break;
      case "map_template":
        current = applyMapTemplate(current, parameters);
        break;
      case "normalize":
        current = applyNormalize(current, parameters);
        break;
      case "deduplicate":
        current = applyDeduplicate(current, parameters);
        break;
      case "sample":
        current = applySample(current, parameters, seed, index);
        break;
      case "split":
        current = applySplit(current, parameters, seed, index);
        break;
      case "attach_rubric":
        current = applyAttachRubric(current, parameters);
        break;
      default:
        throw new TransformProfileError(`Unknown operation: ${operation.operation}`);
    }
  });
  return {
    cases: current,
    case_digests: current.map((item) => caseDigest(item)),
    dataset_digest: datasetDigest(current),
    recipe_digest: recipeDigest(recipe as unknown as JsonValue),
    engine: { profile: PROFILE_NAME, profile_version: PROFILE_VERSION, engine_version: ENGINE_VERSION },
  };
}
