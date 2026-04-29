# Governor

The Governor is an autonomous meta-analysis agent that periodically reviews recent task activity and emits structured findings (suggestions and observations) to the operator. It is entirely meta-scoped — it observes how jobs and tasks are performing within the platform, identifies friction and improvement opportunities, and surfaces findings on a dedicated UI. It does not touch project code, does not participate in task dispatch, and does not converse with the user.

The Governor replaces the standalone chat interface that earlier versions of the platform exposed. The chat surface is gone; the meta-aware agent has been redirected from reactive Q&A to proactive analysis.

**Modules**:
- `backend/governor.py` — orchestration: trigger handling, prompt assembly, CLI invocation, finding parsing, suggestion execution
- `backend/governor_mcp.py` — stdio MCP server providing read tools (and write tools in execution mode)
- `backend/governor_routes.py` — REST endpoints for findings, runs, manual trigger, status
- `backend/db_governor.py` — persistence (counters, runs, findings)

## Trigger Sources

Two trigger sources, both routed through `governor.run_governor(trigger, task_count)`:

- **Auto** — the worker increments a `governor_task_counter` (config key) on every executed terminal transition (`completed`, `exhausted`, `failed`, `timed_out`) via `_check_governor_trigger`. `cancelled` is skipped because it reflects user intent rather than system behavior; `rejected` never reaches the worker. When the counter reaches 10, the worker resets it to 0 and spawns a Governor run with `trigger="auto"`. Counting failures alongside successes is intentional — a job consistently exhausting turns or failing is exactly what the Governor should review, and counting only successes would let broken jobs evade oversight indefinitely. The cadence is fixed; the user does not configure it.
- **Manual** — `POST /api/governor/trigger` calls `run_governor("manual")` directly. Manual runs do not affect the counter.

A third trigger source — `trigger="execution"` — is used by `execute_suggestion` when the operator approves a suggestion. Execution runs are not user-initiated analyses; they are focused, write-enabled passes that apply a single approved change.

## Run Lock

A module-level `asyncio.Lock` (`_run_lock`) ensures at most one Governor run is in flight at a time. Auto triggers fire-and-forget through `governor.spawn` (which holds a strong reference to prevent GC of the task before its first await); if a run is already in progress, the new invocation is logged and skipped. The lock is held across CLI invocation, finding parsing, and persistence.

Manual triggers are also spawned in the background — the API endpoint returns immediately while the run executes asynchronously.

## Run Persistence

Every run produces a `governor_runs` row at start and updates it at completion. Rows record: trigger source, task count at trigger (for auto), started_at, completed_at, findings_count, optional error message.

A run is considered "active" when `completed_at IS NULL`. The status endpoint surfaces this for the UI's "running" indicator.

## Context Assembly

`_build_context` gathers the data the Governor reasons over:

- **Jobs** — full configuration via `db.list_jobs()` (name, description, model, max_turns, timeout, subscriptions, all properties)
- **Recent terminal tasks** — `db.get_recent_tasks_for_governor(limit=50)` reads through `tasks_resolved` so coalesced subordinates inherit their root's execution metrics. Each row includes `is_subordinate` and `effective_root_id` so the Governor can tell folded contexts from independent runs.
- **Job health** — `db.dashboard_health(7)` for per-job aggregates over the last 7 days
- **Prior findings** — `db.get_governor_findings(limit=30)` with statuses, providing continuity across runs without persistent sessions
- **Git log** — `git.log_oneline` for the last 30 commits, attributing changes to jobs by author

`_format_context` serializes this into the Governor's user prompt as a structured Markdown document. Critically, it includes an explicit note about coalescing semantics — that subordinates' metrics are inherited from their root and should not be summed — because the Governor's analysis would otherwise misclassify coalesce groups as "many cheap runs" instead of "one consolidated run."

There is no persistent session. Each run is a fresh context window. Continuity comes from including prior findings in the context, not from resuming a CLI session.

## CLI Invocation

`_invoke_cli` calls the standard CLI bridge (`cli.invoke`) with:

- A Governor-specific system prompt (`GOVERNOR_SYSTEM_PROMPT` for analysis, `EXECUTION_SYSTEM_PROMPT` for suggestion execution)
- The assembled user prompt
- A temporary MCP config file pointing at `governor_mcp.py` with mode=read or mode=write
- Model `sonnet`, max_turns 20

Governor runs do not go through the worker, do not create a `tasks` row, and do not appear in the queue. The Governor is operationally distinct from job dispatch — it reasons about jobs and tasks, but it is not one of them. This is enforced structurally: there is no "Governor job" in `jobs`, and `governor.run_governor` invokes the CLI directly rather than through `worker.process_one`.

The CLI's response text is collected from `text` and `assistant_complete` events. `error` events raise. The temporary MCP config is unlinked in the `finally` block.

## Finding Parsing

The Governor's contract is to output a JSON array of `{type, title, body}` objects as its final message. `_parse_findings` is robust to common output drift:

1. Try direct `json.loads` on the trimmed text
2. Try extracting from a fenced code block
3. Try parsing whatever JSON-like substring sits between the first `[` and last `]`
4. As a last resort, wrap the raw output as a single observation titled "Governor analysis (unstructured)"

