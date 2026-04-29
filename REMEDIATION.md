# Remediation

Architectural debt and remediation plan for the mAistro backend. Ordered by impact — highest-leverage fixes first.

---

## R2: database.py is a god module

**Problem:** 1300+ lines handling schema, migrations, goal CRUD, task lifecycle, coalescing, chat sessions, MCP servers, config, dashboard aggregation, and property casting. No internal abstraction boundaries. Lifecycle invariants (like "what makes a task eligible") are implicit and spread across functions.

**Remediation:** Split along domain boundaries:
- `db_core.py` — connection management, `get_db()`, `init_db()`, `close_db()`, schema, migrations
- `db_jobs.py` — goal CRUD, property system, `list_jobs`, `get_jobs_depending_on`
- `db_tasks.py` — task CRUD, enqueue, coalescing, state transitions (from R1), queue queries, sweep
- `db_chat.py` — chat sessions, messages, events
- `db_config.py` — config table, MCP servers
- `db_dashboard.py` — aggregation queries

Each module imports `get_db` from `db_core`. The public API (`database.py`) re-exports everything for backward compatibility during transition, then call sites migrate to direct imports.

**Scope:** Pure restructuring — no behavior change. Can be done incrementally, one domain at a time.

---


## R4: worker.py and chat.py duplicate the CLI execution harness

**Problem:** Both modules independently spawn Claude CLI subprocesses, stream NDJSON, accumulate responses, store chat events/messages, and manage session IDs. The patterns are nearly identical but chat lacks timeout, cancellation, and MCP tool audit. Adding features to one requires remembering to update the other.

**Remediation:** Extract a shared `run_cli_session` coroutine (or async context manager) that encapsulates:
- Session creation/reuse
- `cli.invoke()` iteration with event classification
- Raw event buffering and batch storage
- Response accumulation and message storage
- Session ID capture and update
- Optional timeout watchdog and cancellation

The worker and chat become thin callers that configure the harness (broadcast vs. queue, timeout vs. none, read-only vs. full tools) and handle their domain-specific lifecycle around it.

**Scope:** New module (e.g., `cli_session.py`), then refactor `worker._process_task` and `chat._run_cli` to use it. Medium effort — the two implementations are close enough that extracting the common core is straightforward.

---

## R5: Coalescing logic is dispersed across 12+ touch points (partially done)

**Problem:** Coalescing has two forms of dispersal — *write-side* (mutation) and *read-side* (queries that need coalescing-awareness). Both leak through the codebase as code discipline rather than as expressible invariants.

*Write side.* Coalescing was referenced in: `enqueue_task`, `merge_tasks`, `split_task`, `uncoalesce_task`, `_flatten_coalesce`, `coalesce_under`, `get_subordinate_tasks`, `get_oldest_queued_task`, `get_task_queue`, `transfer_task`, `cancel_task` (queue_routes), and five worker completion paths. The depth-1 invariant was maintained by code discipline only.

*Read side.* Subordinate task rows have null execution-outcome columns (`num_turns`, `cost_usd`, `stop_reason`, `started_at`, `session_id`, `start_commit`, `result_commit`, `error`) because only the root actually executes. Any query that reads these columns directly off `tasks` returns null for subordinates and a misleading picture of "what actually happened to this task." The Governor was the canary: `get_recent_tasks_for_governor` returned 5 rows with null metrics for an Engineering coalesce group, and the agent inferred (wrongly) that "only the root's context was reviewed." Other consumers — dashboards, queue routes, frontend output endpoints — had the same shape of bug latent in them.

The two dispersals interact: the existing pending-only constraint on every coalesce-mutating op (`split_task`, `uncoalesce_task`, `merge_tasks` require `pending`; `transfer_task` requires `pending`/`queued`) means a terminal coalesced subtree is already structurally immutable. That makes the read-side problem a "show the right thing" issue, not a "guard against split" issue — outcome data for a terminal coalesced group is permanent and safe to inherit.

### Done in this pass

*Write side:*
- ✅ **Reordered `coalesce_under` and `merge_tasks`** to flatten *before* re-parenting, so depth-1 holds at every intermediate state. Required so the new triggers don't trip on the transient depth-2 state during reply/resume.
- ✅ **Three SQLite triggers** (`tasks_depth1_insert`, `tasks_depth1_update_target`, `tasks_depth1_update_self`) added to `SCHEMA_SQL`. Depth-1 is now a hard DB-level invariant. Smoke-tested: blocks pointing a `coalesced_id` at a subordinate (insert and update) and blocks demoting a task that still has subordinates of its own. Legitimate flatten+coalesce sequences pass.
- ✅ **Cleaned up 4 historical depth-N violations** in the local project DB (chains 272→273→274→285 and 359→361→364→369). Iterative flatten run as a one-shot outside the application code (per the no-baked-in-migrations convention). Other projects with legacy data may need the same cleanup; the trigger doesn't validate existing rows on install, so legacy chains don't break — they just produce stale fall-through data in `tasks_resolved`.
- ✅ **Extracted `db.cascade_completion(root_id, terminal_status, **fields)`** — single helper now used by all five worker completion paths (timeout, cancelled, exhausted, completed, failed). Side benefit: completed/exhausted paths previously used the start-of-task subordinate snapshot, missing late-coalesces that arrived during execution. They now re-query fresh.

