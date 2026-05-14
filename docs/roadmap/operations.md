# Roadmap — Operations

> Enhancements to operational workflows, data safety, observability, and extensibility.

## 🟡 Medium Priority

### HITL (Human-in-the-Loop) Improvements
- [ ] Support approval workflows for high-cost tasks
- [ ] Configurable approval thresholds per complexity tier
- [ ] Slack/Discord notifications for pending approvals

### SQLite Automated Backups
The daemon's task history is stored in SQLite (`/data/bmas.db`). Currently, backups rely on Docker volume management. A configurable automated backup would improve data safety.
- [ ] Add `storage.backup_interval` setting to `bmas.yaml` (e.g., `daily`, `weekly`, `never`)
- [ ] Use SQLite's built-in `.backup` API to create periodic snapshots to a configurable path
- [ ] Rotate old backups (keep last N)

## 🟢 Low Priority

### Multi-Tenant Support
- [ ] API authentication for Mission Control
- [ ] Per-user task isolation on the blackboard
- [ ] Role-based access control (RBAC)

### Observability
- [ ] OpenTelemetry integration for distributed tracing
- [ ] Prometheus metrics endpoint on the daemon
- [ ] Grafana dashboard templates
- [ ] Cost alerting (budget limits per complexity tier)

### Plugin System
- [ ] Custom agent types via plugin interface
- [ ] Custom triage classifiers
- [ ] Webhook integrations (on task complete, on error, etc.)
