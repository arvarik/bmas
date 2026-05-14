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

Each agent needs a persona file that defines its bMAS role. Create one based on the role
you assigned in `bmas.yaml`:

- `planner` — Focuses on task decomposition and DAG construction
- `executor` — Focuses on implementation and research
- `auditor` — Focuses on review, validation, and conflict resolution

### Start the Agent API Server

```bash
python api_server.py --host 0.0.0.0 --port 8000 --persona planner
```

### Create a systemd Service

```bash
cat > /etc/systemd/system/hermes-agent.service << 'EOF'
[Unit]
Description=Hermes bMAS Agent
After=network.target

[Service]
Type=simple
User=agent
WorkingDirectory=/opt/hermes-agent
Environment="PATH=/opt/hermes-agent/.venv/bin:/usr/bin"
ExecStart=/opt/hermes-agent/.venv/bin/python api_server.py \
  --host 0.0.0.0 --port 8000 --persona planner
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now hermes-agent
```

### Verify

```bash
curl http://localhost:8000/health
# Should return {"status": "ok"}

curl http://localhost:8000/skills
# Should return the agent's available skills
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