*Read side:*
- ✅ **`tasks_resolved` view** in `SCHEMA_SQL`. Outcome columns COALESCE through `coalesced_id` to root; intrinsic columns pass through unchanged. Adds `is_subordinate` and `effective_root_id` markers. View is `DROP+CREATE` on every `init_db` so the definition stays current without a migration.
- ✅ **`db.get_task_resolved(task_id)`** added — opt-in resolved read for callers that care about effective outcome.
- ✅ **`/api/tasks/{id}/output`, `/diff`, `/outcome`** switched to `get_task_resolved`. Subordinate clicks now return the root's chat output / commit range / outcome summary instead of empty.
- ✅ **`/api/tasks/{id}/stream`** subscribes to the effective root's pubsub channel for subordinates (was hanging forever on an empty channel).
- ✅ **`get_recent_tasks_for_governor`** reads through `tasks_resolved`; surfaces `is_subordinate` / `effective_root_id` markers; Governor's prompt formatter now renders `coalesced→#N` and is instructed not to sum metrics across coalesced rows.
- ✅ **`dashboard_health`** filters `coalesced_id IS NULL` to count one row per actual execution. Other dashboard queries (`dashboard_timeline`, `dashboard_chains`, `dashboard_tool_usage`) verified naturally root-only because they join through `task_events` or `chat_sessions`, which subordinates don't have.

*Documentation:*
- ✅ **CLAUDE.md** describes: depth-1 invariant enforced by triggers, lock-at-terminal as emergent policy from existing pending-only guards, two semantic profiles for reading task data (`tasks_resolved` for "per-task as the agent saw it" vs. `tasks WHERE coalesced_id IS NULL` for "per-execution / per-cost").

### Still outstanding

*Write-side consolidation* — **deferred to R2.** The original plan called for a `coalesce` module with named helpers (`coalesce_into`, `decoalesce`, `decoalesce_all`, etc.). With the depth-1 trigger now in place and `cascade_completion` already extracted, the remaining benefit of a dedicated module is naming/locality, not correctness. Better to fold it into R2's `db_tasks.py` extraction so the helpers and the schema definitions live next to each other from day one.

*Read-side consumer sweep, remaining call sites:*
- `get_task` and `get_task_with_session` — still raw. Used in many places; some callers want raw subordinate identity (queue listing, drag-drop UI), others would benefit from resolution. Migration is opt-in per call site via the new `get_task_resolved` — no global change needed. Audit the remaining call sites case-by-case as bugs surface or as part of broader refactors.
- Frontend `Queue.jsx` / `Tasks.jsx` task-card rendering — currently shows null metrics for subordinates because they read `task.num_turns` etc. directly. Could either render `coalesced→#N (inherits #N's metrics)` or fall back to root values via a frontend helper. Low priority — cosmetic, not a correctness bug.
- Anything else that surfaces — the test for "does this caller need resolution?" is "would a user clicking on a subordinate get a worse experience than clicking on its root?" If yes, switch to `get_task_resolved`. If no, leave alone.

*Legacy data hygiene:*
- The local DB had 4 depth-N violations cleaned up manually. Other projects that have used the system may have similar legacy chains. A `/scripts/flatten_coalesce.py` one-shot would let users repair their own DBs; not necessary for new projects (trigger prevents new violations).

**Scope of remaining work:** Small. The write-side consolidation rides with R2; the read-side sweep is opt-in per call site as needed. Most R5 risk is now fixed by the trigger making depth-1 violations impossible going forward.

---

## R6: EAV property system pays ongoing tax for unused flexibility

**Problem:** Every `get_job` call joins three tables, builds defaults, overlays overrides, and casts types. The assembly code is copy-pasted across `get_job`, `list_jobs`, and `get_jobs_depending_on`. Property definitions are hardcoded in `SEED_SQL` — the EAV flexibility (add properties without migrations) is never actually used at runtime.

**Remediation:** Two options depending on appetite:
- **Minimal:** Extract a `_assemble_job_properties(job_id_or_rows, prop_rows, running_ids)` helper that all three consumers call. Eliminates the copy-paste without changing the data model.
- **Structural:** Flatten properties into a `job_config` JSON column on `jobs`. One column, one parse, no joins. Migration script reads current EAV state and writes JSON. Property definitions become a Python-side schema (dataclass or Pydantic model) that validates and applies defaults on read.

