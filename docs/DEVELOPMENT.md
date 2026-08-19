# Development

This guide defines one supported local development setup.

## Required tools

- Python 3.13
- Node.js 22
- npm
- Docker 24 or newer
- Docker Compose 2.20 or newer
- `curl`

Use the exact major versions from continuous integration. The production Dockerfiles use fixed patch versions.

## Install development dependencies

Run one command from the repository root.

```bash
./scripts/bmas setup-dev
```

The command creates `.venv`, installs Python development dependencies, and runs `npm ci` for Mission Control.

Set `PYTHON_COMMAND` when Python 3.13 has another executable name.

```bash
PYTHON_COMMAND=/path/to/python3.13 ./scripts/bmas setup-dev
```

Do not install the repository dependencies into the system Python environment.

## Create local runtime files

The development Compose stack uses the same `bmas.yaml` and `.env` files as the production Compose stack.

```bash
./scripts/bmas init --provider gemini
```

If these files already exist, keep them. The `init --force` option replaces both files.

## Start development services

```bash
./scripts/bmas dev
```

This command starts source mounts and reload servers for the daemon, starter agent, and Mission Control.

Open [http://localhost:9321](http://localhost:9321). Use the generated `BMAS_DASHBOARD_KEY` as the browser password.

Press `Ctrl+C` to stop the foreground Compose logs. Run `docker compose down` if containers remain active.

## Run all checks

```bash
./scripts/bmas test
```

The command runs these groups:

1. Daemon Ruff lint, mypy type checks, and pytest tests.
2. Agent pytest tests.
3. Evaluation pytest tests.
4. Mission Control install, lint, type checks, tests, production build, and browser tests.
5. Configuration, generated schema, documentation link, and Compose checks.

The command stops after it reports all failed groups. It returns a nonzero status when one group fails.

## Run one Python test group

```bash
.venv/bin/python -m pytest daemon/tests/test_config_validation.py -q
.venv/bin/python -m pytest agent/tests/test_reliability.py -q
.venv/bin/python -m pytest eval/tests -q
```

Run Ruff and mypy from the daemon directory so each tool reads `daemon/pyproject.toml`.

```bash
cd daemon
../.venv/bin/python -m ruff check src tests
../.venv/bin/python -m mypy src --ignore-missing-imports
```

## Run Mission Control checks

```bash
cd mission-control
npm run lint
npx tsc --noEmit
npm run test:run
npm run build
npx playwright install chromium
npm run test:e2e
```

The Vitest environment uses Node. Playwright starts Mission Control and tests browser flows through mocked API contracts.

## Change `bmas.yaml` fields

The typed source of truth is `daemon/src/config_schema.py`.

Complete these steps for each configuration change:

1. Change the typed schema.
2. Change the daemon loader.
3. Change each affected published example.
4. Change [Configuration](CONFIGURATION.md).
5. Add a loader or semantic test.
6. Regenerate `docs/reference/config.schema.json`.
7. Run the configuration checks.

```bash
.venv/bin/python scripts/generate_config_schema.py
.venv/bin/python scripts/validate_configs.py
```

The configuration validator requires every published example to load without warnings.

## Change documentation

```bash
./scripts/bmas docs-check
```

The command checks local links in tracked Markdown files. Continuous integration also checks the generated configuration schema.

## Change Mission Control components

Use server components unless the component needs state, an effect, or a browser API.

Keep daemon requests in server routes under `src/app/api`. Validate each response before a client component uses it.

Run lint, type checks, tests, and the production build after a component change.

## Add an API route

1. Define the request and response contract.
2. Validate untrusted input at the route boundary.
3. Add authentication to mutation routes.
4. Add a focused route test.
5. Add the route to the relevant component README.
6. Check `/health` or `/readiness` when the route depends on a new service.

## Repository rules

- Keep secrets in `.env` only.
- Keep generated runtime files out of Git.
- Use named volumes for durable container data.
- Use fixed base-image versions.
- Keep the public examples warning-free.
- Preserve older saved task input aliases when a migration requires them.
- Add an exact test for each fixed defect.
