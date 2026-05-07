# Remediation

Architectural debt and remediation plan for the mAistro backend. The original
backlog has been worked through. The Shipped appendix at the bottom is the
historical record. The one item left open is a deliberate deferral, not
unfinished work.

---

## R6 (deferred): structural EAV flatten

The minimal fix shipped (see Shipped appendix) — `get_job`, `list_jobs`, and
`get_cascade_targets` now share helpers, and the property schema is cached.
The structural option remains open:

**Problem:** Property definitions are hardcoded in `SEED_SQL`, so the EAV
flexibility (add properties without migrations) is never actually used at
runtime. Three tables still get joined on every property assembly.

**Remediation:** Flatten properties into a `job_config` JSON column on `jobs`.
One column, one parse, no joins. Migration script reads current EAV state
and writes JSON. Property definitions become a Python-side schema (dataclass
or Pydantic model) that validates and applies defaults on read.

**Scope:** Medium — migration + update all property reads/writes.

**When to revisit:** No urgency. Defer until there's a concrete reason —
property-shape changes, an actual performance budget, or operator-extensible
properties become a goal.

---

## Shipped (historical)

For continuity. Each was originally tracked here as architectural debt;
resolved via the indicated mechanism.

| Original item | Status | Resolution |
|---|---|---|
| R1 Task state machine | Shipped | Event-sourced lifecycle in `task_events`; all transitions go through `transition_task` / `transition_tasks_batch`. |
| R2 database.py is a god module | Shipped | Split into `db_core`, `db_jobs`, `db_tasks`, `db_chat`, `db_config`, `db_dashboard`, `db_governor`, `db_migrations`. `database.py` is a re-export shim. |
| R3 DB connection coordination | Shipped | Readers-draining protocol in `db_core` (`db_read_guard`, `close_db`); `state._switching` flag; HTTP layer rejects switch with 409 if a task is active. |
| R4 worker/chat CLI duplication | Obsolete | The standalone chat surface was removed when the Governor replaced it. Nothing left to dedupe. |
| R5 coalescing dispersal (write side) | Shipped | Depth-1 SQLite triggers (`tasks_depth1_*`); `cascade_completion` extracted; lock-at-terminal as emergent policy from pending-only guards. |
| R5 coalescing dispersal (read side) | Shipped | `tasks_resolved` view, `get_task_resolved` helper, all endpoints (output/diff/outcome/stream), `get_recent_tasks_for_governor`, `dashboard_health` migrated. Frontend "residual" turned out to be moot — `get_task_queue` already filters `coalesced_id IS NULL`, so subordinates never render as standalone cards in `Queue.jsx`/`Tasks.jsx`. |
| R6 EAV property tax (minimal) | Shipped | `_get_property_schema()` caches `(default_props, def_types)`; `_overlay_properties()` is the single overlay helper. `get_job`, `list_jobs`, `get_cascade_targets` all call them — no more copy-pasted assembly. |
| R7 async git operations | Shipped | All subprocess-running helpers in `git.py` are async via `asyncio.to_thread`. 60 callsites across worker/scheduler/dispatch/governor/route modules await them. |
| R8 glob matching mis-filed | Shipped | `_any_file_matches` and `_glob_to_regex` extracted to `backend/matching.py` as `any_file_matches` / `glob_to_regex`. |
| R9 main.py route extraction | Shipped | All `*_routes.py` modules extracted; `main.py` is a thin shell (lifespan, CORS, `include_router`). |
| R10 single-tenant project state | Policy, not debt | Locked in by [installer-distribution.md](architecture/proposals/installer-distribution.md) — single-user single-tenant is the deployment shape, not technical debt. |
| R11 chat events lost on batch failure | Shipped | Worker writes each raw NDJSON event incrementally via `add_chat_event`, with per-event try/except. The original "giant batch dump at end" path is gone. |
| R12 thinking blocks not surfaced | Shipped | `_translate_event` in `cli.py` emits `thinking` events from both `thinking_delta` (streaming) and full-block paths. |
| R13 task execution metadata | Shipped | `task_executions` columns (`stop_reason`, `num_turns`, `cost_usd`, `started_at`, `completed_at`), `result_meta` event, `max_turns` as EAV property (default 100), Queue.jsx renders turns + cost. Dashboard aggregation lands in `dashboard_job_impact` alongside per-job commit stats. |
