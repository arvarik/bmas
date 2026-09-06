/**
 * Every route that calls the daemon reaches it through a helper that
 * attaches the operator key.
 *
 * The daemon authenticates its whole edge, so a route that calls the
 * global fetch() directly receives 401 for every read once a
 * deployment configures an operator key. A plain fetch() to
 * DAEMON_BASE_URL is therefore a defect, and this test names the file
 * that carries it.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE_ROOT = path.resolve(__dirname, "..");
// The ways a file attaches the operator key: the shared helpers, or a
// direct read of the key it puts on its own Authorization header.
const CREDENTIAL_SOURCES = [
  "daemonFetch",
  "daemonHeaders",
  "daemonMutationHeaders",
  "BMAS_API_KEY",
];

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const full = path.join(directory, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return full.endsWith(".ts") || full.endsWith(".tsx") ? [full] : [];
  });
}

describe("daemon route credentials", () => {
  it("routes every daemon call through a helper that sends the operator key", () => {
    const offenders = sourceFiles(SOURCE_ROOT)
      .filter((file) => !file.includes(`${path.sep}__tests__${path.sep}`))
      .filter((file) => {
        const text = readFileSync(file, "utf8");
        if (!text.includes("DAEMON_BASE_URL")) return false;
        // config.ts declares the constant and calls nothing.
        if (!/(?<![A-Za-z])fetch\(/.test(text)) return false;
        return !CREDENTIAL_SOURCES.some((source) => text.includes(source));
      })
      .map((file) => path.relative(SOURCE_ROOT, file));

    expect(offenders).toEqual([]);
  });
});
