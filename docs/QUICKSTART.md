# Quick Start

This guide starts the classic runtime on one computer. The starter uses one cloud provider and one tool-free execution agent.

## Prerequisites

Install these tools:

- Docker 24 or newer
- Docker Compose 2.20 or newer
- `curl`
- One Gemini, Anthropic, or OpenAI API key

The starter does not need a GPU. It does not need a separate agent computer.

## 1. Get the source

```bash
git clone https://github.com/arvarik/bmas.git
cd bmas
```

## 2. Create the starter files

Choose your provider.

```bash
./scripts/bmas init --provider gemini
```

You can replace `gemini` with `anthropic` or `openai`. The command requests your provider key without displaying it.

The command creates two local files:

- `bmas.yaml` contains the non-secret runtime configuration.
- `.env` contains the provider key and generated local secrets.

The command sets `.env` permissions to `0600`. Git ignores both files.

For a non-interactive shell, pass the provider key through one temporary variable.

```bash
BMAS_PROVIDER_API_KEY="your-provider-key" ./scripts/bmas init --provider gemini
```

Do not use `--force` unless you want to replace the existing `bmas.yaml` and `.env` files.

## 3. Check the configuration

```bash
./scripts/bmas doctor
```

The command checks Docker, Compose, required files, required secrets, the selected provider key, and the Compose document.

## 4. Start the stack

```bash
./scripts/bmas up
```

This command builds each local image. It waits up to three minutes for the full stack.

The final output must include these results:

```text
PASS: Redis, SQLite, LiteLLM, the starter agent, and classic are ready.
PASS: Mission Control is reachable.
```

If startup fails, run this command:

```bash
./scripts/bmas doctor --wait 30
```

The readiness output identifies the failed service. The [Operations guide](OPERATIONS.md) lists exact log commands.

## 5. Submit a smoke task

```bash
./scripts/bmas smoke
```

The command submits one classic task. It waits for the final task status and prints the Mission Control URL.

This test can use several provider requests. Check `coordination.classic.budget_ceiling_usd` before you use an expensive model.

## 6. Open Mission Control

Open [http://localhost:9321](http://localhost:9321).

The browser requests a username and password. Enter any username. Use the `BMAS_DASHBOARD_KEY` value from `.env` as the password.

Mission Control checks readiness before it enables task submission. A failed check includes one repair command.

Select the system status button in the top bar. The panel shows these checks in one place:

- Provider credential variables and the model gateway
- Agent availability
- Upload and output storage access
- Redis access
- Active and queued task capacity

Select **Run test task** after every required check passes. This action submits a real classic task. It verifies the browser, daemon, queue, agent, model provider, and event stream.

The home page shows one composer. Type the objective, attach files if you need them, select a runtime, and press Enter.

The **Tasks** page lists every task. Select **Needs attention** to see failed tasks and pending operator actions. Use the search field, sort menu, and filters to find older work. Save a filter set as a view to reuse it. Pin a task to keep it at the top of the list.

Each task page shows one lifecycle row. The row identifies queued, running, blocked, failed, and completed work. It also shows the queue, provider, agents, model, tokens, cost, and storage use.

Use these task actions when they appear:

- **Cancel** stops a queued or running task.
- **Resume** restarts a paused task or queues compatible blocked recovery.
- **Retry** creates a new task from a failed task and copies its input files.
- **Duplicate** creates a new task from any saved task and copies its input files.

Open the **Files** tab to review all task files. The Inputs section contains user uploads. The Outputs section contains agent artifacts and their immutable versions.

Mission Control previews PDF, image, text, JSON, Markdown, and common code files. Select two output versions to compare them side by side.

## Stop the stack

```bash
docker compose down
```

This command keeps Redis data, SQLite data, uploads, artifacts, and agent state in named Docker volumes.

## Next steps

- Read [Concepts](CONCEPTS.md) to understand the five starter services.
- Read [Configuration](CONFIGURATION.md) to change models, budgets, or role limits.
- Read [Deployment](DEPLOYMENT.md) before you expose any port outside the local computer.
- Read [Node Setup](NODE_SETUP.md) when you need Hermes tools or more execution capacity.
- Read [Development](DEVELOPMENT.md) before you change the source code.
