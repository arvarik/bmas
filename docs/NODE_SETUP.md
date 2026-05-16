# Edge Node Setup Guide

This guide covers manually provisioning edge nodes for your bMAS swarm.
Each node consists of two components:

1. **Inference Server** — Runs `llama.cpp` (or vLLM) serving a local model
2. **Hermes Agent** — Runs the bMAS agent process that receives tasks from the daemon

> **Future:** Automated provisioning via `bmas provision` is planned. See [TODO.md](TODO.md).

---

## Prerequisites (Per Node)

- Linux host (bare metal, VM, or LXC container)
- Python 3.11+
- For inference: GPU with Vulkan support (AMD/NVIDIA/Intel) OR NVIDIA GPU with CUDA
- Network access to the control plane (Redis, LiteLLM)

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
└─────────────────────┘     └─────────────────────┘
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

### Configure the Agent Persona

### Deploy the Agent API Server

The bMAS repo contains the canonical agent code at `agent/api_server.py`. Copy it to the node:

```bash
# From the control plane (where the bmas repo lives)
scp /opt/bmas/agent/api_server.py root@NODE_IP:/opt/bmas/api_server.py
scp /opt/bmas/agent/requirements.txt root@NODE_IP:/opt/bmas/requirements.txt

# On the node: install dependencies
ssh root@NODE_IP 'cd /opt/bmas && pip install -r requirements.txt'
```

Each agent needs environment variables for its role. Set these in the systemd service (see below):

- `NODE_ID` — e.g. `agent-node1`, `agent-node2`, `agent-node3`
- `LITELLM_MODEL` — the default model to use (typically `medium`)
- `HERMES_SKILLS_DIR` — path to Hermes skills directory (default: `~/.hermes/skills`)

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

> **Note:** Set `NODE_ID` to `agent-node1`, `agent-node2`, or `agent-node3` depending on the node.

### Verify

```bash
curl http://localhost:8000/health
# Should return {"status": "healthy", "node_id": "agent-node1", ...}

# Skills are served by the Hermes Dashboard (port 9119), not the agent API
curl http://localhost:9119/api/skills/installed
# Should return installed skills (requires Hermes Dashboard to be running)
```

---

## Part 3: Register in bmas.yaml

Add the node to your `bmas.yaml` on the control plane:

```yaml
nodes:
  - name: "my-new-node"
    host: "192.168.1.101"      # Agent IP
    port: 8000
    role: executor              # or planner, auditor
    inference:
      host: "192.168.1.102"    # Inference server IP
      port: 8080
      model: "gemma-4-e4b"
```

Restart the control plane to pick up the new node:

```bash
docker compose restart daemon dashboard litellm
```

The new node should appear in the Mission Control dashboard.