`_validate_findings` filters: type must be `suggestion` or `observation`, title and body must be non-empty, title is truncated to 200 chars. Invalid items are silently dropped. Each surviving finding is persisted via `db.create_governor_finding`, which also sets the initial status (`pending` for suggestions, `unread` for observations).

## Suggestion Execution

When the operator approves a pending suggestion:

1. `POST /api/governor/findings/{id}/approve` updates the finding's status to `approved` and spawns `governor.execute_suggestion(id)`
2. `execute_suggestion` writes a new `governor_runs` row with `trigger="execution"`, assembles a focused user prompt containing the approved suggestion's title and body, writes a write-mode MCP config, and invokes the CLI with `EXECUTION_SYSTEM_PROMPT`
3. The Governor reads the current state, applies the change via write tools, and emits a brief JSON summary
4. The finding is updated to `executed` (with the response text as `execution_result`) or `failed` (with the error message)

This is not a general-purpose execution channel. The Governor executes the specific change it proposed. The write tools are scoped to meta-operations (job config, queue settings) — the Governor cannot modify project files, commit code, or dispatch tasks.

Decline is purely a status update — `POST /api/governor/findings/{id}/decline` sets status to `declined` with no Governor invocation.

Read/dismiss for observations work analogously: status transitions, no agent run.

## MCP Server: `governor_mcp.py`

A dedicated stdio MCP server presents Governor-specific tools, gated by mode:

### Read Tools (always available)

| Tool | Backed by |
|------|-----------|
| `list_jobs` | `GET /api/jobs/` — full configuration |
| `get_recent_tasks` | `GET /api/governor/recent-tasks?limit=N` — terminal tasks via `tasks_resolved` |
| `get_git_log` | `git log --oneline -N` (subprocess, project-dir scoped) |
| `get_job_health` | `GET /api/dashboard?window=N` — per-job aggregates |
| `get_prior_findings` | `GET /api/governor/findings?limit=N` |

### Write Tools (execution mode only)

| Tool | Backed by |
|------|-----------|
| `update_job_properties` | `PATCH /api/jobs/{id}` — any property |
| `create_job` | `POST /api/jobs/` |
| `delete_job` | `DELETE /api/jobs/{id}` |
| `update_queue_settings` | `POST /api/queue/settings` |

Mode is selected by the `MAISTRO_GOVERNOR_MODE` env var (`read` or `write`) which `_write_mcp_config` sets when assembling the MCP config. Inside the server, `tools/list` returns the appropriate set, and `tools/call` rejects write-tool invocations in read mode with a `Method not found` error. This is defense-in-depth — even if the Governor model attempts to call a write tool during analysis, the server refuses.

Tool calls reach the backend over HTTP (`localhost:8420`) rather than through direct database access. The MCP server is a separate process with its own environment; routing through HTTP keeps the trust boundary clean (every action passes through the same routes the UI uses).

## Routes

`backend/governor_routes.py` exposes (`prefix="/api/governor"`):

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/findings` | List findings, optionally filtered by type/status |
| `POST` | `/findings/{id}/approve` | Mark approved and spawn execution run |
| `POST` | `/findings/{id}/decline` | Mark declined (no run) |
| `POST` | `/findings/{id}/read` | Mark observation as read |
| `POST` | `/findings/{id}/dismiss` | Mark observation/suggestion as dismissed |
| `POST` | `/trigger` | Spawn a manual run |
| `GET` | `/runs` | Recent runs with status |
| `GET` | `/status` | Aggregated state for the rail badge (counter, running flag, last run, pending/unread counts) |
| `GET` | `/recent-tasks` | The same data the MCP `get_recent_tasks` tool returns (used by the MCP server itself) |

## Frontend

The `Governor` rail item opens `Governor.jsx`, which renders:

- A findings feed ordered by recency (suggestions and observations interleaved)
- Per-suggestion approve/decline buttons; per-observation read/dismiss buttons
- A manual trigger button
- A run history sidebar (last N runs, with their findings count and trigger source)
- A status badge counting pending suggestions and unread observations, surfaced on the rail

The view does not provide a text input. The operator does not converse with the Governor.

## Constraints

- **Meta-scoped**: write tools are restricted to job config and queue settings. Cannot modify project files, dispatch tasks, or commit code.
- **No persistent session**: each run is fresh context. Continuity is via prior-findings injection, not session resume.
- **Lock-serialized**: at most one run in flight. Auto triggers that arrive during a running execution are dropped (the counter does not back-pressure — the next 10 completions trigger the next run).
- **Independent of the worker**: Governor runs do not block, queue behind, or interact with task dispatch. The two systems share the database but not the execution path.
- **Fixed cadence**: 10 completed tasks. Not user-configurable.
- **Coalescing-aware**: recent-tasks reads through `tasks_resolved`; the system prompt explicitly warns against summing metrics across coalesced subordinates.

## Relationship to Other Systems

- [Storage](storage.md) — `governor_runs` and `governor_findings` tables; the `governor_task_counter` config key
- [Dispatch Engine](dispatch-engine.md) — the worker increments the auto-trigger counter on successful completion
- [CLI Bridge](cli-bridge.md) — Governor runs invoke the same CLI bridge as job dispatches, with a separate MCP config
- [Tool Mediation](tool-mediation.md) — the Governor MCP server is structurally similar to the internal MCP server but scoped to meta-operations
- [Frontend](frontend.md) — the Governor view replaces the prior Chat surface
