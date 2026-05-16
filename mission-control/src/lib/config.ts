/**
 * bMAS Configuration — loaded from bmas.yaml at server startup.
 *
 * This module reads the central bmas.yaml config file and exports all
 * derived values (URLs, endpoints, stream keys) as typed constants.
 * Secrets (passwords, API keys) are always read from environment variables.
 *
 * Only runs server-side (Node.js). Client components should NOT import this.
 *
 * At Next.js *build* time the YAML file may not yet be mounted (Docker
 * multi-stage build). In that case we fall back to safe defaults so the
 * build succeeds — real values are resolved at container start.
 */

import { readFileSync } from "fs";
import { load } from "js-yaml";

// ── Types ─────────────────────────────────────────────────────────────

interface BmasNode {
  name: string;
  host: string;
  port: number;
  role: string;
  color?: string;
  dashboard_port?: number;
  inference?: {
    host: string;
    port: number;
    model: string;
  };
}

interface BmasConfig {
  project: {
    name: string;
    description?: string;
  };
  control_plane: {
    host: string;
    ports: {
      redis: number;
      litellm: number;
      triage?: number;
      daemon: number;
      dashboard: number;
    };
  };
  nodes: BmasNode[];
  triage?: {
    enabled?: boolean;
    model?: string;
    gpu_memory_utilization?: number;
    max_model_len?: number;
    default_complexity?: string;
  };
  models?: Record<
    string,
    {
      provider: string;
      model: string;
      api_key_env: string;
      max_tokens?: number;
    }
  >;
  routing?: Record<string, string>;
  monitoring?: {
    beszel_hub?: string;
  };
}

// ── Helpers ───────────────────────────────────────────────────────────

function fatal(msg: string, hint?: string): never {
  const lines = [`\n❌ FATAL: ${msg}`];
  if (hint) lines.push(`   ↳ ${hint}`);
  throw new Error(lines.join("\n"));
}

function ok(msg: string): void {
  console.log(`  ✓ ${msg}`);
}

function warn(msg: string): void {
  console.warn(`⚠️  WARNING: ${msg}`);
}

function requireEnv(name: string, description: string): string {
  const val = process.env[name];
  if (!val) {
    fatal(
      `Missing required environment variable: ${name}`,
      `${description}. Set it in .env or your shell environment.`,
    );
  }
  return val;
}

// ── Minimal defaults for build-time (no bmas.yaml available) ──────────

const BUILD_TIME_DEFAULTS: BmasConfig = {
  project: { name: "bMAS", description: "Blackboard Multi-Agent System" },
  control_plane: {
    host: "localhost",
    ports: { redis: 6379, litellm: 4000, daemon: 9000, dashboard: 9321 },
  },
  nodes: [],
};

// ── Load Config ───────────────────────────────────────────────────────

const CONFIG_PATH = process.env.BMAS_CONFIG ?? "/etc/bmas/bmas.yaml";

/**
 * Detect whether we are in Next.js's static generation / build phase.
 * During `next build` the NEXT_PHASE env is set to "phase-production-build".
 * In that phase the YAML config may not exist yet (Docker multi-stage build).
 */
const isBuildPhase = process.env.NEXT_PHASE === "phase-production-build";

if (!isBuildPhase) {
  console.log("");
  console.log("┌─────────────────────────────────────────────┐");
  console.log("│    Mission Control — Configuration Loader   │");
  console.log("└─────────────────────────────────────────────┘");
  console.log(`  Config: ${CONFIG_PATH}`);
  console.log("");
}

let cfg: BmasConfig;
try {
  const raw = readFileSync(CONFIG_PATH, "utf8");
  const parsed = load(raw);

  // js-yaml's load() returns undefined for empty files / bare "---" documents
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    fatal(
      `${CONFIG_PATH} is empty or not a YAML mapping`,
      "The file must contain a top-level YAML mapping (key: value pairs).",
    );
  }

  cfg = parsed as BmasConfig;
  if (!isBuildPhase) ok(`Config file loaded: ${CONFIG_PATH}`);
} catch (err) {
  if (isBuildPhase) {
    // Build time: use minimal defaults so Next.js can compile pages that
    // import this module. Real values are resolved when the container starts.
    console.warn(
      `[config] bmas.yaml not found at build time (${CONFIG_PATH}), using defaults`,
    );
    cfg = BUILD_TIME_DEFAULTS;
  } else {
    const message = err instanceof Error ? err.message : String(err);
    fatal(
      `Failed to load bMAS config from ${CONFIG_PATH}`,
      `${message}\n   ↳ Copy bmas.example.yaml → bmas.yaml and configure your deployment.`,
    );
  }
}

// ── Validate Required Keys (skip during build phase) ──────────────────

