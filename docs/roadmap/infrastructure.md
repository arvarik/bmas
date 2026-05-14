# Roadmap — Infrastructure

> Enhancements to deployment, networking, configuration management, and compute infrastructure.

## 🔴 High Priority

### Cloud-Based Triage
Currently, triage requires an NVIDIA GPU running a local vLLM instance. Users without GPUs should be able to use a cheap cloud model for complexity classification.
- [ ] Add `triage.provider` field to `bmas.yaml` (e.g., `local`, `cloud`)
- [ ] When `provider: cloud`, use a configured model (e.g., Gemini Flash Lite) via LiteLLM
- [ ] Maintain the same triage prompt and complexity enum
- [ ] Update documentation

## 🟡 Medium Priority

### Multi-Node Load Balancing for `local` Routing
When routing to `local`, currently maps to `edge-node-1` only. LiteLLM supports load balancing across multiple model deployments with the same name.
- [ ] Register all edge nodes under a shared `local` model group in LiteLLM
- [ ] Support round-robin, least-busy, and latency-based routing strategies
- [ ] Expose routing strategy in `bmas.yaml`

### Automated Node Provisioning (`bmas provision`)
Currently, users must manually set up Hermes agents and llama.cpp on each edge node. A provisioning CLI would automate this via SSH.
- [ ] Design `bmas provision` CLI interface
- [ ] Support SSH-based provisioning (Ansible, Fabric, or plain SSH)
- [ ] Auto-install llama.cpp with Vulkan support
- [ ] Auto-download and configure inference models
- [ ] Auto-install and configure Hermes agent with persona
- [ ] Create systemd service files on each node

### Config Hot Reload
Currently, changing `bmas.yaml` requires `docker compose restart`. Components should detect changes and reload automatically.
- [ ] File watcher in daemon (`watchdog` or inotify)
- [ ] File watcher in Mission Control (chokidar or fs.watch)
- [ ] Graceful reload without dropping active tasks

### Redis Key Prefix Configurable
Currently hardcoded to `bmas:`. Making it configurable allows multiple deployments to share a single Redis instance.
- [ ] Add `project.redis_prefix` to `bmas.yaml`
- [ ] Thread prefix through all Redis operations in `blackboard.py`
- [ ] Thread prefix through all API routes in Mission Control

## 🟢 Low Priority

### Kubernetes Support
- [ ] Helm chart for bMAS control plane
- [ ] Kubernetes operator for managing agent nodes
- [ ] Horizontal pod autoscaling for inference nodes
