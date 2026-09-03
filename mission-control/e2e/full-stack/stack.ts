import { readFileSync } from "node:fs";
import { ENV_FILE } from "./global-setup";

export interface StackEnvironment {
  stack_id: string;
  ports: Record<string, number>;
  urls: Record<string, string>;
  api_key: string;
  database_path: string;
  log_dir: string;
}

/** Read the generated environment file the controller wrote. */
export function readStack(): StackEnvironment {
  return JSON.parse(readFileSync(ENV_FILE, "utf-8")) as StackEnvironment;
}
