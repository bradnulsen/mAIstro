# Remediation

Architectural debt and remediation plan for the mAistro backend. Live items are at the top with full design context. The bulk of the original list has shipped — see the Shipped appendix at the bottom for the historical record.

---

## R6: EAV property system pays ongoing tax for unused flexibility

**Problem:** Every `get_job` call joins three tables, builds defaults, overlays overrides, and casts types. The assembly code is copy-pasted across `get_job`, `list_jobs`, and `get_jobs_depending_on`. Property definitions are hardcoded in `SEED_SQL` — the EAV flexibility (add properties without migrations) is never actually used at runtime.

**Remediation:** Two options depending on appetite:
- **Minimal:** Extract a `_assemble_job_properties(job_id_or_rows, prop_rows, running_ids)` helper that all three consumers call. Eliminates the copy-paste without changing the data model.
- **Structural:** Flatten properties into a `job_config` JSON column on `jobs`. One column, one parse, no joins. Migration script reads current EAV state and writes JSON. Property definitions become a Python-side schema (dataclass or Pydantic model) that validates and applies defaults on read.

**Scope:** Minimal fix is small (one helper in `db_jobs.py`). Structural fix is medium — migration + update all property reads/writes.

---

## R7: git.py blocks the async event loop

**Problem:** All git operations use synchronous `subprocess.run`, blocking the event loop. The worker calls `git.head_hash()` while holding `_lock`. The scheduler calls it every 30 seconds. Feed routes call `git.log` with `--numstat`. On large repos or slow storage, any call can block for hundreds of milliseconds.

**Remediation:**
- Wrap `run_git` in `asyncio.to_thread` (or `loop.run_in_executor`) and make all callers `await` it.
- Alternative: provide an `async_run_git` alongside the sync version for the transition period.
- The `cli.py` module already handles this correctly with `Popen` + thread readers — `git.py` should follow the same pattern or use the simpler `to_thread` wrapper.

**Scope:** `git.py` (add async wrappers), all callers in `dispatch.py`, `worker.py`, `scheduler.py`, `feed_routes.py`, `git_routes.py`. Mechanical but wide-reaching — every `git.foo()` becomes `await git.foo()`.

---

## R8: Subscription glob matching lives in the prompt assembly module

**Problem:** `_any_file_matches` and `_glob_to_regex` in `dispatch.py` are the core watch-trigger mechanism — they determine which jobs fire on commits. This is a triggering/matching concern, not a prompt assembly concern.

**Remediation:** Move glob matching to `git.py` (which already has `resolve_glob_files`) or a dedicated `matching.py`. The `check_watch_triggers` function that calls it should move to wherever trigger evaluation lives.

**Scope:** Small. Move two functions + one caller. No behavior change.

---

## R5 residual: subordinate task cards render null metrics

**Problem:** `Queue.jsx` and `Tasks.jsx` render task cards by reading `task.num_turns`, `task.cost_usd`, etc. directly. For coalesced subordinates, those columns are null on the row (the actual outcome lives on the root's `task_executions`). The user sees blank cells instead of "inherits from root."

**Remediation:** Two paths:
- Render `coalesced→#N (inherits #N's metrics)` for subordinates and skip the metric cells entirely.
- Or read through `tasks_resolved` so subordinate cards inherit root values and look indistinguishable.

The structural fix shipped — `tasks_resolved` view, `get_task_resolved` helper, output/diff/outcome endpoints, dashboard, governor all switched. This is the last consumer not yet migrated, and it's cosmetic rather than correctness. Touch when those surfaces are reworked for any other reason.

**Scope:** Tiny. Two frontend components, one helper.

---

## R13 residual: dashboard cost / turns aggregation

**Problem:** Per-task execution metadata (`stop_reason`, `num_turns`, `cost_usd`, `started_at`, `completed_at`) is now captured on `task_executions` and rendered on Queue cards. The dashboard does not aggregate any of it — there is no per-job cost-over-time, no turns-vs-limit chart, no efficiency view.

**Remediation:** Add a `dashboard_cost_efficiency(window_days)` query to `db_dashboard.py` that joins `tasks t JOIN task_executions te WHERE t.coalesced_id IS NULL` (the per-execution profile) and aggregates by `job_id`. Render in the Dashboard alongside Job Health and Job Impact (the latter coming from the dashboard reorientation proposal).

**Scope:** One backend query + one frontend panel. Fits naturally into the dashboard reorientation work — recommend bundling them.

---

## Sequencing

Live work, ordered by leverage:

1. **R7 async git** — touches everything else. Doing this first means subsequent work doesn't bake in more sync git calls.
2. **R8 glob matching** — trivial; reduces module mis-attribution. Pair with R7 if convenient.
3. **R6 EAV** — minimal helper extraction is one PR; structural flatten is a separate decision point.
4. **R5 residual + R13 residual** — bundle into the [dashboard reorientation](architecture/proposals/dashboard-commits-reorientation.md) work, since that's already touching the affected surfaces.

Bigger structural items (Governor Threads, Job Learnings, Installer) live as proposals in `architecture/proposals/` rather than remediation — they're new capability, not debt repayment.

---

## Shipped (historical)

For continuity. Each was originally tracked here as architectural debt; resolved via the indicated mechanism.

| Original item | Status | Resolution |
|---|---|---|
| R1 Task state machine | Shipped | Event-sourced lifecycle in `task_events`; all transitions go through `transition_task` / `transition_tasks_batch`. |
| R2 database.py is a god module | Shipped | Split into `db_core`, `db_jobs`, `db_tasks`, `db_chat`, `db_config`, `db_dashboard`, `db_governor`, `db_migrations`. `database.py` is a re-export shim. |
| R3 DB connection coordination | Shipped | Readers-draining protocol in `db_core` (`db_read_guard`, `close_db`); `state._switching` flag; HTTP layer rejects switch with 409 if a task is active. |
| R4 worker/chat CLI duplication | Obsolete | The standalone chat surface was removed when the Governor replaced it. Nothing left to dedupe. |
| R5 coalescing dispersal (write side) | Shipped | Depth-1 SQLite triggers (`tasks_depth1_*`); `cascade_completion` extracted; lock-at-terminal as emergent policy from pending-only guards. |
| R5 coalescing dispersal (read side) | Mostly shipped | `tasks_resolved` view, `get_task_resolved` helper, all endpoints (output/diff/outcome/stream), `get_recent_tasks_for_governor`, `dashboard_health` migrated. Frontend cosmetic residual tracked above. |
| R9 main.py route extraction | Shipped | All `*_routes.py` modules extracted; `main.py` is a thin shell (lifespan, CORS, `include_router`). |
| R10 single-tenant project state | Policy, not debt | Locked in by [installer-distribution.md](architecture/proposals/installer-distribution.md) — single-user single-tenant is the deployment shape, not technical debt. |
| R11 chat events lost on batch failure | Shipped | Worker writes each raw NDJSON event incrementally via `add_chat_event`, with per-event try/except. The original "giant batch dump at end" path is gone. |
| R12 thinking blocks not surfaced | Shipped | `_translate_event` in `cli.py` emits `thinking` events from both `thinking_delta` (streaming) and full-block paths. |
| R13 task execution metadata | Mostly shipped | `task_executions` columns (`stop_reason`, `num_turns`, `cost_usd`, `started_at`, `completed_at`), `result_meta` event, `max_turns` as EAV property (default 100), Queue.jsx renders turns + cost. Dashboard aggregation residual tracked above. |
