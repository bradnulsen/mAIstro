# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop, faithful execution pipeline, hardened extensibility surface, autonomous self-monitoring, structurally isolated task workspaces, and an async-throughout backend. The execution loop is solid; the structural debt that slowed iteration is paid down. What remains is not "make it work" but "make it learn."

What's in place:
- **Per-task git worktrees** — every task runs in `.maistro/worktrees/task-<id>/` on its own `<job-slug>/task-<id>` branch. The operator's main checkout is structurally untouchable. Successful integrations fast-forward into main and clean up; non-success terminals preserve the worktree+branch with discard / manual-merge controls. Operator-wins conflict policy.
- **`tasks` / `task_executions` split** — intrinsic identity + queue placement on `tasks`; per-execution outcome data (session, commits, stop_reason, num_turns, cost, timestamps, worktree, branch) on `task_executions`. `tasks_resolved` view + `get_task_resolved` route per-task reads through subordinates.
- **Async-throughout backend** — `git.py` async via `asyncio.to_thread`; 60+ awaiting callsites across worker, scheduler, dispatch, governor, routes. Glob matching extracted to `matching.py`.
- **Activity Dashboard (commits-centric)** — Job Health (task outcomes) + Job Impact (commits, lines, files in window) + Timeline + scrollable commit history with diff drill-down. Feed view absorbed; one place answers both "what ran" and "what shipped."
- **Unified Dispatch kanban** — three-column layout (Upcoming / Active / Resolved) with drag-based merge / split / transfer / reorder. Resolved column floats tasks with preserved worktrees to the top so unfinished business is visible. Bottom detail drawer with WorkspaceBanner exposing path, branch, manual-merge command, and confirm-gated discard.
- **Outcome summaries** — auto-derived from commits, displayed inline, forwarded to downstream agents in dependency context.
- **Six trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`, circular chains permitted), agent (imperative cross-job dispatch), reply / resume.
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses.
- **Dispatch continuity** — resume reuses the original worktree; retry for failed/timed-out dispatches; reply for follow-up context on resolved tasks.
- **Live dispatch streaming** — SSE with real-time text, tool use, thinking blocks, and execution metadata; incremental per-event flush.
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill.
- **Coalescing** — trigger-specific and global modes; manual queue composition (merge/split) in pending; agent triggers coalesce like other types. Depth-1 enforced by SQLite triggers.
- **Internal MCP server** — platform-hosted, context-aware tool surface with git operations (including branch ops), file access, task info, branch management, and inter-agent dispatch.
- **External MCP onboarding** — registration validation, pre-dispatch health checks with named error attribution, cascade deletion, disabled-server enforcement, and **drag-and-drop `.mcpb` bundle import** in the MCP Servers UI.
- **Three-dimensional tool control** — `allowed_tools` (CLI), `allowed_internal_tools` (internal MCP), `mcp_servers` (external MCP) compose independently with discoverable inventories and health indicators.
- **Inter-agent dispatch** — `dispatch_task` MCP tool, `get_queue_status`, `allowed_dispatch_targets` with depth limiting and loop prevention.
- **Governor (one-way findings, current shape)** — autonomous meta-analysis agent triggered every 10 executed terminal tasks (or manually). Reads jobs / recent tasks / health / git log; emits findings (suggestions / observations) for the operator to approve, decline, or execute via a dedicated write-mode invocation.
- **Files, Settings** — supporting surfaces.

## Diagnosis

The platform's *operational* loop is mature: tasks dispatch reliably, run in isolation, surface their work faithfully, and clean up after themselves. Structural debt is paid down. External MCP onboarding is now self-service via `.mcpb` drag-drop. Dashboard answers the two questions that matter operationally — "what ran" and "what shipped."

What the platform does *not* yet do well: **learn**. The feedback loops that turn lived experience into better future behavior are either absent or one-directional:

- **Operator ↔ Governor is one-way.** The Governor speaks (findings); the operator can only assent or decline. Real meta-management is a conversation — push-back, clarification, "yes but in this case do X." Right now an operator who disagrees with a finding has nowhere to put that disagreement; the Governor never sees the rebuttal, never refines its read of the project.
- **Agents could not write to their own instructions.** *(Closed: the `job_learnings` surface ships with both operator and gated agent CRUD, and dispatch queries learnings on demand rather than auto-injecting.)*

A separate concern, orthogonal to learning, is **reach**: today mAistro ships as a developer setup (`pip install`, `npm install`, two terminals). The operators who would benefit most — non-developer subject-matter experts coordinating with autonomous agents — can't run it. An installer closes that gap.

External MCP polish (env-var UI, server status in dispatch detail, runtime error attribution, config validation) and structural item R6 (EAV-flatten to JSON column) are real but not load-bearing. They live in Deferred with explicit revisit triggers.

## Guiding Policy

**Close the meta-management loop, then open the reach.** The platform's execution surface is mature, and the agent-writable side of the learning loop has shipped. The remaining structural change is the two-way meta-management surface (Governor threads); after that, an installer makes the platform usable by the operators who most need it.

The order from here: Governor threads first (so the operator and the Governor can actually have a conversation, and the Governor can propose learnings via thread `action_payload`s rather than findings); installer second (depth before breadth — shipping to a non-developer audience while the Governor surface is one-way would lock in a shape we're already replacing).

Task session interrogation was previously sequenced here as P2; it has been **dropped** as a planned priority. The "let the operator chat with a completed task's resumed agent" framing fails the value-vs-cost test once you notice that (a) the resumed agent confabulates rather than recalls, (b) the dispatch primitive already handles "ask the agent about its work" via a fresh task with read-only context, and (c) the multi-turn human↔agent thread primitive lands in P1 anyway. If a real need surfaces, revisit then — likely as a "thread on a task" reusing P1's infrastructure rather than as its own subsystem.

## Priority 1: Governor Threads

**The problem:** The Governor surfaces findings; the operator approves, declines, or executes. There is no return channel. The operator cannot ask the Governor a question, cannot push back on a misreading, cannot refine a proposal through conversation. Findings are static artifacts in a feed — read, dismiss, or commit. This shape was right when the Governor was a proof of value (does autonomous oversight surface anything useful?). Now that the answer is yes, the missing piece is dialogue: the Governor's analysis is most valuable when it can be challenged, scoped, and iterated.

**Why first:** Governor threads reshape the conversational surface that learning proposals will eventually flow through. With learnings already shipped, the Governor will gain a natural path to propose them via thread `action_payload`s ("I noticed jobs without explicit error-handling guidance fail more — propose adding...") instead of one-shot findings.

**Specifically:** see [governor-threads proposal](architecture/proposals/governor-threads.md) for the full design. The shape:
- **Threads** replace findings as the persistent unit of Governor↔Human exchange. Each thread is a meta-management discussion with a status (open / closed), an opener (governor / human), and an ordered append-only message list.
- **Two invocation types**: `survey` (the existing 10-task auto trigger, reframed — the Governor either updates open threads or opens new ones) and `reply` (any human action in a thread — auto-spawned on thread creation, on a reply post, or coalesced into a pending invocation).
- **Approvals collapse into prose.** No Approve / Decline buttons. A Governor message may carry a structured `action_payload` (proposal); the human responds in the compose box; the next reply invocation reads the conversation, decides whether the human's intent is clearly affirmative, and writes via MCP if so. Reply invocations are uniformly write-capable; the Governor's discretion (informed by the system prompt) decides when to use the write tool.
- **Closed = muted.** Closing a thread is human-only and excludes that thread entirely from future Governor context. Reopening is human-only too.
- **Constructive write surface.** `update_job_properties`, `create_job`, `update_queue_settings`. No `delete_job`, no `disable_job` — destruction is operator-only.
- **Per-thread coalescing on replies.** If a reply invocation is queued or in flight for thread X and a new human message arrives on X, it merges into the pending invocation. One Governor message covers everything; the latest human message is operative.

Schema: `governor_findings` is dropped; `governor_threads` and `governor_messages` replace it. Per the no-migration-system policy, the dev DB is recreated; an optional `migrate_db.py` script can fold each existing finding into a single-message thread.

**In flight:** P1 backend (schema, db helpers, routes, governor.py refactor, MCP server) shipped. The frontend (`Governor.jsx` rewrite) and architecture-doc rewrite (`architecture/governor.md`) are the remaining steps.

## Priority 2: Installer Distribution

**The problem:** mAistro ships as a developer setup — clone the repo, `pip install -r requirements.txt`, `npm install`, `python run.py` in one terminal and `npm run dev` in another. The operators who would benefit most — non-developer subject-matter experts coordinating with autonomous agents on a single project — cannot follow that recipe. The platform's deployment shape (single user, single project, localhost) was always meant to ship to end users; what's missing is the artifact that puts it in their hands.

**Why second:** Depth before breadth. Shipping an installer whose UX still has one-way findings locks in a shape that's about to change. Once P1 lands, the product is ready for the audience the installer opens up.

**In flight:** The app-DB path has been relocated to user app-data with a `MAISTRO_APPDATA` env override for dev runs (P4 step 1). Remaining: PyInstaller spec + smoke test, Inno Setup script, Claude CLI detect-and-prompt, single-instance launcher.

**Specifically:** see [installer-distribution proposal](architecture/proposals/installer-distribution.md) for the full design. v1 shape:
- **PyInstaller-bundled backend + browser-based UI.** Lowest mechanical cost; backend serves `frontend/dist/` and the user opens `localhost:8420`. Native window (Tauri) is v2 if browser-tab UX proves unacceptable. Electron stays a non-goal.
- **Windows-first Inno Setup installer.** macOS / Linux deferred until Windows is stable.
- **Per-user data relocation.** App-DB moves from `<repo>/.maistro/app.db` to `%APPDATA%\mAistro\app.db` (Windows) / `~/Library/Application Support/mAistro/` (macOS) / `~/.local/share/mAistro/` (Linux). Single change point in `appstate.py` with `MAISTRO_APPDATA` env override for dev runs. Project DBs stay at `<project>/.maistro/maistro.db` — git-aware, the right shape regardless of distribution.
- **Claude CLI: detect-and-prompt.** CLI is required, can't be embedded, requires user's auth. Installer detects, shows setup screen if missing, surfaces a CLI status indicator in the UI alongside MCP server health pills.
- **Single-instance behavior.** Second launch detects the existing 8420 binding and opens the browser to it.
- **Auto-start, system tray, autoupdate: v2.** First installer's job is "make it runnable."

**Second-order effects:** Once the installer ships, the README's Quick Start needs a parallel "end-user" section alongside the current developer one. Schema upgrades that include data changes still use the bespoke-script-via-`migrate_db.py` pattern; the installer ships those scripts and the UI exposes them as upgrade actions. Multi-project orchestration becomes more defensible to defer once the deployment shape is locked as single-user-single-project.

## Deferred

Valuable but deliberately postponed:

- **External MCP polish (residual)** — environment variable UI in the server config form, server status surfaced in the dispatch detail view, runtime error attribution for in-CLI MCP failures, JSON validation of assembled MCP config before write. The biggest onboarding gap closed with `.mcpb` drag-drop import; remaining items are bounded polish that can land opportunistically without P-sequencing. Revisit when an operator hits one of these gaps in real use.
- **R6 structural EAV flatten** — replace `job_property_defs` + `job_properties` with a `job_config` JSON column on `jobs`. The minimal fix shipped (cached schema, shared assembly helpers); the structural option is a medium migration with no urgency. Revisit when property-shape changes, a real perf budget, or operator-extensible properties become a goal.
- **Bash restriction** — structured MCP tools as the default with Bash as a governed escape hatch. The tool surface is broad enough that agents can do most coordination without Bash; `allowed_internal_tools` makes restriction possible. Revisit once usage patterns reveal whether agents still rely on Bash for operations that could be structured tools.
- **Parallel dispatch** — concurrent execution of multiple tasks. The structural blocker (shared working tree) is gone; remaining concerns are per-task DB transaction discipline, concurrent SSE streams, and MCP session capacity. Revisit once the dashboard reveals whether sequential processing is a real bottleneck.
- **Auto-evaluation** — LLM judges of dispatch output quality. The Governor provides a lighter-weight version (pattern-based analysis without a calibration dataset). A dedicated evaluator with structured scoring stays deferred until task volume justifies it.
- **Prompt effectiveness tracking** — correlate job instruction changes with task success rates. Partially addressed by the Governor's scope-drift and effectiveness analysis. Formal A/B-style tracking deferred until enough task history accumulates.
- **Desktop / webhook notifications** — browser Notification API and outbound webhooks. The Governor subsumes the high-level "proactive insight" need; low-level event notifications may be valuable at scale but are deferred until Governor-based oversight proves insufficient.
- **Conditional dependencies** — "only run if upstream output matches X." Adds significant complexity to the trigger model for a use case that hasn't surfaced.
- **Structured output passing** — typed data exchange between tasks beyond outcome summary text. Current approach (commit messages + diff stats forwarded as text) works for most coordination. Full structured passing requires a data contract model that adds complexity without proven demand.
- **Multi-project orchestration** — cross-project dispatch. Becomes harder to justify once the installer locks single-user-single-project as the deployment shape; would need careful design around DB isolation and project-context state.
- **Project context injection (R10)** — replace `PROJECT_DIR` global with a `ProjectContext` object. Per [installer-distribution.md](architecture/proposals/installer-distribution.md) this is policy-locked, not technical debt: single-tenant is the deployment shape.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — `useState` / `useEffect` is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron specifically** — fat bundle and a redundant browser runtime aren't justified. Tauri stays an open option for v2 of installer distribution.
- **Custom agent runtime** — Claude CLI subprocess model works. The MCP server extends it rather than replacing it.
- **Full Bash removal** — agents need escape hatches. The aim is structured alternatives that agents prefer, not a sandbox that breaks them.
- **Auto-distillation of learnings from task outcomes** — the Governor or a future evaluator can *propose* learnings via the same write tools as the agent, but no automatic synthesis pipeline ships in v1 of learnings.
- **RAG / vector retrieval over learnings or descriptions** — learnings concatenate directly into the prompt. Retrieval-based selection is deferred until prompts grow large enough to justify the complexity.
- **Multi-tenant / hosted SaaS** — different proposal entirely. The installer locks in single-user single-tenant as the deployment shape.

## Completed

- **Job Learnings** (was P3) — per-job, individually-toggleable rules complementing the prose `description`. Operator and gated agent CRUD via internal MCP. v2 follow-up: capped list (`max_learnings`, default 10) with per-row size cap, switched from auto-injection to query-on-demand (`list_learnings` returns id+summary, `read_learnings` fetches full bodies for selected ids), system prompt directs the agent to use them. Cap acts as a forcing function for consolidation over append.
- **Triggers/Dispatches Stage 1 rename** — Python identifiers, frontend props/wrappers, API URLs, and the `MAISTRO_TRIGGER_ID` env var moved from `task`/`task_execution` to `trigger`/`dispatch`. SQL tables stay legacy (`tasks` / `task_executions` / `task_events`); Stage 2+ structural promotion of dispatches as a first-class entity remains deferred.
- **Task Workspace Isolation** (was P1) — three-phase delivery now fully shipped. Phase 1: CLI subtype mapping (`error_max_turns` → `stop_reason: max_turns`) so turn-limit failures classify as `exhausted`, not `completed`. Phase 2: stash-on-orphan safety net for non-success terminals with a dirty working tree. Phase 3: per-task git worktrees at `.maistro/worktrees/task-<id>/` on `<job-slug>/task-<id>` branches. Terminal handlers reconcile via git: `completed` fast-forwards into main and removes the worktree; non-success preserves with discard / manual-merge controls. Operator-wins conflict policy. WorkspaceBanner exposes path, branch, manual-merge command, and confirm-gated `POST /api/tasks/{id}/workspace/discard`. Resume reuses the original worktree. Resolved kanban floats tasks with preserved worktrees to the top.
- **`tasks` / `task_executions` split** — intrinsic identity + queue placement on `tasks`; per-execution outcome data on `task_executions`. `tasks_resolved` view + `get_task_resolved` route per-task reads through subordinates. Recovery from / prevention of `tasks_old` FK corruption.
- **Async git operations (R7)** — `git.py` async via `asyncio.to_thread`; ~60 callsites across worker/scheduler/dispatch/governor/route modules await them.
- **Glob matching extracted (R8)** — `_any_file_matches` and `_glob_to_regex` moved from `dispatch.py` to a dedicated `backend/matching.py`.
- **EAV property assembly dedup (R6 minimal)** — `_get_property_schema()` caches the `(default_props, def_types)` tuple; `_overlay_properties()` is the single overlay helper. `get_job`, `list_jobs`, `get_cascade_targets` all share the helpers. Structural flatten remains deferred.
- **Activity Dashboard reorientation** (was implicit P2) — Job Health + Job Impact (commits, lines, files in window) + Timeline + commit history with diff drill-down. Feed view absorbed into Dashboard; Feed rail entry removed. Dispatch Chains and Tool Usage sections deleted.
- **`.mcpb` bundle drag-and-drop import** — operators can drop a Claude Desktop `.mcpb` bundle onto the MCP Servers page to register it without copy-pasting JSON config.
- **Default `max_turns` raised from 50 to 100** — observed task volumes no longer hit 50 as a soft ceiling; 100 is the new practical default.
- **Resume reuses original worktree** — workspace-action gating tightened.
- **Project name in command bar; default to Dispatch view** — minor wayfinding polish.
- **Dispatch Outcome Summaries** — auto-derived outcome summaries from commit history, injected into dependency trigger context.
- **Two-Stage Queue** — pending (curation) and queued (execution runway) with specialized drag operations.
- **Unified Dispatch View** — three-column kanban with bottom detail drawer; replaces the earlier History tab.
- **Terminal State Differentiation** — six distinct terminal states with clear behavioral semantics. Only `completed` (no error) triggers downstream dependencies.
- **Job Colors** — visual identifiers from a curated palette across all job-related surfaces.
- **Internal MCP Server** — context-aware tool surface with git operations, read-only project context tools, tool invocation logging, task-specific surfaces.
- **Files View** — project file browser with glob search, syntax highlighting, markdown rendering.
- **Contextual Help Tooltips** — hover tooltips on configuration fields.
- **Circular Dependencies** — `depends_on` permits cycles; coalescing absorbs redundant triggers.
- **Design Tokens** — consolidated hardcoded colors into CSS custom properties.
- **Approval Gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses.
- **Project Switch Safety** — block project switch/close during active dispatch.
- **Schema Cleanup** — definitive schema with no migrations or legacy fallbacks.
- **Trigger Context** — structured `triggers` JSON array with pre-formatted context strings.
- **Task Dependencies** — `depends_on` property, `dependency` trigger type with coalescing.
- **Watch Semantics** — subscriptions presence = watch active.
- **Settings and Configuration UI** — queue behavior, default model, default timeout, MCP server management.
- **Tool Discoverability** — platform presents available CLI tools, internal MCP tools, and external MCP server tools as selectable options.
- **Platform Tool Governance** — `allowed_internal_tools` per-job property, three-dimensional tool composition, git branch operations, branch audit trail via MCP tool call logging.
- **Inter-Agent Coordination** — `dispatch_task` MCP tool with `agent` trigger type, `get_queue_status`, `allowed_dispatch_targets` per-job property, dispatch attribution, self-dispatch prohibition, loop prevention, configurable depth limiting.
- **External MCP Robustness** — registration validation, pre-dispatch health checks gating task execution with named server error attribution, cascade deletion, disabled server enforcement at dispatch, structured args storage, health indicators on job config.
- **Execution Pipeline Fidelity** — thinking blocks captured and streamed/persisted, execution metadata (stop_reason, num_turns, cost_usd) extracted from CLI result events and stored on `task_executions`, max-turns exhaustion distinguished from success, incremental per-event flush during streaming.
- **Job Identity Model** — INTEGER PRIMARY KEY AUTOINCREMENT with slug column derived from name. Slug used for git authorship and branch naming.
- **Route Modularization (R9)** — every route group lives in its own module; `main.py` is lifespan + CORS + `include_router` calls.
- **Database Module Split (R2)** — `database.py` decomposed into seven domain modules; legacy migrations isolated in `db_migrations`.
- **Coalescing Consolidation (R5)** — write-side helpers consolidated in `db_tasks.py`. Depth-1 invariant enforced by SQLite triggers. Read-side resolution via `tasks_resolved` view.
- **Project Switch DB Coordination (R3)** — readers-draining protocol; prevents mid-task project switch race conditions.
- **Dispatch Diff View** — `start_commit` / `result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue, reply with follow-up context.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Task Status State Machine** — authoritative `status` column with validated transitions, batch cascading for coalesced tasks, automatic timestamp management via `transition_task()`.
- **Event-Sourced Task Lifecycle** — `task_events` table with dual-write. Foundation for Governor analysis and audit trail.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
- **Governor (one-way findings)** — autonomous internal meta-analysis agent replacing the standalone Chat feature. Auto-trigger via worker counter (every 10 executed terminals, excluding cancelled / rejected) or manual. Read tools always available; write tools (`update_job_properties`, `create_job`, `delete_job`, `update_queue_settings`) gated by execution mode. Structured findings: suggestions (approve / decline / execute) and observations (read / dismiss). Governor view on left nav with findings feed, approval actions, manual trigger, run history, badge count. *Reshaped into threads in P1.*
