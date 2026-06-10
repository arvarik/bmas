[🏠 Index](../README.md) | [📂 Proposal Index](README.md) | [⬅️ Data Model](07-data-model.md) | [🗺️ Migration Plan](10-migration-and-rollout.md)

# 17 — Files & Artifacts: PDF Input, Storage Config, Agent-Created Output

> [!ABSTRACT]
> A chat-grade multi-agent system must do what a chat/harness CLI does with files: **accept uploads (PDFs, images, text) as task input**, and **write files as task output** (a codebase, a report, generated media). Today bMAS supports neither — there is no upload route, no storage config, and agent-created files die inside the node's Hermes workspace. This document specifies the complete file pipeline: the `storage.*` config on the brain node, the upload path with PDF extraction, how attachments reach agents on the edge nodes, and how agent-created artifacts sync back to a per-task output directory (e.g. `/opt/output/{task-name}/`). It is **variant-agnostic** infrastructure: the traditional core, PatchBoard, and stigmergic variants all ride the same pipeline.

---

## 1. Current state (verified)

- No `storage` block exists in `bmas.yaml` / `config.py`; no upload or file routes exist in `daemon/src/routes/` or `mission-control/src/app/api/`.
- The task composer submits text only (`POST /submit`).
- Agents *can* create files (Hermes has full file tools) — but only inside the node-local workspace, invisible to the daemon and the user.

## 2. Storage configuration (the brain node owns disk)

New required-when-enabled block in `bmas.yaml`, validated fail-fast in `config.py` (Phase 0):

```yaml
storage:
  enabled: true
  user_media_dir: "/opt/bmas-data/uploads"   # uploaded task inputs: {dir}/{task_id}/{filename}
  artifacts_dir: "/opt/output"               # agent-created outputs: {dir}/{task_slug}/…
  max_upload_mb: 50
  max_task_output_mb: 500
  allowed_upload_types: [pdf, txt, md, csv, json, png, jpg, docx]
  pdf_extraction: pymupdf                    # pymupdf (default) | pypdf | off
  extraction_max_chars: 60000                # cap on extracted text per file
```

Deployment notes (explicit, because a lower-capability implementer will follow them literally):

