# Quick Start Guide

Get a bMAS swarm running in under 5 minutes.

## Prerequisites

- **Docker** ≥ 24.0 and **Docker Compose** ≥ 2.20
- At least one cloud API key (Gemini, Anthropic, or OpenAI)
- *Optional:* NVIDIA GPU + drivers for local triage classification
- *Optional:* Edge nodes (separate machines) for local inference

## 1. Clone the Repository

```bash
git clone https://github.com/arvarik/bmas.git
cd bmas
```

## 2. Configure Your Deployment

```bash
# Copy the example config and secrets template
cp bmas.example.yaml bmas.yaml
cp .env.example .env
```

### Edit `bmas.yaml`

This file defines your entire deployment. At minimum, set:

```yaml
project:
  name: "My bMAS"            # Your deployment name

control_plane:
  host: "localhost"           # Or your server IP
  ports:
    redis: 6379
    litellm: 4000
    daemon: 9000
    dashboard: 9321
```

If you have edge nodes with Hermes agents, add them under `nodes:`.
If not, set `nodes: []` and all routing will go to cloud models.

See [CONFIGURATION.md](CONFIGURATION.md) for the full reference.

### Edit `.env`

Fill in your secrets:

```env
REDIS_PASSWORD=your-secure-password
LITELLM_MASTER_KEY=sk-your-litellm-key
GEMINI_API_KEY=your-gemini-key
```

## 3. Start the Swarm

```bash
# Without GPU (no local triage)
docker compose up -d

# With GPU (enables local triage classifier)
docker compose --profile gpu up -d
```

## 4. Verify

Check that all services are healthy:

```bash
docker compose ps
```

You should see:

| Service | Status |
|:---|:---|
| `bmas-redis` | Up (healthy) |
| `bmas-litellm` | Up (healthy) |
| `bmas-daemon` | Up |
| `bmas-dashboard` | Up |
| `bmas-triage` | Up *(only with `--profile gpu`)* |

Open the dashboard:

```
http://localhost:9321
```

## 5. Submit a Test Task

From the dashboard, type a task in the command bar, or use the API directly:

```bash
curl -X POST http://localhost:9000/submit \
  -H "Content-Type: application/json" \
  -d '{"task": "Explain the architecture of a blackboard multi-agent system"}'
```

## Next Steps

- **Add edge nodes:** See [NODE_SETUP.md](NODE_SETUP.md) for provisioning agents
- **Multi-provider routing:** See [CONFIGURATION.md](CONFIGURATION.md) for mixing Gemini + Claude + OpenAI
- **Development:** Use `docker compose -f docker-compose.yml -f docker-compose.dev.yml up` for hot reload