if (!isBuildPhase) {
  if (!cfg.project?.name) {
    fatal(
      "'project.name' is required",
      `Add 'project.name' to ${CONFIG_PATH}. See bmas.example.yaml for reference.`,
    );
  }
  if (!cfg.control_plane?.host) {
    fatal(
      "'control_plane.host' is required",
      `Add 'control_plane.host' to ${CONFIG_PATH}. This is the IP/hostname of your control plane.`,
    );
  }
  if (!cfg.control_plane?.ports?.redis) {
    fatal(
      "'control_plane.ports.redis' is required",
      `Add the Redis port under 'control_plane.ports' in ${CONFIG_PATH}.`,
    );
  }
  if (!cfg.control_plane?.ports?.daemon) {
    fatal(
      "'control_plane.ports.daemon' is required",
      `Add the daemon port under 'control_plane.ports' in ${CONFIG_PATH}.`,
    );
  }

  ok(`Project: ${cfg.project.name}`);
  ok(`Control plane: ${cfg.control_plane.host} (redis:${cfg.control_plane.ports.redis}, daemon:${cfg.control_plane.ports.daemon})`);
}

// ── Exported Constants ────────────────────────────────────────────────

/** Project name from config (used in page titles, etc.) */
export const PROJECT_NAME: string = cfg.project.name;

/** Project description from config */
export const PROJECT_DESCRIPTION: string =
  cfg.project.description ??
  "Real-time operations dashboard for a distributed AI swarm built on the Blackboard Multi-Agent System architecture.";

const cp = cfg.control_plane;

/** Redis connection URL (password from env var, defaults to DB 0) */
export const REDIS_URL: string = (() => {
  if (isBuildPhase) {
    return `redis://:placeholder@${cp.host}:${cp.ports.redis}/0`;
  }
  console.log("");
  console.log("  Checking environment variables...");
  const password = requireEnv("REDIS_PASSWORD", "Password for Redis authentication");
  ok("REDIS_PASSWORD is set");
  return `redis://:${password}@${cp.host}:${cp.ports.redis}/0`;
})();

/** bMAS Daemon state endpoint */
export const DAEMON_STATE_URL: string =
  `http://${cp.host}:${cp.ports.daemon}/state`;

/** bMAS Daemon submit endpoint */
export const DAEMON_SUBMIT_URL: string =
  `http://${cp.host}:${cp.ports.daemon}/submit`;

/** bMAS Daemon base URL (for building arbitrary daemon API paths) */
export const DAEMON_BASE_URL: string =
  `http://${cp.host}:${cp.ports.daemon}`;

/** Agent host URLs keyed by role (bMAS API at :8000) */
export const AGENT_HOSTS: Record<string, string> = Object.fromEntries(
  (cfg.nodes ?? []).map((n) => [n.role, `http://${n.host}:${n.port}`]),
);

/** Agent dashboard URLs keyed by role (Hermes Dashboard at :9119) */
export const AGENT_DASHBOARD_HOSTS: Record<string, string> = Object.fromEntries(
  (cfg.nodes ?? []).map((n) => [
    n.role,
    `http://${n.host}:${n.dashboard_port ?? 9119}`,
  ]),
);

/** Redis Stream keys for log tailing (one per agent node) */
export const STREAM_KEYS: string[] = (cfg.nodes ?? []).map(
  (n) => `bmas:logs:${n.role}`,
);

/** Beszel Hub URL for telemetry (optional) */
export const BESZEL_HUB_URL: string | null =
  cfg.monitoring?.beszel_hub ?? null;

/** Beszel Hub credentials for PocketBase auth (optional) */
export const BESZEL_EMAIL: string | null =
  process.env.BESZEL_EMAIL || null;
export const BESZEL_PASSWORD: string | null =
  process.env.BESZEL_PASSWORD || null;

/** Node configuration (for UI colors, etc.) */
export const NODES: BmasNode[] = cfg.nodes ?? [];

/** The raw config object (for advanced use) */
export const RAW_CONFIG: BmasConfig = cfg;

// ── Runtime Startup Summary ───────────────────────────────────────────

if (!isBuildPhase) {
  console.log("");
  console.log("  Validating nodes and streams...");
  if (NODES.length > 0) {
    for (const node of NODES) {
      ok(`Node '${node.role}' → ${node.host}:${node.port}`);
    }
    ok(`Log streams: ${STREAM_KEYS.join(", ")}`);
  } else {
    warn("No agent nodes configured — agent panels and log streams will be empty.");
  }

  if (BESZEL_HUB_URL) {
    ok(`Monitoring: ${BESZEL_HUB_URL}`);
  } else {
    ok("Monitoring: disabled (no beszel_hub in config)");
  }

  console.log("");
  console.log("┌─────────────────────────────────────────────┐");
  console.log(`│  ✅ ${PROJECT_NAME} — Configuration OK        `);
  console.log("└─────────────────────────────────────────────┘");
  console.log(`  Daemon:  ${DAEMON_STATE_URL.replace("/state", "")}`);
  console.log(`  Redis:   ${cp.host}:${cp.ports.redis}`);
  console.log(`  Agents:  ${Object.keys(AGENT_HOSTS).join(", ") || "none"}`);
  console.log(`  Streams: ${STREAM_KEYS.length} configured`);
  console.log("");
}
