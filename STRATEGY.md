# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop, operational visibility, and a hardened extensibility surface. The platform dispatches through a two-stage queue, streams output with thinking blocks and execution metadata, enforces timeouts, manages approvals, and records everything with incremental persistence. External MCP integration validates at registration, health-checks at dispatch, and cascades cleanup on deletion. The execution pipeline captures what agents actually do — thinking, cost, turns, stop reason — instead of silently dropping it.

What's in place:
- **Unified Dispatch kanban** — three-column layout (Upcoming / Active / Resolved) with drag-based merge, split, transfer, reorder, and bottom detail drawer for task inspection
- **Activity Dashboard** — job health summaries, task timeline, agent dispatch chain visualization, tool usage patterns, configurable time windows
- **Outcome summaries** — auto-derived from commits, displayed inline, forwarded to downstream agents in dependency context
- **Job colors** — visual identifiers distinguishing jobs across all surfaces
- **Five trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`, circular chains permitted), agent (imperative cross-job dispatch)
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses
- **Dispatch continuity** — resume (CLI `--resume`), retry for failed/timed-out dispatches, reply for follow-up context on resolved tasks
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, thinking blocks, and execution metadata
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — trigger-specific and global modes; manual queue composition (merge/split) in pending; agent triggers coalesce like other types
- **Internal MCP server** — platform-hosted, context-aware tool surface with git operations, file access, task info, branch management, and inter-agent dispatch
- **External MCP robustness** — registration validation (command existence, args/env type checking), pre-dispatch health checks with named error attribution, cascade deletion removing stale job references, disabled server enforcement at dispatch
- **Three-dimensional tool control** — `allowed_tools` (CLI), `allowed_internal_tools` (internal MCP), `mcp_servers` (external MCP) compose independently with discoverable inventories and health indicators
- **Execution pipeline fidelity** — thinking blocks streamed and persisted, execution metadata captured (stop_reason, num_turns, cost_usd), incremental event flush during streaming, max-turns exhaustion distinguished from success
- **Inter-agent dispatch** — `dispatch_task` MCP tool, `get_queue_status` for situational awareness, `allowed_dispatch_targets` with depth limiting and loop prevention
- **Files, Feed, Settings** — complete supporting surfaces

## Diagnosis

The platform has execution capability, operational visibility, a hardened extensibility surface, and a faithful execution pipeline. External MCP integration — the mechanism that makes agents useful beyond built-in tools — now validates at registration, health-checks before dispatch, and cleans up stale references. The execution pipeline captures thinking blocks, execution metadata (cost, turns, stop reason), and flushes events incrementally. The CLI execution path is a single implementation, not duplicated.

Three gaps remain, in order of urgency:

**Autonomous self-monitoring.** The dashboard answers "how are my agents doing?" when the operator looks — but the operator must look, and must synthesize patterns across jobs and tasks manually. The standalone chat interface attempted to bridge this gap but was the wrong abstraction — the operator doesn't want to interrogate the system conversationally, they want the system to surface what matters proactively. The Governor agent replaces chat with autonomous analysis: it runs periodically, synthesizes patterns, and produces actionable findings. **Design complete** — trigger cadence, finding types (suggestions/observations), approval-driven execution, dedicated MCP tool surface, and UI all specified in DESIGN.md.

**Outcome interrogation.** When a task completes, the user can see the outcome summary, diff, and dashboard aggregates — but cannot ask follow-up questions. "Why did you change this file?" "What alternatives did you consider?" The agent's session is locked behind the streamed output log. **Design complete** — session resume via `--resume`, read-only tool stripping, all specified. No remaining blockers — the shared CLI path and execution metadata that were prerequisites are in place.

**Structural debt.** `database.py` is a ~2000 line monolith covering schema, CRUD, task lifecycle, coalescing, chat, config, and dashboard queries. Route extraction from `main.py` is partial. Git operations use synchronous `subprocess.run`, blocking the async event loop. None of this blocks specific features, but it slows every future change and makes the codebase harder to reason about.

Additionally, external MCP has remaining polish items: no environment variable UI in the frontend (backend supports it, operators must set env vars externally), no server status surfaced in the dispatch detail view, and no JSON validation of assembled MCP config before writing. These are real gaps but bounded — the foundation is solid.

## Guiding Policy

**Ship the operator experience, then clean the internals.** The extensibility surface is now trustworthy and the execution pipeline is faithful. The remaining barriers are about the operator's relationship with the system: can the system self-monitor and surface insights (Governor), and can the operator interrogate what agents did (session interrogation). These are the features that make autonomous operation viable — without them the operator must watch dashboards and read raw logs. After operator experience: clean the internal structure that slows every future change.

## Priority 1: External MCP Polish

**The problem:** External MCP's foundation is solid — registration validates, health checks gate dispatch, stale references cascade on deletion, disabled servers are enforced. But the frontend is missing environment variable controls, the dispatch detail view doesn't surface which MCP servers were included, and the assembled config JSON has no validation before it's written to disk.

**Why first:** These are bounded, high-value completions of work already in progress. Env var UI is the most impactful — most external MCP servers require API keys, and without frontend controls the operator must set env vars outside the platform. Finishing these items makes external MCP fully self-service.

**Specifically:**
- **Environment variable UI** — add key-value pair management to the MCP server configuration form in `McpServers.jsx`. Add/remove controls, masked value display for secrets. The backend already stores and passes env vars; the frontend gap is the only thing forcing operators outside the platform for API-key-authenticated servers.
- **Server status in dispatch context** — the task detail view should show which external MCP servers were included in the dispatch config. When a task fails, the operator can immediately see whether the failure correlates with a server issue. Currently the detail drawer shows task metadata but nothing about the MCP server environment.
- **Runtime error attribution** — pre-dispatch health check errors are already attributed to specific servers by name. During CLI execution, MCP-related errors are still generic. Parse CLI error output for MCP connection failures and surface them with server name and failure mode.
- **Config file robustness** — validate the assembled MCP config JSON before writing to the temp file. Catch malformed args/env at config assembly time with a specific error rather than letting it propagate as an opaque CLI failure.

**Second-order effects:** A fully self-service MCP configuration means operators can onboard external tools without leaving the UI. This is the last step before the extensibility surface is genuinely production-ready.

## Priority 2: Task Session Interrogation

**The problem:** Completed tasks produce outcome summaries and diffs, but the user cannot ask follow-up questions. "Why did you change this file?" "What alternatives did you consider?" This requires conversational access to the agent's session — the same context, the same reasoning chain. Currently the user must read the raw streamed output log, which is noisy and non-interactive.

**Why second:** This is the highest-leverage feature for operator understanding of individual task outcomes. The Governor surfaces system-level patterns; session interrogation lets the operator drill into specific tasks. Together they provide both macro and micro visibility.

**Specifically:**
- **Session resume from Dispatch** — completed tasks in the Resolved column gain a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. The agent can read files, search code, and reason about its prior work, but cannot modify the project.
- **Gating new work through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel.

**Second-order effects:** Interrogation provides a natural feedback channel. The Governor can reference task sessions it has analyzed; the operator can verify Governor suggestions by interrogating the tasks the Governor flagged.

## Priority 3: Structural Remediation

**The problem:** The backend has accumulated structural debt that slows every future change. `database.py` is a ~2100 line monolith covering schema, CRUD, task lifecycle, coalescing, chat, config, governor, and dashboard queries. Route extraction from `main.py` is partial — project, feed, git, dashboard, and config routes remain inline. Git operations use synchronous `subprocess.run`, blocking the async event loop. Glob matching functions live in the prompt-assembly module instead of with git utilities.

**Why third:** None of this blocks specific features — P2 can ship on the current structure. But every change to the database layer requires navigating 1900 lines. Every new route group added to `main.py` makes the file harder to scan. Every git call blocks the event loop for the duration of the subprocess. This is the kind of debt that doesn't cause incidents but makes everything take longer.

**Specifically:**
- **R2: Split database.py** — 1900+ lines covering schema, CRUD, task lifecycle, coalescing, chat, config, dashboard, and property casting. Split into `db_core.py` (connection, schema), `db_jobs.py` (job CRUD, property system), `db_tasks.py` (task CRUD, state transitions, coalescing, queue queries), `db_chat.py` (sessions, messages, events), `db_config.py` (config, MCP servers), `db_dashboard.py` (aggregation). Re-export from `database.py` for backward compatibility.
- **R5: Consolidate coalescing** — fold into R2. Extract `coalesce_into`, `decoalesce`, `cascade_completion` as explicit functions in `db_tasks.py`. Add depth-1 CHECK constraint.
- **R9: Complete route extraction** — extract remaining route groups from `main.py`: project, feed, git, dashboard, config. `main.py` becomes lifespan + CORS + `include_router` calls + health check.
- **R8: Move glob matching** — `_any_file_matches` and `_glob_to_regex` are trigger-matching functions living in `dispatch.py`. Move to `git.py` or a dedicated `matching.py`. Trivial.
- **R7: Async git operations** — all git functions use sync `subprocess.run`, blocking the event loop. Wrap in `asyncio.to_thread`, make all callers `await`. Mechanical but wide-reaching.

**What's deferred from the remediation plan:** R6 (flatten EAV properties to JSON column) — not blocking. R10 (replace global `PROJECT_DIR` with `ProjectContext`) — only justified if multi-project becomes a requirement.

**Second-order effects:** R2 makes every future schema change lower risk. R9 makes `main.py` scannable. R7 eliminates a class of event-loop stalls that could become visible as task volume grows.

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. The tool surface is now broad enough (branch ops, dispatch, queue status) that agents can do most coordination without Bash. The `allowed_internal_tools` mechanism makes restriction possible. Revisit once real usage patterns reveal whether agents still rely on Bash for operations that could be structured tools.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, concurrent MCP sessions). Branch tools are now implemented, which removes the safety prerequisite. Revisit once the dashboard reveals whether sequential processing is a real bottleneck and once the orchestration pattern sees real use.
- **Auto-evaluation** — LLM judges dispatch output quality automatically. The Governor (P2) provides a lighter-weight version of this — pattern-based analysis of job effectiveness without a formal calibration dataset. A dedicated evaluation agent with structured scoring remains deferred until task volume justifies it.
- **Prompt effectiveness tracking** — correlate job instruction changes with task success rates. The Governor partially addresses this through its scope-drift and effectiveness analysis. A formal A/B-style tracking system remains deferred until enough task history accumulates.
- **Desktop/webhook notifications** — browser Notification API and outbound webhooks for real-time alerting on task events. The Governor subsumes the high-level "proactive insight" need. Low-level event notifications (individual task failures, approval requests) may still be valuable at scale but are deferred until Governor-based oversight proves insufficient.
- **Conditional dependencies** — "only run if upstream output matches X." Adds significant complexity to the trigger model for a use case that hasn't surfaced yet.
- **Structured output passing** — typed data exchange between tasks beyond the current outcome summary in trigger context. The current approach (commit messages + diff stats forwarded as text) works for most coordination. Full structured passing requires a data contract model that adds complexity without proven demand.
- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and global `PROJECT_DIR` state.
- **EAV property flattening** (R6) — replace the three-table EAV join with a JSON column on `jobs`. The assembly overhead is real but not blocking. Revisit if property queries become a measurable bottleneck.
- **Project context injection** (R10) — replace `PROJECT_DIR` global with a `ProjectContext` object. Large structural change only justified if multi-project becomes a requirement.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. The MCP server extends it rather than replacing it.
- **Full Bash removal** — agents need escape hatches. The aim is structured alternatives that agents prefer, not a sandbox that breaks them.

## Completed

- **Dispatch Outcome Summaries** (was P1) — auto-derived outcome summaries from commit history and outcome injection into dependency trigger context. The first feedback loop.
- **Two-Stage Queue** — pending (staging/curation) and queued (execution runway) with specialized drag operations: merge and split in pending, reorder in queued, transfer between columns.
- **Unified Dispatch View** — three-column kanban (Upcoming / Active / Resolved) with bottom detail drawer. Resolved column separates completed vs. non-success with status badges and inline error context. Replaces the earlier separate History tab.
- **Terminal State Differentiation** — six distinct terminal states with clear behavioral semantics. Only completed (no error) triggers downstream dependencies. Each non-success state implies a different user response.
- **Job Colors** — visual identifiers assigned on creation from a curated palette, displayed across all job-related surfaces.
- **Internal MCP Server** — platform-hosted, context-aware MCP server with git operations, read-only project context tools, tool invocation logging, and task-specific tool surfaces.
- **Files View** — project file browser with glob search, syntax highlighting for code, markdown rendering for documentation.
- **Contextual Help Tooltips** — hover tooltips on configuration fields (subscriptions, schedule, allowed tools, approval, coalescing, dependencies, timeout, auto-queueing, MCP servers, model).
- **Circular Dependencies** — `depends_on` permits cycles; coalescing absorbs redundant triggers.
- **Design Tokens** — consolidated hardcoded colors into CSS custom properties.
- **Approval Gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses.
- **Project Switch Safety** — block project switch/close during active dispatch.
- **Schema Cleanup** — definitive schema with no migrations or legacy fallbacks.
- **Trigger Context** — structured `triggers` JSON array with pre-formatted context strings.
- **Task Dependencies** — `depends_on` property, `dependency` trigger type with coalescing.
- **Watch Semantics** — subscriptions presence = watch active, no separate toggle.
- **Settings and Configuration UI** — queue behavior (auto-queueing/manual), default model, default timeout, MCP server management.
- **Tool Discoverability** — platform presents available CLI tools, internal MCP tools, and external MCP server tools as selectable options.
- **Platform Tool Governance** (was P2) — `allowed_internal_tools` per-job property, three-dimensional tool composition (CLI + internal MCP + external MCP), git branch operations (`git_branch_create` with naming conventions, `git_branch_switch`, `git_branch_merge`), branch audit trail via MCP tool call logging.
- **Inter-Agent Coordination** (was P3) — `dispatch_task` MCP tool with `agent` trigger type, `get_queue_status` for situational awareness, `allowed_dispatch_targets` per-job property (empty = deny-all), dispatch attribution to originating task, self-dispatch prohibition, loop prevention, and configurable dispatch depth limiting.
- **Activity Dashboard** (was P2) — job health summaries, task timeline visualization, agent dispatch chain tracing, tool usage patterns. Read-only aggregation over existing task and MCP tool call data with configurable time windows.
- **External MCP Robustness** (was P1) — registration validation (command existence on PATH, args/env type checking), pre-dispatch health checks gating task execution with named server error attribution, cascade deletion removing stale job references from all affected jobs, disabled server enforcement at dispatch, structured args storage, health indicators on job config. Remaining polish items tracked in current P1.
- **Execution Pipeline Fidelity** (was P2 Phase 1) — R12: thinking blocks captured from CLI and streamed/persisted. R13: execution metadata (stop_reason, num_turns, cost_usd) extracted from CLI result events and stored on tasks; max-turns exhaustion distinguished from success. R11: incremental per-event flush during streaming instead of batch-at-end. R4: CLI execution path confirmed as single implementation (no duplication between worker and chat).
- **Job Identity Model** — INTEGER PRIMARY KEY AUTOINCREMENT with slug column derived from name via `slugify()`. Slug used for git authorship and branch naming. Stale string-ID references cleaned up across codebase.
- **Route Modularization** (partial) — job and queue routers extracted to separate modules. Remaining route groups (project, feed, git, dashboard, config) still in `main.py` — completion tracked in P4 (R9).
- **Project Switch DB Coordination** (R3) — readers-draining protocol with `db_read_guard()`, `_closing` flag, `_switching` flag in state.py. Prevents mid-task project switch race conditions.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue, reply with follow-up context.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Task Status State Machine** — authoritative `status` column with validated transitions, batch cascading for coalesced tasks, automatic timestamp management via `transition_task()`.
- **Event-Sourced Task Lifecycle** — `task_events` table (schema v4) with dual-write, backfill from existing timestamps. Foundation for Governor analysis and audit trail.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
- **Governor** (was P2) — autonomous internal meta-analysis agent replacing the standalone Chat feature. Runs every 10 completed tasks (auto-trigger via worker counter) or manually. Dedicated Governor MCP server with read tools (list_jobs, get_recent_tasks, get_git_log, get_job_health, get_prior_findings) and write tools (update_job_properties, create_job, delete_job, update_queue_settings) gated by execution mode. Produces structured findings: suggestions (approve/decline/execute) and observations (read/dismiss). Governor view on left nav with findings feed, approval actions, manual trigger, run history, badge count. Chat interface, chat.py routes, and Chat.jsx removed; task output session tables retained for worker use.
