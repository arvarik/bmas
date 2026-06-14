# Edge Node Setup Guide

This guide covers manually provisioning edge nodes for your bMAS swarm.
Each node consists of two components:

1. **Inference Server** — Runs `llama.cpp` (or vLLM) serving a local model
2. **Hermes Agent** — Runs the bMAS agent process that receives tasks from the daemon

> **Future:** Automated provisioning via `bmas provision` is planned.

---

## Prerequisites (Per Node)

- Linux host (bare metal, VM, or LXC container)
- Python 3.11+
- For inference: GPU with Vulkan support (AMD/NVIDIA/Intel) OR NVIDIA GPU with CUDA
- Network access to the control plane (Redis, LiteLLM, Daemon)

---

## Architecture

Each logical "node" in `bmas.yaml` typically maps to two hosts:

```
┌─────────────────────┐     ┌─────────────────────┐
│   Inference LXC     │     │    Agent LXC         │
│                     │     │                      │
│  llama-server       │────▶│  Hermes agent        │
│  :8080              │     │  :8000               │
│  (runs the model)   │     │  (processes tasks)   │
└─────────────────────┘     └──────────┬───────────┘
                                       │
                                       │ POST /ingest/traces
                                       │ POST /ingest/logs
                                       ▼
                               ┌───────────────┐
                               │ Daemon :9000  │
                               │ (control plane)│
                               └───────────────┘
```

You can also run both on the same machine if resources allow.

---

## Part 1: Inference Server Setup

### Option A: llama.cpp with Vulkan (Recommended for AMD GPUs)

```bash
# Install build dependencies
apt update && apt install -y git cmake build-essential libvulkan-dev

# Clone and build llama.cpp
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DGGML_VULKAN=ON
cmake --build build --config Release -j$(nproc)

# Download a model (example: Gemma 4B)
mkdir -p models
# Use huggingface-cli or wget to download your model
# huggingface-cli download google/gemma-4-e4b-it-gguf --local-dir models/

# Start the server
./build/bin/llama-server \
  --model models/your-model.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  --n-gpu-layers 99 \
  --ctx-size 8192
```

### Option B: vLLM with CUDA (For NVIDIA GPUs)

```bash
pip install vllm
python -m vllm.entrypoints.openai.api_server \
  --model google/gemma-4-e4b \
  --host 0.0.0.0 \
  --port 8080
```

### Create a systemd Service

```bash
cat > /etc/systemd/system/llama-server.service << 'EOF'
[Unit]
Description=llama.cpp Inference Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/llama.cpp
ExecStart=/path/to/llama.cpp/build/bin/llama-server \
  --model /path/to/model.gguf \
  --host 0.0.0.0 --port 8080 \
  --n-gpu-layers 99 --ctx-size 8192
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now llama-server
```

### Verify

```bash
curl http://localhost:8080/v1/models
# Should return the model info
```

---

## Part 2: Hermes Agent Setup

### Install Hermes

```bash
# Create a virtual environment
python3 -m venv /opt/hermes-agent/.venv
source /opt/hermes-agent/.venv/bin/activate

# Install Hermes (from your agent framework)
pip install hermes-agent  # or clone from your repo
```

### Deploy the Agent API Server

The bMAS repo contains the canonical agent code at `agent/api_server.py`. Copy it to the node:

```bash
# From the control plane (where the bmas repo lives)
scp /opt/bmas/agent/api_server.py root@NODE_IP:/opt/bmas/api_server.py
scp /opt/bmas/agent/requirements.txt root@NODE_IP:/opt/bmas/requirements.txt

# On the node: install dependencies
ssh root@NODE_IP 'cd /opt/bmas && pip install -r requirements.txt'
```

### Deploy Hermes Profiles

Each agent role needs its Hermes profile (planner, expert, critic, etc.). Use the deploy script:

```bash
# From the control plane
./scripts/deploy_profiles.sh
```

Or manually copy profiles to each node:

```bash
scp -r /opt/bmas/agent/profiles/* root@NODE_IP:~/.hermes/profiles/
```

### Configure the Systemd Service

Each agent needs environment variables for its role and the daemon ingest connection:

```bash
cat > /etc/systemd/system/hermes-agent.service << 'EOF'
[Unit]
Description=Hermes bMAS Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/bmas
Environment="PATH=/opt/hermes-agent/.venv/bin:/usr/local/bin:/usr/bin"
Environment="NODE_ID=agent-node1"
Environment="LITELLM_MODEL=medium"
Environment="LITELLM_URL=http://<CONTROL_PLANE_IP>:4000/v1"
Environment="HERMES_GATEWAY_URL=http://localhost:8642"
Environment="HERMES_GATEWAY_KEY=your-gateway-key"
Environment="DAEMON_INGEST_URL=http://<CONTROL_PLANE_IP>:9000"
Environment="BMAS_NODE_KEY=your-node-auth-token"
ExecStart=/opt/hermes-agent/.venv/bin/uvicorn api_server:app \
  --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now hermes-agent
```

#### Environment Variables

| Variable | Required | Description |
|:---|:---|:---|
| `NODE_ID` | ✅ | Unique node identifier (e.g., `agent-node1`) |
| `LITELLM_URL` | ✅ | LiteLLM gateway URL on the control plane |
| `LITELLM_MODEL` | ❌ | Default model name (default: `medium`) |
| `HERMES_GATEWAY_URL` | ❌ | Hermes Gateway URL for Runs API path |
| `HERMES_GATEWAY_KEY` | ❌ | API key for the Hermes Gateway |
| `DAEMON_INGEST_URL` | ❌ | Daemon URL for trace/log ingest (e.g., `http://192.168.1.100:9000`) |
| `BMAS_NODE_KEY` | ❌ | Bearer token for authenticating ingest requests (must match `.env` on control plane) |
| `SSE_READ_TIMEOUT` | ❌ | SSE stream read timeout in seconds (default: `600`) |

> **Note:** Set `NODE_ID` to a unique value per node (e.g., `agent-node1`, `agent-node2`, `agent-node3`). Set `BMAS_NODE_KEY` to the same value as in your control plane `.env` to enable trace/log shipping.

### Verify

```bash
curl http://localhost:8000/health
# Should return {"status":"healthy","node_id":"agent-node1",...}
```

---

## Part 3: Register in bmas.yaml

Add the node to your `bmas.yaml` on the control plane:

```yaml
nodes:
  - name: "my-new-node"
    host: "192.168.1.101"      # Agent IP
    port: 8000
    role: executor              # or planner, auditor, or any custom role
    inference:
      host: "192.168.1.102"    # Inference server IP
      port: 8080
      model: "gemma-4-e4b"
```

If using the role registry, also add the role mapping:

```yaml
coordination:
  role_registry:
    planner:
      preferred_host: "192.168.1.101"
      profile: planner
      dispatch_port: 8000
```

Restart the control plane to pick up the new node:

```bash
docker compose restart daemon dashboard litellm
```

The new node should appear in the Mission Control dashboard.

---

## Troubleshooting

| Issue | Solution |
|:---|:---|
| Agent health check fails | Verify `LITELLM_URL` is reachable from the agent node |
| Traces not appearing in dashboard | Check `DAEMON_INGEST_URL` and `BMAS_NODE_KEY` match the control plane `.env` |
| Hermes profiles not found | Ensure profiles are deployed to `~/.hermes/profiles/` on the agent node |
| Runs API not working | Set `HERMES_GATEWAY_URL` to the Hermes Gateway address; falls back to CLI otherwise |
