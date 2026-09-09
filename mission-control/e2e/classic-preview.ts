import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import type { Page } from "@playwright/test";

const execute = promisify(execFile);
const root = path.resolve(__dirname, "../..");
const python = existsSync(path.join(root, ".venv/bin/python")) ? path.join(root, ".venv/bin/python") : "python3";
// The browser exercises the real schema and route compiler over an isolated deployment.
const script = `
import asyncio, json, os, sys, tempfile, yaml
from pathlib import Path
from unittest.mock import AsyncMock
from fastapi import FastAPI
import httpx
fixture_config = yaml.safe_load(Path(os.environ["BMAS_CONFIG"]).read_text())
fixture_config["storage"]["enabled"] = False
with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as config_file:
    yaml.safe_dump(fixture_config, config_file)
    config_file.flush()
    os.environ["BMAS_CONFIG"] = config_file.name
    from routes import classic_spec
from core.variants.classic.spec import DeploymentSnapshot, BoardSettings
classic_spec.BMAS_API_KEY = ""
classic_spec.deployment_snapshot = AsyncMock(return_value=DeploymentSnapshot(
    board=BoardSettings(max_entry_chars=8000, max_title_len=200),
    routing={tier: "fixture" for tier in ("simple", "light", "medium", "complex")},
    triage_model="fixture", node_endpoints=["http://agent.fixture"],
    model_profiles={"fixture": {"provider": "fixture", "model": "fixture"}},
    model_pricing={"fixture": {"input_cost_per_token": "0.000001", "output_cost_per_token": "0.000002"}},
))
app = FastAPI()
app.include_router(classic_spec.router)
async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.request(sys.argv[1], "/classic/spec/" + ("schema" if sys.argv[1] == "GET" else "compile"), content=sys.argv[2])
        print(json.dumps({"status": response.status_code, "body": response.json()}))
asyncio.run(main())
`;
export async function compileResponse(method: string, input = "{}") {
  const { stdout } = await execute(python, ["-c", script, method, input], {
    cwd: root,
    env: { ...process.env, REDIS_PASSWORD: "preview-fixture", LITELLM_MASTER_KEY: "preview-fixture", BMAS_NODE_KEY: "preview-fixture", PYTHONPATH: path.join(root, "daemon/src"), BMAS_CONFIG: path.join(root, "bmas.example.yaml") },
    maxBuffer: 4 * 1024 * 1024,
  });
  return JSON.parse(stdout) as { status: number; body: Record<string, unknown> };
}
export async function classicShell(page: Page) {
  await page.route("**/api/benchmarks/tests?**", (route) => route.fulfill({ json: { tests: [], total: 0 } }));
  await page.route("**/api/tasks?**", (route) => route.fulfill({ json: { tasks: [], total: 0, grand_total: 0, limit: 50, offset: 0 } }));
  await page.route("**/api/stream/system", (route) => route.fulfill({ contentType: "text/event-stream", body: "" }));
  await page.route("**/api/readiness", (route) => route.fulfill({ json: { status: "ready", checks: [] } }));
  await page.route("**/api/capabilities", (route) => route.fulfill({ json: {
    api_version: "1", variants: [{ id: "classic", label: "Classic", available: true, contract_version: "1", configuration_schema_version: "1", supports_recovery: true, aliases: [], required_agent_features: [], features: { events: [], panels: [], graphs: [], controls: [], progress: [], result: [] } }],
  } }));
  await page.route("**/api/datasets?**", (route) => route.fulfill({ json: { datasets: [{ id: "fixture", name: "Fixture", latest_version_id: "fixture-version", latest_version: 1 }] } }));
  await page.route("**/api/benchmarks/scorers", (route) => route.fulfill({ json: { scorers: [{ id: "exact", name: "Exact", version: "1", description: "Exact match" }] } }));
}
