import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { ENV_FILE, resolvePython } from "./global-setup";

const REPO_ROOT = path.resolve(__dirname, "../../..");

/**
 * Send cancellation, stop every process in reverse order, verify that
 * no process, port, or temporary secret survives, and delete the
 * temporary data. A teardown that leaves state fails the run.
 */
export default async function globalTeardown(): Promise<void> {
  if (!existsSync(ENV_FILE)) return;
  const python = resolvePython();
  const stopped = spawnSync(
    python,
    ["scripts/test-stack.py", "stop", "--env-file", ENV_FILE],
    { cwd: REPO_ROOT, stdio: "inherit", timeout: 120_000 },
  );
  if (stopped.status !== 0) {
    throw new Error(`teardown left a process, a bound port, or a temporary secret (exit ${stopped.status})`);
  }
}
