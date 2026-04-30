"""Aggregation queries for the operational dashboard.

Each function answers one panel of the dashboard view: per-job health, run
timeline, agent dispatch chains, and tool-usage stats. All take a window in
days and return rows shaped for direct JSON serialization.
"""

import json
from collections import defaultdict

from backend.db_core import get_db
from backend.db_tasks import TERMINAL_STATUSES_SQL


async def dashboard_health(window_days: int) -> list[dict]:
    """Per-job task counts by terminal state within a time window.

    Returns current window stats and previous-window stats for trend calculation.

    Counts roots only (coalesced_id IS NULL) — subordinates inherit the root's
    terminal status via cascade, so including them would multiply the count by
    the coalesce-group size and distort success-rate metrics.
    """
    db = await get_db()
    sql = f"""
        SELECT
            t.job_id,
            j.name AS job_name,
            COUNT(*) AS total,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN t.status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out,
            SUM(CASE WHEN t.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
            SUM(CASE WHEN t.status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted,
            SUM(CASE WHEN t.status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN t.status = 'exhausted' THEN 1 ELSE 0 END) AS exhausted
        FROM tasks t
        JOIN jobs j ON j.id = t.job_id
        LEFT JOIN task_executions te ON te.task_id = t.id
        WHERE t.status IN {TERMINAL_STATUSES_SQL}
          AND t.coalesced_id IS NULL
          AND te.completed_at >= datetime('now', ?)
        GROUP BY t.job_id, j.name
    """
    current = await db.execute_fetchall(sql, (f"-{window_days} days",))

    # Previous equivalent window for trend comparison
    prev_sql = f"""
        SELECT
            t.job_id,
            COUNT(*) AS total,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed
        FROM tasks t
        LEFT JOIN task_executions te ON te.task_id = t.id
        WHERE t.status IN {TERMINAL_STATUSES_SQL}
          AND t.coalesced_id IS NULL
          AND te.completed_at >= datetime('now', ?)
          AND te.completed_at < datetime('now', ?)
        GROUP BY t.job_id
    """
    prev = await db.execute_fetchall(
        prev_sql, (f"-{window_days * 2} days", f"-{window_days} days")
    )
    prev_by_job = {r["job_id"]: dict(r) for r in prev}

    results = []
    for r in current:
        row = dict(r)
        p = prev_by_job.get(row["job_id"])
        if p and p["total"] > 0:
            row["prev_success_rate"] = p["completed"] / p["total"]
        else:
            row["prev_success_rate"] = None
        results.append(row)
    return results


async def dashboard_timeline(window_days: int) -> list[dict]:
    """Tasks with start/end times for timeline visualization.

    Derives start and end timestamps from task_events (active/terminal pairs)
    rather than the transitional timestamp columns on the task record.
    """
    db = await get_db()
    # Note: this filter is on terminal *events*, not statuses, and intentionally
    # omits 'rejected'. Rejected tasks never activated, so they have no
    # activated→terminal pair to plot — including 'rejected' here would
    # produce timeline rows with no start time. Do not "unify" with
    # TERMINAL_STATUSES_SQL.
    terminal_events_sql = "('completed', 'failed', 'cancelled', 'timed_out', 'interrupted', 'exhausted')"
    rows = await db.execute_fetchall(
        f"""SELECT t.id, t.job_id, j.name AS job_name,
                  te_start.created_at AS started_at,
                  te_end.created_at AS completed_at,
                  tx.error, t.status
           FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           LEFT JOIN task_executions tx ON tx.task_id = t.id
           JOIN task_events te_start ON te_start.task_id = t.id AND te_start.event IN ('activated', 'active')
           LEFT JOIN task_events te_end ON te_end.task_id = t.id
             AND te_end.event IN {terminal_events_sql}
             AND te_end.id = (
               SELECT MAX(e2.id) FROM task_events e2
               WHERE e2.task_id = t.id
                 AND e2.event IN {terminal_events_sql}
             )
           WHERE te_start.created_at >= datetime('now', ?)
           ORDER BY te_start.created_at""",
        (f"-{window_days} days",)
    )
    return [dict(r) for r in rows]


async def dashboard_chains(window_days: int) -> list[dict]:
    """Agent-triggered tasks for dispatch chain visualization."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.id, t.job_id, j.name AS job_name,
                  t.trigger_detail, te.error, te.completed_at
           FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           LEFT JOIN task_executions te ON te.task_id = t.id
           WHERE t.trigger = 'agent'
             AND te.completed_at IS NOT NULL
             AND te.completed_at >= datetime('now', ?)""",
        (f"-{window_days} days",)
    )
    return [dict(r) for r in rows]


async def dashboard_tool_usage(window_days: int) -> list[dict]:
    """Per-job tool frequency and error rates from MCP tool use events."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.job_id, j.name AS job_name, ce.raw_json
           FROM chat_events ce
           JOIN chat_sessions cs ON cs.id = ce.session_id
           JOIN tasks t ON t.id = cs.task_id
           JOIN jobs j ON j.id = t.job_id
           WHERE ce.event_type = 'mcp_tool_use'
             AND ce.created_at >= datetime('now', ?)""",
        (f"-{window_days} days",)
    )

    # Aggregate: per-job tool counts and error counts
    job_tools: dict[int, dict] = {}  # job_id -> {job_name, tools: {tool -> {count, errors}}}
    for r in rows:
        jid = r["job_id"]
        if jid not in job_tools:
            job_tools[jid] = {"job_id": jid, "job_name": r["job_name"], "tools": defaultdict(lambda: {"count": 0, "errors": 0})}
        try:
            data = json.loads(r["raw_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        tool_name = data.get("tool", "unknown")
        job_tools[jid]["tools"][tool_name]["count"] += 1
        result = data.get("result")
        if isinstance(result, str) and ("error" in result.lower() or "Error" in result):
            job_tools[jid]["tools"][tool_name]["errors"] += 1
        elif isinstance(result, dict) and result.get("isError"):
            job_tools[jid]["tools"][tool_name]["errors"] += 1

    # Convert defaultdicts to plain dicts for JSON serialization
    return [
        {
            "job_id": v["job_id"],
            "job_name": v["job_name"],
            "tools": {k: dict(c) for k, c in v["tools"].items()},
        }
        for v in job_tools.values()
    ]
