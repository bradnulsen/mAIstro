"""Public database API — re-exports from the split db_* domain modules.

Schema, connection, and CRUD helpers live in:
- db_core         — connection management, schema, init
- db_migrations   — isolated legacy-DB migrations (do NOT add new ones)
- db_jobs         — job CRUD, EAV property system
- db_triggers     — trigger lifecycle, state machine, coalescing
                    (renamed from db_tasks in Stage 1 of triggers-and-dispatches)
- db_chat         — chat sessions, messages, raw event log
- db_config       — config kv + MCP server registration
- db_dashboard    — operational analytics queries
- db_governor     — Governor counters, runs, findings

New code should import directly from those modules. This file exists so
existing call sites that do `from backend import database as db; db.foo()`
keep working.
"""

# ── Core (connection, schema, init) ────────────────────────
from backend.db_core import (
    DB_PATH,
    SCHEMA_SQL,
    SEED_SQL,
    close_db,
    db_read_guard,
    get_db,
    get_db_path,
    init_db,
)

# ── Jobs ───────────────────────────────────────────────────
from backend.db_jobs import (
    create_job,
    delete_job,
    get_cascade_targets,
    get_job,
    job_for_commit_author,
    list_jobs,
    reorder_jobs,
    slugify,
    update_job,
)

# ── Triggers (CRUD, state machine, coalescing) ─────────────
from backend.db_triggers import (
    LEGAL_TRANSITIONS,
    NON_SUCCESS_TERMINAL_STATUSES,
    PRE_EXECUTION_STATUSES,
    PRE_EXECUTION_STATUSES_SQL,
    TERMINAL_STATUSES,
    TERMINAL_STATUSES_SQL,
    apply_events,
    approve_trigger,
    cascade_completion,
    clear_workspace_pointers,
    coalesce_under,
    compute_durations_from_events,
    enqueue_trigger,
    get_agent_dispatch_depth,
    get_oldest_queued_trigger,
    get_subordinate_triggers,
    get_trigger,
    get_trigger_events,
    get_trigger_events_batch,
    get_trigger_queue,
    get_trigger_resolved,
    get_trigger_status_from_events,
    get_triggers_by_result_commits,
    merge_triggers,
    prune_task_events,
    reject_trigger,
    reorder_triggers,
    split_trigger,
    status_from_events,
    sweep_stale_triggers,
    timestamps_from_events,
    transfer_all_triggers,
    transfer_trigger,
    transition_trigger,
    transition_triggers_batch,
    uncoalesce_trigger,
    update_trigger,
    update_triggers_batch,
)

# ── Chat ───────────────────────────────────────────────────
from backend.db_chat import (
    add_chat_event,
    add_chat_events_batch,
    add_chat_message,
    create_chat_session,
    find_session_by_cli_session,
    get_chat_messages,
    get_chat_session,
    prune_chat_events,
    reconstruct_output_from_events,
    update_chat_session,
)

# ── Config + MCP servers ───────────────────────────────────
from backend.db_config import (
    create_mcp_server,
    delete_mcp_server,
    delete_mcp_server_cascade,
    get_config,
    get_config_prefix,
    get_jobs_referencing_mcp_server,
    list_mcp_servers,
    set_config,
    update_mcp_server_enabled,
    update_mcp_server_fields,
)

# ── Dashboard ──────────────────────────────────────────────
from backend.db_dashboard import (
    dashboard_health,
    dashboard_job_impact,
    dashboard_timeline,
)

# ── Governor ───────────────────────────────────────────────
from backend.db_governor import (
    add_message,
    complete_governor_run,
    count_unexecuted_proposals,
    create_governor_run,
    create_thread,
    get_governor_counter,
    get_governor_debug,
    get_governor_runs,
    get_governor_status,
    get_recent_tasks_for_governor,
    get_thread,
    increment_governor_counter,
    list_open_threads_thin,
    list_threads,
    mark_thread_read,
    reset_governor_counter,
    set_thread_status,
)
