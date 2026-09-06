/**
 * The keyed digest, the semantic text transform, and the exact content
 * digest in TypeScript.
 *
 * The daemon is the reference implementation. Every digest input
 * starts with the frame `bmas:<domain>` and one NUL byte. The keyed
 * digest is an HMAC-SHA256 over that frame under one tenant key. The
 * semantic text transform applies NFC normalization and LF line
 * endings for comparison only. The exact content digest hashes the
 * exact UTF-8 bytes with no normalization.
 *
 * The frozen vectors at daemon/tests/fixtures/keyed_digest.json prove
 * that this implementation and the Python implementation produce
 * byte-identical output.
 */
import { createHash, createHmac } from "node:crypto";

export const KEYED_DIGEST_ALGORITHM = "hmac-sha256";
export const KEYED_DIGEST_DOMAIN_PREFIX = "bmas:";

// A lone surrogate is a high surrogate without a following low surrogate,
// or a low surrogate without a preceding high surrogate.
const HIGH_SURROGATES = "\\ud800-\\udbff";
const LOW_SURROGATES = "\\udc00-\\udfff";
const LONE_SURROGATE = new RegExp(
  `[${HIGH_SURROGATES}](?![${LOW_SURROGATES}])|(?<![${HIGH_SURROGATES}])[${LOW_SURROGATES}]`,
);
const ASCII_DOMAIN = /^[\x21-\x7e]+$/;

export class KeyedDigestError extends Error {}

/** Return the canonical text value for semantic comparison. */
export function semanticText(value: string): string {
  if (LONE_SURROGATE.test(value)) {
    throw new KeyedDigestError("The semantic text transform rejects invalid Unicode");
  }
  return value.normalize("NFC").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

/** Frame one payload under one domain: `bmas:<domain>` + NUL + payload. */
export function framedBytes(domain: string, payload: Uint8Array): Uint8Array {
  if (!domain || !ASCII_DOMAIN.test(domain)) {
    throw new KeyedDigestError(`Invalid digest domain: ${domain}`);
  }
  const frame = new TextEncoder().encode(`${KEYED_DIGEST_DOMAIN_PREFIX}${domain}`);
  const framed = new Uint8Array(frame.length + 1 + payload.length);
  framed.set(frame, 0);
  framed[frame.length] = 0;
  framed.set(payload, frame.length + 1);
  return framed;
}

/** SHA-256 of the exact framed bytes, as lowercase hex. */
export function exactBytesDigestHex(domain: string, payload: Uint8Array): string {
  return createHash("sha256").update(framedBytes(domain, payload)).digest("hex");
}

/** SHA-256 of the exact UTF-8 bytes of one text value, framed under one domain. */
export function exactTextDigestHex(domain: string, value: string): string {
  if (LONE_SURROGATE.test(value)) {
    throw new KeyedDigestError("The exact content digest rejects invalid Unicode");
  }
  return exactBytesDigestHex(domain, new TextEncoder().encode(value));
}

/** HMAC-SHA256 of one framed text value under one tenant key, as lowercase hex. */
export function keyedDigestHex(keyBytes: Uint8Array, domain: string, value: string): string {
  if (keyBytes.length < 32) {
    throw new KeyedDigestError("A keyed digest key needs at least 32 bytes");
  }
  if (LONE_SURROGATE.test(value)) {
    throw new KeyedDigestError("The keyed digest rejects invalid Unicode");
  }
  const framed = framedBytes(domain, new TextEncoder().encode(value));
  return createHmac("sha256", keyBytes).update(framed).digest("hex");
}

export function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0 || !/^[0-9a-f]*$/.test(hex)) {
    throw new KeyedDigestError("A key is lowercase hex with an even length");
  }
  const bytes = new Uint8Array(hex.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}
