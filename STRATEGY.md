# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop and operational visibility. The platform dispatches through a two-stage queue (pending → queued), streams output in real time, enforces timeouts, manages approvals, and records everything. Outcome summaries close the feedback loop — the user can see what agents produced, and downstream agents receive that context. The Activity Dashboard aggregates system health, task timelines, agent dispatch chains, and tool usage patterns into a single view. The Dispatch view presents the full task lifecycle as a unified three-column kanban: Upcoming (pending + queued), Active (executing), and Resolved (completed vs. non-success), with a detail drawer for inspecting any task.

What's in place:
- **Unified Dispatch kanban** — three-column layout (Upcoming / Active / Resolved) with drag-based merge, split, transfer, reorder, and bottom detail drawer for task inspection
- **Activity Dashboard** — goal health summaries, task timeline, agent dispatch chain visualization, tool usage patterns, configurable time windows
- **Outcome summaries** — auto-derived from commits between `start_commit` and `result_commit`, displayed inline on completed tasks
- **Outcome in dependency context** — downstream agents receive upstream outcome summaries in their trigger context
- **Goal colors** — visual identifiers distinguishing goals across all surfaces
- **Five trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`, circular chains permitted), agent (imperative cross-goal dispatch)
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses
- **Dispatch continuity** — resume (CLI `--resume`), retry for failed/timed-out dispatches, and reply for follow-up context on resolved tasks
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — trigger-specific and global modes; manual queue composition (merge/split) in pending; agent triggers coalesce like other types
- **Internal MCP server** — platform-hosted, context-aware tool surface with git operations, file access, task info, branch management, and inter-agent dispatch. Every tool call is observable and auditable
- **Three-dimensional tool control** — `allowed_tools` (CLI), `allowed_internal_tools` (internal MCP), `mcp_servers` (external MCP) compose independently with discoverable inventories
- **Branch operations** — agents can create, switch, and merge branches with enforced naming conventions and audit trail
- **Inter-agent dispatch** — agents can enqueue tasks for other goals via `dispatch_task`, query queue state via `get_queue_status`, governed by `allowed_dispatch_targets` with depth limiting and loop prevention
- **Chat, Files, Feed, Settings** — complete supporting surfaces

## Diagnosis

The platform now has both execution capability and operational visibility. Agents can manage branches, dispatch work to other agents, query queue state, and be selectively restricted from tools. The Activity Dashboard surfaces goal health, task timing, dispatch chains, and tool usage — the operator can see patterns across tasks without clicking through history one by one. Dispatch depth limiting prevents runaway agent cascades.

Four gaps, in order of urgency:

**External MCP is fragile and opaque.** External MCP servers are the platform's extensibility mechanism — they give agents access to arbitrary tool surfaces beyond what the platform provides natively. The lifecycle is nominally complete (register, discover tools, assign to jobs, pass config to CLI), but the integration is held together by assumptions that don't hold in practice. There is no validation at registration time — an invalid command is accepted silently and only fails at dispatch. There is no pre-flight health check — a task dispatches with a dead server and fails with a generic CLI error that doesn't name the server or explain the failure. There are no environment variable controls in the UI — env vars must be set outside the platform. Jobs can reference deleted or disabled servers with no warning — the server is silently omitted from the config and the agent loses tools without any indication. The args input accepts space-separated strings that are easy to misformat. Error messages from MCP server failures during execution are indistinguishable from other CLI errors. The configuration is harder than it needs to be, the diagnostics tell you less than they should, and failures propagate silently into task results.

**Accumulated technical debt.** A systematic remediation audit (REMEDIATION.md) identified 13 items across the backend. Two are already resolved — the task status state machine (R1) and project-switch DB coordination (R3). Route modularization (R9) is partially done. The remaining items fall into three tiers: user-visible gaps where the platform silently drops information (thinking blocks in streamed output, execution metadata like cost and stop reason, chat events lost on batch write failure), structural debt that slows every future change (database.py as a 2100-line god module, duplicated CLI execution harness in worker and chat, coalescing logic dispersed across 12+ touch points), and execution robustness issues (synchronous git calls blocking the async event loop). The debt is not theoretical — agents silently exhaust max turns with no indication, thinking blocks are dropped from the stream, and chat event failures discard all task output.

**Outcome interrogation.** When a task completes, the user can see the outcome summary, diff, and dashboard aggregates — but cannot ask follow-up questions. The agent's session is locked behind the streamed output log. **Design complete** — session resume via `--resume`, read-only tool stripping, UI integration in chat tray, all specified.

**Proactive alerting.** The dashboard answers "how are my agents doing?" when the user looks — but doesn't tell them when something needs attention. **Design complete** — in-app feed, desktop notifications, per-goal rules, webhook integration, all specified in DESIGN.md.

## Guiding Policy

**Make the extensibility surface trustworthy, then fix the internals.** External MCP is how the platform becomes more than what it ships with — every external tool, every third-party integration, every custom capability flows through this mechanism. If configuration is confusing, diagnostics are opaque, and failures propagate silently, the platform's extensibility promise is hollow. Getting external MCP right is a prerequisite for real-world use, where agents need tools beyond the built-in surface. After that: stabilize the internal execution pipeline (remediation), then let the operator interrogate completed work (session interrogation), then make the platform proactive about alerting (notifications).

## Priority 1: External MCP Robustness

**The problem:** External MCP servers are the platform's extensibility mechanism, but the current integration is fragile and opaque across the full lifecycle. Configuration accepts invalid commands silently. The UI has no environment variable controls — a common requirement for API-key-authenticated servers. Args input is a space-separated text field that's easy to misformat. Jobs can reference deleted or disabled servers with no warning — the server is silently omitted from the dispatch config and the agent loses tools it was supposed to have. There is no pre-flight health check before dispatch — a task runs against a dead server and fails with a generic CLI error that doesn't identify the server or the failure mode. Error messages from MCP server failures during execution are indistinguishable from other CLI errors. The probe mechanism works but is on-demand only — the operator must manually click "Test" to discover a server is broken.

**Why first:** External MCP is how the platform becomes more than a closed system. Every third-party tool, every custom integration, every domain-specific capability flows through this mechanism. If it's hard to configure, if failures are silent and opaque, and if diagnostics require guesswork, the extensibility promise is hollow. This is the most immediate barrier to real-world use — an operator who can't reliably add and trust external tools will not trust the platform with autonomous work.

**Phase 1 — Configuration that's hard to get wrong:**
- **Registration validation** — validate command at registration time: check that the executable exists on PATH (or is a valid absolute path). Reject registration with a clear error if the command can't be found. Validate that args and env parse as valid JSON before storing. Surface validation errors in the UI at registration time, not at dispatch time.
- **Environment variable UI** — add env var management to the MCP server configuration form. Key-value pairs with add/remove controls. Env vars are critical for API-key-authenticated servers (which is most of them). Without this, operators must set env vars outside the platform, which breaks the self-contained configuration model.
- **Args as structured list** — replace the space-separated text field with a proper list input that handles quoting, special characters, and multi-word arguments correctly. Each argument is a discrete entry. Eliminates the class of errors where a misplaced space produces wrong arguments.
- **Stale reference cleanup** — when a server is deleted, remove it from all jobs' `mcp_servers` lists. When a server is disabled, either exclude it from dispatch (current behavior) or warn the operator that jobs referencing it will lose tools. Surface stale references visibly — a job configured with a server that no longer exists should show a warning, not silently degrade.

**Phase 2 — Diagnostics that tell you what's wrong:**
- **Pre-dispatch health check** — before a task dispatches, probe all assigned external MCP servers. If any are unreachable, fail the task with a specific error naming the server and the failure reason (command not found, timeout, handshake failed, disabled). The operator sees exactly what broke and can fix it before retrying. This is not a background monitor — it's a gate at dispatch time.
- **Server status in dispatch context** — the task detail view shows which external MCP servers were included in the dispatch config and their health at dispatch time. When a task fails, the operator can immediately see whether the failure correlates with a server issue.
- **Structured error attribution** — when the CLI reports an MCP-related error during execution, surface it with the server name and failure mode rather than as a generic CLI error. This requires parsing CLI error output for MCP connection failures and enriching the error context.
- **Health indicator on job config** — the job's MCP server selection shows live health status per server (healthy/unreachable/disabled) so the operator can see problems before dispatching.

**Phase 3 — Integration that doesn't break the system:**
- **Disabled server enforcement** — a disabled server is never included in dispatch config, period. Currently `build_mcp_config` defaults missing `enabled` field to `True`. Make the default explicit and enforce it. A disabled server in a job's list produces a visible warning on the job config, not silent omission at dispatch.
- **Graceful degradation on server deletion** — deleting a server that's referenced by jobs requires confirmation showing affected jobs. After deletion, affected jobs show a clear "missing server" indicator rather than silently losing tools.
- **Config file robustness** — validate the assembled MCP config JSON before writing to the temp file. Catch malformed args/env at config assembly time with a specific error rather than letting it propagate as an opaque CLI failure.

**What's already working:** Registration and storage, on-demand probe with 10s timeout and parallel execution, per-job server assignment, tool discovery and inventory display, test button with health indicators, config generation and temp file lifecycle, internal server integration (always included, separate from external). The probe mechanism is solid — the gap is in what happens before, after, and around it.

**Second-order effects:** Reliable external MCP changes what agents can do. With trustworthy tool extension, agents can access databases, APIs, documentation systems, and domain-specific tools. This makes the platform useful for real workflows, not just self-contained demos. It also makes the tool governance story (allowed_tools, allowed_internal_tools, mcp_servers) coherent end-to-end — governance over tools you can't reliably connect to is theoretical.

## Priority 2: Technical Remediation

**The problem:** The backend has accumulated structural debt that silently degrades the operator's experience and blocks clean implementation of the remaining features. Agents drop thinking blocks from streamed output, silently exhaust max turns with no indication, lose all chat events on batch write failure, and block the async event loop on every git call. The 2100-line database.py monolith makes every schema change risky. The CLI execution harness is duplicated between worker and chat — adding session interrogation (P3) would create a third copy.

**Why second:** These items fix silent data loss and invisible failures — operational correctness, not polish. The shared CLI harness (R4) is a direct prerequisite for session interrogation (P3). Execution metadata capture (R13) makes notifications (P4) more meaningful. But none of this matters if external tool integration — the mechanism that makes agents useful beyond the built-in surface — is unreliable.

**Phase 1 — Surface what agents actually do (small effort, high visibility):**
- **R12: Thinking blocks in assistant messages** — `_translate_event` in `cli.py` handles `thinking_delta` in the legacy stream path but drops `thinking` content blocks from the current `assistant` message format. Three lines in `cli.py`, SSE handler + render in `Queue.jsx`. *Partially implemented — legacy path works, current path drops.*
- **R13: Execution metadata capture** — the CLI `result` event carries `stop_reason`, `num_turns`, `duration_ms`, `total_cost_usd`, `modelUsage` — all silently dropped. Max-turns exhaustion looks identical to success. Schema columns on `tasks`, extraction in `_translate_event`, `max_turns` as a configurable goal property (currently hardcoded at 50). Frontend surfaces turns used, cost, and distinct treatment for max-turns exhaustion.
- **R11: Incremental chat event flush** — events accumulate in memory and flush once after CLI finishes. Failure discards everything (silent `pass` on exception). Fix: periodic flush during streaming (every ~50 events), retry with chunking on failure, fallback file for unrecoverable writes.
- **R8: Move glob matching** — `_any_file_matches` and `_glob_to_regex` are trigger-matching functions living in the prompt-assembly module (`dispatch.py`). Move to `git.py` or a dedicated `matching.py`. Trivial.

**Phase 2 — Structural clarity (mechanical, no behavior change):**
- **R2: Split database.py** — 2100+ lines covering schema, migrations, goal CRUD, task lifecycle, coalescing, chat, config, dashboard, and property casting. Split into `db_core.py` (connection, schema, migrations), `db_jobs.py` (goal CRUD, property system), `db_tasks.py` (task CRUD, state transitions, coalescing, queue queries), `db_chat.py` (sessions, messages, events), `db_config.py` (config, MCP servers), `db_dashboard.py` (aggregation). Re-export from `database.py` for backward compatibility.
- **R5: Consolidate coalescing** — fold into R2. Extract `coalesce_into`, `decoalesce`, `cascade_completion` as explicit functions in `db_tasks.py`. Add depth-1 CHECK constraint. Eliminate the three duplicate cascade paths in the worker.
- **R9: Complete route extraction** — goal, queue, and chat routers exist. Extract remaining route groups from `main.py`: project, feed, git, dashboard, config. `main.py` becomes lifespan + CORS + `include_router` calls + health check.

**Phase 3 — Execution robustness:**
- **R4: Shared CLI execution harness** — extract `run_cli_session` from the duplicated patterns in `worker.py` and `chat.py`. Encapsulates session creation, `cli.invoke()` iteration, event buffering with incremental flush (from R11), response accumulation, optional timeout/cancellation. Worker and chat become thin callers. **This is prerequisite for P3** — session interrogation adds a third CLI execution path.
- **R7: Async git operations** — all git functions use sync `subprocess.run`, blocking the event loop. Wrap in `asyncio.to_thread`, make all callers `await`. Mechanical but wide-reaching.

**What's already done:** R1 (task status state machine with validated transitions), R3 (DB connection coordination with readers-draining protocol and `_switching` flag), R9 partial (goal, queue, chat routers extracted).

**What's deferred from the remediation plan:** R6 (flatten EAV properties to JSON column) — the assembly overhead is real but not blocking. R10 (replace global `PROJECT_DIR` with `ProjectContext`) — structural change that only matters if multi-project becomes a requirement.

**Second-order effects:** R13 unlocks cost tracking, which is currently listed as deferred with the rationale "CLI doesn't expose token counts cleanly" — but it does. The `result` event already carries `total_cost_usd` and per-model usage. R4 makes session interrogation (P3) a configuration concern rather than a third implementation. R2 makes every future schema change lower risk.

## Priority 3: Task Session Interrogation

**The problem:** Completed tasks produce outcome summaries and diffs, but the user cannot ask follow-up questions. "Why did you change this file?" "What alternatives did you consider?" "What did the test output look like?" These questions require conversational access to the agent's session — the same context, the same reasoning chain. Currently the user must read the raw streamed output log, which is noisy and non-interactive.

**Why third:** Still the highest-leverage feature for operator understanding — but building it on the shared CLI harness (R4 in P2) instead of creating a third duplicate is the right sequencing. The design is complete in DESIGN.md. Implementation-ready once R4 lands.

**Specifically:**
- **Session resume from Dispatch** — completed tasks in the Resolved column gain a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. No `Edit`, `Write`, `Bash` (or Bash in a read-only mode), no `git_commit`. The agent can read files, search code, and reason about its prior work, but cannot modify the project. This is the critical constraint: interrogation does not produce side effects.
- **Gating new work through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.
- **UI integration** — the session chat appears in the same chat tray used for standalone conversation, but with a visual indicator that this is a task session (goal color, task reference) and that it is read-only.

**Second-order effects:** This changes how the operator relates to completed work. Instead of treating outcomes as final artifacts to accept or reject, the user can interrogate the reasoning, build understanding, and make better decisions about what to dispatch next. It also provides a natural feedback channel — the user's questions reveal what information the agent should have surfaced proactively, which can inform dashboard refinements and notification rule design.

## Priority 4: Notifications

**The problem:** The platform requires the operator to be watching. Task completions, failures, timeouts, and anomalies are only visible when the user opens the UI and looks at the queue or dashboard. As the system becomes more autonomous — scheduled goals running overnight, agent-initiated dispatch chains executing while the operator is away, watch triggers firing on every commit — the gap between "something happened" and "the operator knows" grows. The dashboard made patterns visible; notifications make them timely.

**Why fourth:** This is the transition from reactive to proactive operation. External MCP robustness (P1) makes agents capable. Remediation (P2) fixes what agents report. Session interrogation (P3) lets the operator investigate it. Notifications complete the loop — telling the operator when to look. Together they close the full cycle: tools work reliably (P1), the system captures everything (P2), alerts the operator (P4), the operator investigates (P3), then dispatches corrective work through the queue. Without notifications, the operator must poll. Polling doesn't scale with autonomous task volume. **Design complete** — all four channels (in-app feed, desktop, rules, webhook) are specified in DESIGN.md with safety constraints. Implementation-ready once P2 lands execution metadata.

**Specifically:**
- **In-app notification feed** — a lightweight notification center (badge + dropdown or panel) showing recent events: task completions, failures, timeouts, approval requests pending. This is the minimum viable surface — no external integrations required, works immediately.
- **Desktop notifications** — browser Notification API for critical events (failures, timeouts, approval requests). The user opts in. Low implementation cost, high leverage for the "away from tab" use case.
- **Notification rules** — per-goal or global: notify on all completions, only failures, only timeouts, never. Defaults to failures and timeouts. The operator tunes signal-to-noise as task volume grows.
- **Webhook integration** — an outbound webhook (URL + optional secret) that fires on configurable events. This is the escape hatch for Slack, Discord, email, or any external system. The platform sends a structured JSON payload; the user wires it to wherever they want. One generic mechanism instead of N specific integrations.

**Second-order effects:** Notifications change the operator's relationship with the system from monitoring to supervising. The operator can leave the UI, trust that the system will surface what matters, and return only when needed. This is a prerequisite for scaling to more goals, more triggers, and longer autonomous runs. It also makes approval gates practical at scale — an approval request that sits unseen in the queue is a blocked pipeline. A notification makes it a prompt.

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. The tool surface is now broad enough (branch ops, dispatch, queue status) that agents can do most coordination without Bash. The `allowed_internal_tools` mechanism makes restriction possible. Revisit once real usage patterns reveal whether agents still rely on Bash for operations that could be structured tools.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, concurrent MCP sessions). Branch tools are now implemented, which removes the safety prerequisite. Revisit once the dashboard reveals whether sequential processing is a real bottleneck and once the orchestration pattern sees real use.
- **Auto-evaluation** — LLM judges dispatch output quality automatically. Needs a calibration dataset. Revisit once the dashboard reveals performance patterns and task session interrogation (P3) gives the operator a way to validate agent reasoning.
- **Prompt effectiveness tracking** — correlate goal instruction changes with task success rates. Needs enough task history to be statistically meaningful. The dashboard now surfaces goal-level health; revisit once enough history accumulates.
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
- **Full Bash removal** — agents need escape hatches. The goal is structured alternatives that agents prefer, not a sandbox that breaks them.

## Completed

- **Dispatch Outcome Summaries** (was P1) — auto-derived outcome summaries from commit history and outcome injection into dependency trigger context. The first feedback loop.
- **Two-Stage Queue** — pending (staging/curation) and queued (execution runway) with specialized drag operations: merge and split in pending, reorder in queued, transfer between columns.
- **Unified Dispatch View** — three-column kanban (Upcoming / Active / Resolved) with bottom detail drawer. Resolved column separates completed vs. non-success with status badges and inline error context. Replaces the earlier separate History tab.
- **Terminal State Differentiation** — six distinct terminal states with clear behavioral semantics. Only completed (no error) triggers downstream dependencies. Each non-success state implies a different user response.
- **Goal Colors** — visual identifiers assigned on creation from a curated palette, displayed across all goal-related surfaces.
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
- **Platform Tool Governance** (was P2) — `allowed_internal_tools` per-goal property, three-dimensional tool composition (CLI + internal MCP + external MCP), git branch operations (`git_branch_create` with naming conventions, `git_branch_switch`, `git_branch_merge`), branch audit trail via MCP tool call logging.
- **Inter-Agent Coordination** (was P3) — `dispatch_task` MCP tool with `agent` trigger type, `get_queue_status` for situational awareness, `allowed_dispatch_targets` per-goal property (empty = deny-all), dispatch attribution to originating task, self-dispatch prohibition, loop prevention, and configurable dispatch depth limiting.
- **Activity Dashboard** (was P2) — goal health summaries, task timeline visualization, agent dispatch chain tracing, tool usage patterns. Read-only aggregation over existing task and MCP tool call data with configurable time windows.
- **External MCP Server Lifecycle** — full registration, connection, tool discovery, and per-goal assignment.
- **Route Modularization** (partial) — goal, queue, and chat routers extracted to separate modules. Remaining route groups (project, feed, git, dashboard, config) still in `main.py` — completion tracked in P2 Phase 2 (R9).
- **Project Switch DB Coordination** (R3) — readers-draining protocol with `db_read_guard()`, `_closing` flag, `_switching` flag in state.py. Prevents mid-task project switch race conditions.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue, reply with follow-up context.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Task Status State Machine** — authoritative `status` column with validated transitions, batch cascading for coalesced tasks, automatic timestamp management via `transition_task()`.
- **Event-Sourced Task Lifecycle** — `task_events` table (schema v4) with dual-write, backfill from existing timestamps. Foundation for notifications and audit trail.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
