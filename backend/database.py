"""Public database API — re-exports from the split db_* domain modules.

Schema, connection, and CRUD helpers live in:
- db_core         — connection management, schema, init
- db_migrations   — isolated legacy-DB migrations (do NOT add new ones)
- db_jobs         — job CRUD, EAV property system
- db_tasks        — task lifecycle, state machine, coalescing
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
    list_jobs,
    reorder_jobs,
    slugify,
    update_job,
)

# ── Tasks (CRUD, state machine, coalescing) ────────────────
from backend.db_tasks import (
    LEGAL_TRANSITIONS,
    PRE_EXECUTION_STATUSES,
    PRE_EXECUTION_STATUSES_SQL,
    TERMINAL_STATUSES,
    TERMINAL_STATUSES_SQL,
    apply_events,
    approve_task,
    cascade_completion,
    coalesce_under,
    compute_durations_from_events,
    enqueue_task,
    get_agent_dispatch_depth,
    get_oldest_queued_task,
    get_subordinate_tasks,
    get_task,
    get_task_events,
    get_task_events_batch,
    get_task_queue,
    get_task_resolved,
    get_task_status_from_events,
    merge_tasks,
    reject_task,
    reorder_tasks,
    split_task,
    status_from_events,
    sweep_stale_tasks,
    timestamps_from_events,
    transfer_all_tasks,
    transfer_task,
    transition_task,
    transition_tasks_batch,
    uncoalesce_task,
    update_task,
    update_tasks_batch,
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
    dashboard_chains,
    dashboard_health,
    dashboard_timeline,
    dashboard_tool_usage,
)

# ── Governor ───────────────────────────────────────────────
from backend.db_governor import (
    complete_governor_run,
    create_governor_finding,
    create_governor_run,
    get_governor_counter,
    get_governor_finding,
    get_governor_findings,
    get_governor_runs,
    get_governor_status,
    get_recent_tasks_for_governor,
    increment_governor_counter,
    reset_governor_counter,
    update_governor_finding,
)
