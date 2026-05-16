# Roadmap — Mission Control

> Enhancements to the Mission Control dashboard, task management, and UI/UX.

## 🔴 High Priority

### Task-Centric UI Overhaul
Full redesign of Mission Control from a single-task monitoring dashboard to a multi-task, history-aware orchestration interface. Subsumes the previous URL routing item.

- [ ] SQLite data layer for persistent task history (replaces ephemeral Redis-only storage)
- [ ] Unified SSE architecture replacing 2-second polling
- [ ] New daemon API endpoints for task history, debate, cost, and logs
- [ ] Next.js App Router with proper URL routes and deep-linking
- [ ] Task history sidebar (replacing feature navigation)
- [ ] Conversational landing page for task submission
- [ ] Task detail page with tabbed sub-views (Overview, DAG, Logs, Blackboard, Cost)
- [ ] Permanent debate history preservation for future replay

## 🟡 Medium Priority

### Task History Deletion
Task history is currently immutable — tasks cannot be deleted from the SQLite database. Adding deletion support would give operators more control.

- [ ] Add `DELETE /tasks/{id}` endpoint to the daemon (cascading delete of sub_tasks, debate, cost, logs)
- [ ] Add delete button in the task detail page with confirmation dialog
- [ ] Respect the design system's `ActionButton` danger variant and confirmation pattern from `DESIGN.md` §5.6 and §7.1
