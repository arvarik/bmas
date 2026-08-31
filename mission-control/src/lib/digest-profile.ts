/**
 * Foundation Stage 0C: the canonical bmas-digest profile in TypeScript.
 *
 * The profile canonicalizes a JSON value with RFC 8785. Object keys
 * sort by UTF-16 code units, strings use the ECMAScript JSON escape
 * rules without Unicode normalization, and the profile supports
 * integer numbers inside the I-JSON safe range only. Every digest
 * input starts with the frame `bmas:<domain>` and one NUL byte.
 *
 * The frozen vectors at conformance/digest_profile prove that this
 * implementation and the Python implementation produce byte-identical
 * output.
 */

export const DIGEST_PROFILE = "bmas-digest";
export const DIGEST_PROFILE_VERSION = "1";
export const DIGEST_ALGORITHM = "sha256";

const DOMAIN_PATTERN = /^[a-z0-9.-]+$/;

export class DigestInputError extends Error {}

type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

// The UTF-16 surrogate ranges: U+D800 through U+DBFF lead, and
// U+DC00 through U+DFFF trail.
const LEAD_SURROGATE_START = 55296;
const LEAD_SURROGATE_END = 56319;
const TRAIL_SURROGATE_START = 56320;
const TRAIL_SURROGATE_END = 57343;

function hasLoneSurrogate(text: string): boolean {
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code >= LEAD_SURROGATE_START && code <= LEAD_SURROGATE_END) {
      const next = text.charCodeAt(index + 1);
      if (next >= TRAIL_SURROGATE_START && next <= TRAIL_SURROGATE_END) {
        index += 1;
        continue;
      }
      return true;
    }
    if (code >= TRAIL_SURROGATE_START && code <= TRAIL_SURROGATE_END) {
      return true;
    }
  }
  return false;
}

function canonicalString(value: string): string {
  if (hasLoneSurrogate(value)) {
    throw new DigestInputError("The digest profile rejects invalid Unicode");
  }
  // JSON.stringify applies the ECMAScript escape rules: the
  // two-character escapes, lowercase \u00xx for other control
  // characters, and literal unnormalized text for everything else.
  return JSON.stringify(value);
}

function canonicalNumber(value: number): string {
  if (Object.is(value, -0)) return "0";
  if (!Number.isInteger(value)) {
    throw new DigestInputError(
      "The digest profile supports integer numbers only",
    );
  }
  if (!Number.isSafeInteger(value)) {
    throw new DigestInputError(
      `The digest profile rejects numbers outside the I-JSON safe integer range: ${value}`,
    );
  }
  return String(value);
}

/** Return the RFC 8785 canonical text of one JSON value. */
export function canonicalize(value: unknown): string {
  if (value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") return canonicalNumber(value);
  if (typeof value === "string") return canonicalString(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalize(item)).join(",")}]`;
  }
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new DigestInputError(
        "The digest profile rejects non-plain objects",
      );
    }
    const record = value as Record<string, JsonValue>;
    // Default string comparison orders by UTF-16 code units, which is
    // the RFC 8785 key order.
    const keys = Object.keys(record).sort();
    const entries = keys.map(
      (key) => `${canonicalString(key)}:${canonicalize(record[key])}`,
    );
    return `{${entries.join(",")}}`;
  }
  throw new DigestInputError(
    `The digest profile rejects values of type ${typeof value}`,
  );
}

/** Return the framed digest input bytes for one domain and value. */
export function digestInputBytes(domain: string, value: unknown): Uint8Array {
  if (!DOMAIN_PATTERN.test(domain)) {
    throw new DigestInputError(`Invalid digest domain: ${domain}`);
  }
  const encoder = new TextEncoder();
  const frame = encoder.encode(`bmas:${domain}`);
  const canonical = encoder.encode(canonicalize(value));
  const framed = new Uint8Array(frame.length + 1 + canonical.length);
  framed.set(frame, 0);
  framed[frame.length] = 0;
  framed.set(canonical, frame.length + 1);
  return framed;
}