**Scope:** Minimal fix is small (database.py only). Structural fix is medium — migration + update all property reads/writes.

---

## R7: git.py blocks the async event loop

**Problem:** All git operations use synchronous `subprocess.run`, blocking the event loop. The worker calls `git.head_hash()` while holding `_lock`. The scheduler calls it every 30 seconds. Feed routes call `git.log` with `--numstat`. On large repos or slow storage, any call can block for hundreds of milliseconds.

**Remediation:**
- Wrap `run_git` in `asyncio.to_thread` (or `loop.run_in_executor`) and make all callers `await` it.
- Alternative: provide an `async_run_git` alongside the sync version for the transition period.
- The `cli.py` module already handles this correctly with `Popen` + thread readers — git.py should follow the same pattern or use the simpler `to_thread` wrapper.

**Scope:** `git.py` (add async wrappers), all callers in `dispatch.py`, `worker.py`, `scheduler.py`, `main.py`, `chat.py`. Mechanical but wide-reaching — every `git.foo()` becomes `await git.foo()`.

---

## R8: Subscription glob matching lives in the prompt assembly module

**Problem:** `_any_file_matches` and `_glob_to_regex` in `dispatch.py` are the core watch-trigger mechanism — they determine which goals fire on commits. This is a triggering/matching concern, not a prompt assembly concern.

**Remediation:** Move glob matching to `git.py` (which already has `resolve_glob_files`) or a dedicated `matching.py`. The `check_watch_triggers` function that calls it should move to wherever trigger evaluation lives (currently also in dispatch.py — could be its own module or part of the worker/scheduler domain).

**Scope:** Small. Move two functions + one caller. No behavior change.

---

## R9: main.py accumulates route groups

**Problem:** `main.py` defines project, feed, git, hook, tool inventory, MCP server, file, dashboard, and config routes directly — 8 route groups in the app shell. The router extraction pattern exists (`job_routes.py`, `queue_routes.py`, `chat.py`) but hasn't been applied to the rest.

**Remediation:** Extract into routers following the existing pattern:
- `project_routes.py` — open, close, browse, recent
- `feed_routes.py` — feed list, feed item
- `git_routes.py` — log, diff, status, file read/write (already has `git.py` for the subprocess layer)
- `dashboard_routes.py` — dashboard aggregation endpoint
- `config_routes.py` — config CRUD, MCP server CRUD, tool inventory, hooks

`main.py` becomes: lifespan, CORS, `include_router` calls, health check.

**Scope:** Small per-router, purely mechanical. No behavior change.

---

## R10: state.py makes the backend structurally single-tenant

**Problem:** `PROJECT_DIR` as a module-level global means one project at a time, no test parallelism, and uncoordinated mid-task project switches. The worker and scheduler read `PROJECT_DIR` without synchronization — it can change between their check and their use.

**Remediation:** This is a design constraint, not a near-term fix. Document it as intentional for now. If multi-project support becomes a goal:
- Replace the global with a `ProjectContext` object passed through the request/task lifecycle.
- Worker and scheduler receive context at task pickup time, not from a global.
- DB connection binds to context, not to the process.

**Scope:** Large — structural change that touches every module. Not recommended until multi-project is an actual requirement. For now, the practical fix is just coordinating project switch with worker/scheduler idle state (covered in R3).

---

## R11: Chat events lost on batch write failure

**Problem:** The `try/except` safety net around `add_chat_events_batch` in `worker.py` (added to fix the zombie-task bug in R1) catches exceptions and logs them, but the events are discarded. The user sees streaming output during the run (via SSE) but the completed task has no stored output — no chat events, no response text, no git diff. The `getTaskOutput` call returns empty.

**Evidence:** Task #294 (engineer) completed successfully but has zero chat_events rows. The migration `_migrate_cascade_fks` left a `_chat_sessions_old` table that caused `no such table` errors on the chat_events INSERT (the session_id FK pointed at the old table). Even after migration cleanup, any future write failure (large batch, DB lock timeout) will silently drop all events.

**Remediation:**
- **Immediate:** Add retry logic to `add_chat_events_batch` — on failure, chunk the batch into smaller pieces and retry. If all retries fail, write events to a fallback file (`<project>/.maistro/lost_events/<task_id>.jsonl`) so they can be recovered.
- **Structural:** The batch write should happen incrementally during streaming, not as one giant dump at the end. Buffer N events (e.g., 50), flush to DB periodically, and do a final flush after the loop. This bounds memory usage and reduces blast radius of a failure.
- **Migration safety:** The cascade FK migration must verify `_old` tables are cleaned up before returning. Add a post-migration check that drops any `_*_old` tables.