1. Both directories are **bind-mounted into the daemon container** in `docker-compose.yml` (`- ${BMAS_UPLOADS:-/opt/bmas-data/uploads}:/data/uploads`, `- ${BMAS_OUTPUT:-/opt/output}:/data/output`). `config.py` resolves container paths; `bmas.yaml` documents host paths. The daemon creates missing directories at startup and fails fast if they are not writable.
2. **Task output directory** = `{artifacts_dir}/{task_slug}/` where `task_slug` = slugified first 60 chars of the task title/query (`[a-z0-9-]`, collisions get `-2`, `-3`…). Created lazily on the first artifact. Recorded as `tasks.output_dir` ([07 §1.7](07-data-model.md#17-tasks-column-additions)). Example: user config `/opt/output` + task "Create a CLI todo app" → the codebase lands in `/opt/output/create-a-cli-todo-app/`.
3. The dashboard container needs **no** mount — all file access flows through daemon endpoints.

## 3. The upload path

```
UI composer (attach button / drag-drop)
   │  multipart/form-data
   ▼
Next.js  POST /api/tasks/[taskId]/files  ──proxy──►  Daemon  POST /tasks/{id}/files
                                                        │
                                                        ├─ validate: size ≤ max_upload_mb, type ∈ allowed,
                                                        │   sanitize filename, sha256
                                                        ├─ store {user_media_dir}/{task_id}/{filename}
                                                        ├─ extract text (PDF → pymupdf; txt/md/csv passthrough;
                                                        │   images: no extraction, metadata only)
                                                        ├─ insert task_files row (07 §1.5)
                                                        └─ emit SSE file_added
```

Two submission flows, both supported:

- **Attach-then-submit (primary):** the composer creates a draft task id on first attach (`POST /submit?draft=true`), uploads files against it, then submits the query. Genesis sees the files already staged.
- **Mid-task upload:** during a running task, an upload becomes a new `attachment` entry + a `directive` ("the operator added q3-earnings.pdf") so agents notice it next round.

**PDF extraction**, precisely: `pymupdf` (fitz) page-by-page text extraction, concatenated with page markers (`[page 3]`), truncated at `extraction_max_chars` with a `[truncated — fetch full file]` marker. Scanned/image-only PDFs yield empty text → the attachment entry says so and agents can fetch the raw file (nodes may have their own vision tools). No OCR in V1 (recorded as an open question, [10 §3 Q13](10-migration-and-rollout.md#3-open-questions-verify-before-building)).

## 4. Attachments on the board

Files become **first-class board state** so agents discover them the same way they discover everything else — by reading the board:

- At genesis (or mid-task), each file gets an `attachment` entry ([04 §1](04-blackboard-protocol.md#1-board-entries-typed-envelopes-natural-language-bodies)): `title` = filename, `body` = a daemon-generated stub — file type, size, page count, and the **first `attachment_preview_chars` (default 1500) of extracted text** — plus the instruction "fetch the full content via your attachments list."
- The full extracted text is **not** inlined into the board (it would blow the context budget and the Cleaner would have to fight it). It lives in `task_files` and is delivered on demand (§5).
- The turn payload's `attachments` array ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)) lists every file with its fetch URL.

## 5. Staging inputs to nodes

Agents run on edge LXCs and need file *contents*, not control-plane paths. The node agent (`agent/api_server.py`) handles staging in `prepare_workspace(req)` at turn start:

1. Create/reuse the node-local task dir: `/opt/bmas-workspace/{task_id}/` (config `WORKSPACE_DIR` in the node's `.env`), with `inputs/` and `outputs/` subdirs.
2. For each attachment not yet cached locally (by sha256): `GET {daemon}/tasks/{id}/files/{fid}` with the `BMAS_NODE_KEY` bearer → write to `inputs/{filename}`. Also write `inputs/{filename}.extracted.txt` when extraction exists (cheaper for the agent to read than re-parsing a PDF).
3. The turn input tells the agent where everything is: *"Task files are in `inputs/`. Write any deliverable files into `outputs/` — they will be collected automatically."*

This keeps nodes credential-free (bearer only), cache-friendly (sha256 dedupe), and works with Hermes's native file tools — no Hermes plugin needed.

## 6. Artifacts: agent-created files

The reverse path. After the run completes, the node agent diffs `outputs/` (and only `outputs/` — the rest of the workspace is scratch):

```
sync_artifacts(req, workspace):
  for each file under outputs/ that is new or whose sha256 changed since last turn:
     POST {daemon}/ingest/artifacts/{task_id}/{turn_id}   (multipart: rel_path, bytes, sha256; bearer auth)
  return the manifest for TaskResponse.artifacts (06 §3.1)
```

Daemon side, per synced file:

1. Validate: cumulative task output ≤ `max_task_output_mb`; `rel_path` is normalized and **must not escape** the task dir (reject `..`, absolute paths, symlinks).
2. Write to `{artifacts_dir}/{task_slug}/{rel_path}` (creating directories). Re-synced paths bump `version`; previous versions are kept as `.bmas-versions/{rel_path}.v{n}` (cheap, bounded by the quota).
3. Insert `artifacts` row ([07 §1.6](07-data-model.md#16-artifacts--agent-created-outputs-doc-17-6)); emit SSE `artifact_created`; post an `artifact` board entry (so *other agents see the file exists* — e.g. the Critic can review `src/main.py` next round by fetching it like an attachment).

So for "create a codebase" with `artifacts_dir: /opt/output`: the expert writes files into its node's `outputs/`, they sync to `/opt/output/create-a-cli-todo-app/src/…` on the brain node, appear live in the UI's artifact browser, and land on the board for peer review. Multi-node concurrency is safe because artifacts are path-namespaced per file and version-bumped on collision — two agents writing the *same* path in one round is visible in the UI as v1/v2, and resolving it is debate material (a `critique`), not a silent overwrite.

## 7. Limits, security, and retention

- **Path traversal**: every `rel_path` and filename passes one shared sanitizer (tests required: `..`, absolute, unicode tricks, symlink).
- **Quotas**: per-upload (`max_upload_mb`), per-task output (`max_task_output_mb`); the artifact ingest returns `413` past quota and the event is surfaced in the UI.
- **Types**: uploads restricted to `allowed_upload_types`; artifacts unrestricted by type (code is the point) but size-capped.
- **Auth**: UI routes use the existing dashboard session; node routes use `BMAS_NODE_KEY` bearer ([03 §4](03-target-architecture.md#4-what-each-turns-agent-payload-looks-like-target)). Files are never served unauthenticated.
- **Retention**: uploads and artifacts persist with the task; task deletion cascades DB rows and removes `{user_media_dir}/{task_id}` — but **never** auto-deletes `{artifacts_dir}/{task_slug}` (that is the user's deliverable; deletion is an explicit UI action with confirmation).

## 8. UI surfaces

Specified here, composed per [doc 13](13-ui-showcase-density.md)'s philosophy and [DESIGN.md](../design/DESIGN.md) tokens:

| Surface | Where | Behavior |
|:--|:--|:--|
| **Attach control** | task composer | drag-drop + button; per-file progress; type/size validation errors inline |
| **Attachments rail** | task header / Mission view | chip per file (icon, name, size); click → preview (extracted text / image) |
| **Artifact browser** | new `Artifacts` tab + Mission panel | live tree of `{task_slug}/`, file rows with author/turn/version chips, download file or zip-all; updates on `artifact_created` SSE |
| **Board nodes** | blackboard graph | `attachment` (paperclip) and `artifact` (file) node types ([08 §3](08-ui-blackboard-visualization.md#3-the-blackboard-graph)) |
| **Trace cross-link** | turn inspector | "produced 3 artifacts" footer linking into the browser ([09 §4](09-ui-agent-trace-inspector.md#4-turn-inspector-slide-over)) |

## 9. Acceptance criteria

- [ ] Upload a PDF in the composer → `attachment` entry appears at genesis with a non-empty preview; an agent's trace shows it fetched and used the extracted text.
- [ ] Submit "write a Python CLI todo app" with `artifacts_dir: /opt/output` → working files exist under `/opt/output/<task-slug>/` on the brain node; each file has an `artifacts` row, an `artifact` board entry, and is downloadable from the UI.
- [ ] A second agent critiques a synced artifact in a later round (proves the board entry + fetch path closes the loop).
- [ ] Path-escape attempts in `rel_path` are rejected and logged; quota breach returns 413 and surfaces in the UI.
- [ ] Task deletion removes uploads, preserves `/opt/output/<task-slug>/`.

➡️ Back to the [Migration Plan](10-migration-and-rollout.md) (Phase 5) or the [Proposal Index](README.md).
