import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";

/**
 * Start the complete local stack through the test-stack controller.
 *
 * The controller starts Redis, the daemon with its scheduler, the
 * deterministic fake provider, the real agent service, and Mission
 * Control with test-only credentials, waits for one real readiness
 * endpoint per service, and writes the selected ports to one
 * generated environment file. Playwright starts only after that file
 * exists. A readiness check that reads a mocked route never exists:
 * every check here is one HTTP request to a running process.
 */
export const ENV_FILE = path.resolve(__dirname, "../../test-results/full-stack/test-env.json");
const REPO_ROOT = path.resolve(__dirname, "../../..");

/**
 * Resolve the Python interpreter that carries the daemon and agent
 * dependencies: an explicit BMAS_TEST_PYTHON, else the repository
 * virtual environment, else the interpreter on PATH (continuous
 * integration installs the dependencies there).
 */
export function resolvePython(): string {
  if (process.env.BMAS_TEST_PYTHON) return process.env.BMAS_TEST_PYTHON;
  const venv = path.join(REPO_ROOT, ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}

export default async function globalSetup(): Promise<void> {
  mkdirSync(path.dirname(ENV_FILE), { recursive: true });
  const python = resolvePython();
  const started = spawnSync(
    python,
    ["scripts/test-stack.py", "start", "--env-file", ENV_FILE, "--keep-on-failure"],
    { cwd: REPO_ROOT, stdio: "inherit", timeout: 420_000 },
  );
  if (started.status !== 0) {
    throw new Error(`the test-stack controller failed to start the complete stack (exit ${started.status})`);
  }
}