**Scope:** `worker.py` (incremental flush), `database.py` (migration cleanup). Small-medium effort.

---

## R12: Thinking blocks not surfaced during streaming

**Problem:** The CLI now emits full `assistant` message events (not `stream_event` deltas) containing `thinking` content blocks. The `_translate_event` function in `cli.py` handles `text` and `tool_use` blocks from `assistant` events but silently drops `thinking` blocks. The user never sees the agent's reasoning during execution.

Additionally, the old `stream_event` / `content_block_delta` path (lines 310-340) handles `thinking_delta` correctly, but the current CLI version (2.1.72+) no longer emits these — it sends complete `assistant` messages instead. The streaming code path is dead code for current CLI versions.

**Evidence:** User's manual CLI test output shows `assistant` events with `{"type":"thinking","thinking":"..."}` content blocks. These are parsed by `_translate_event` into the `assistant` handler (line 343) which iterates content blocks but only checks for `type == "text"` and `type == "tool_use"`.

**Remediation:**
- In `_translate_event`, when processing `assistant` message content blocks, emit `{"type": "thinking", "content": block["thinking"]}` for blocks with `type == "thinking"`.
- Since `assistant` events arrive as complete messages (not streaming deltas), yield one `thinking` event per thinking block. For line-by-line display, the frontend should split on newlines and render incrementally.
- In the frontend `Queue.jsx` SSE handler, add a case for `type === 'thinking'` that appends to a `liveThinking` state variable, rendered in a collapsible/scrollable section above the tool list.
- Keep the `stream_event` / `content_block_delta` path as a fallback for older CLI versions, but add a comment noting it's legacy.

**Scope:** `cli.py` (_translate_event — 3 lines), `Queue.jsx` (SSE handler + render — ~20 lines), `App.css` (thinking panel styles). Small effort, high UX value.

---

## R13: Task execution metadata not captured

**Problem:** The CLI `result` event carries rich execution metadata — `stop_reason`, `num_turns`, `duration_ms`, `total_cost_usd`, `modelUsage` — all silently dropped. None of it is stored or surfaced. Consequences:
- Max-turns exhaustion (`stop_reason: "max_turns"`) looks identical to success — agent gets cut off mid-work with no indication
- No cost tracking per task, per goal, or over time
- No visibility into how many turns a task actually used vs its limit
- Dashboard has no execution efficiency metrics

**Evidence:** Engineer tasks dying mid-work with no indication of why. `stop_reason` and `num_turns` not referenced anywhere in the backend (zero grep hits). `max_turns` hardcoded at 50 in `cli.py:120`, not configurable per goal.

**Remediation:**

1. **Schema** — Add metadata columns to `tasks`:
   - `stop_reason TEXT` — `end_turn`, `max_turns`, etc.
   - `num_turns INTEGER`
   - `duration_ms INTEGER`
   - `cost_usd REAL`
   - `model_usage JSON` — full per-model breakdown from `modelUsage`

2. **CLI event extraction** — In `_translate_event`, yield a `result_meta` event from the `result` event containing all the above fields. Worker stores them on the task row alongside `completed_at`.

3. **Max-turns as goal property** — Add `max_turns` to `job_property_defs` (default 100, type integer). Read in dispatch, pass to `cli.invoke`. Remove the hardcoded 50.

4. **Frontend surfaces** — Task cards show turns used (`12/100`), cost, duration. Max-turns exhaustion gets a distinct warning treatment (not success, not failure). Dashboard can aggregate cost/turns over time.

**Scope:** `database.py` (schema + property def), `cli.py` (extract metadata + accept configurable max_turns), `dispatch.py` (pass max_turns), `worker.py` (store metadata), `Queue.jsx` + `Dashboard.jsx` (render). Medium effort, high value — unlocks cost tracking and operational visibility.

---

## Sequencing

**Phase 1 — Stop the bleeding:**
- R1 (task state machine) — prevents the class of bug we just hit
- R11 (chat events lost) — fix batch write to be incremental, add migration cleanup
- R12 (thinking blocks) — surface agent reasoning in real-time stream
- R13 (max-turns silent death) — surface exhaustion, make turns configurable per goal
- R8 (move glob matching) — trivial, reduces confusion

**Phase 2 — Structural clarity:**
- R2 (split database.py) — unblocks everything else
- R5 (consolidate coalescing) — can be done as part of R2
- R9 (extract routes from main.py) — low risk, high readability

**Phase 3 — Execution robustness:**
- R3 (DB connection coordination) — prevents project-switch race conditions
- R4 (shared CLI harness) — eliminates feature drift between worker and chat
- R7 (async git) — prevents event loop stalls

**Phase 4 — Simplification:**
- R6 (flatten EAV) — reduces per-request overhead, simplifies goal queries
- R10 (project context) — only if multi-project becomes a requirement
