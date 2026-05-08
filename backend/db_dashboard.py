"""Aggregation queries for the operational dashboard.

Each function answers one panel of the dashboard view: per-job task health,
per-job impact (commits + execution cost), and the run timeline.  All take
a window in days and return rows shaped for direct JSON serialization.
"""

from backend import git
from backend.db_core import get_db
from backend.db_jobs import list_jobs
from backend.db_triggers import TERMINAL_STATUSES_SQL


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


async def dashboard_job_impact(window_days: int, project_dir: str) -> list[dict]:
    """Per-job impact summary: what landed in main + what executions cost.

    Two sources, joined on `job_id`:
      - Git side: parses `git log --numstat --since=<window>` for commits
        in the window; attributes by author email (`<slug>@maistro.local`
        for jobs, anything else lumped under the `Operator` pseudo-row).
        Yields commit count, additions, deletions, files-touched.
      - Execution side: aggregates `task_executions` over the same window
        (one row per real execution; subordinates filtered via coalesced_id
        IS NULL on the parent task). Yields total cost_usd, total turns,
        and counts of completed / non-success terminals.

    Returns one row per job (plus the Operator pseudo-row if any operator
    commits exist in the window). Jobs with neither commits nor executions
    in the window are omitted.
    """
    # ── Git side ───────────────────────────────────────────────
    git_log = await git.log(project_dir, limit=10000, with_stats=True)
    cutoff_iso = None
    if window_days:
        # git.log doesn't take --since; do the cutoff client-side from the
        # parsed `date` field (ISO-ish). Convert to a comparable Y-m-d slice.
        from datetime import datetime, timezone, timedelta
        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
        cutoff_iso = cutoff_dt.isoformat()

    jobs = await list_jobs()
    slug_to_job = {j["slug"]: j for j in jobs if j.get("slug")}

    # job_id (or "operator") → impact dict
    impact: dict[str | int, dict] = {}

    def _ensure(key, name):
        if key not in impact:
            impact[key] = {
                "job_id": key if isinstance(key, int) else None,
                "job_name": name,
                "is_operator": key == "operator",
                "commits": 0,
                "insertions": 0,
                "deletions": 0,
                "files_changed": 0,
                "_files_set": set(),
            }
        return impact[key]

    for entry in git_log:
        date_str = entry.get("date") or ""
        if cutoff_iso and date_str < cutoff_iso:
            continue
        email = (entry.get("email") or "").strip()
        # Match the `<slug>@maistro.local` convention from CLAUDE.md
        if email.endswith("@maistro.local"):
            slug = email[: -len("@maistro.local")]
            job = slug_to_job.get(slug)
            if job:
                row = _ensure(job["id"], job["name"])
            else:
                # Stale job slug — operator may have deleted the job after it
                # committed. Bucket under a synthetic "removed-job" name so
                # the commits don't silently disappear.
                row = _ensure(f"removed:{slug}", f"(removed) {slug}")
        else:
            row = _ensure("operator", "Operator")

        row["commits"] += 1
        row["insertions"] += entry.get("insertions", 0)
        row["deletions"] += entry.get("deletions", 0)
        for f in entry.get("files") or []:
            row["_files_set"].add(f)

    # ── Execution side ─────────────────────────────────────────
    db = await get_db()
    exec_rows = await db.execute_fetchall(
        f"""SELECT t.job_id,
                   COUNT(*) AS executions,
                   SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN t.status IN ('failed','exhausted','timed_out','interrupted','cancelled') THEN 1 ELSE 0 END) AS non_success,
                   SUM(COALESCE(te.num_turns, 0)) AS total_turns,
                   SUM(COALESCE(te.cost_usd, 0)) AS total_cost_usd
            FROM tasks t
            JOIN task_executions te ON te.task_id = t.id
            WHERE t.coalesced_id IS NULL
              AND t.status IN {TERMINAL_STATUSES_SQL}
              AND te.completed_at >= datetime('now', ?)
            GROUP BY t.job_id""",
        (f"-{window_days} days",),
    )
    exec_by_job = {r["job_id"]: dict(r) for r in exec_rows}

    # Make sure every job with executions appears in `impact` even with zero commits
    # (failed runs leave no commits but still cost real money).
    for job_id, ex in exec_by_job.items():
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job:
            continue
        _ensure(job_id, job["name"])

    # ── Stitch and finalize ────────────────────────────────────
    results: list[dict] = []
    for key, row in impact.items():
        row["files_changed"] = len(row.pop("_files_set"))
        ex = exec_by_job.get(row["job_id"]) if row["job_id"] is not None else None
        if ex:
            row["executions"] = ex["executions"]
            row["completed"] = ex["completed"]
            row["non_success"] = ex["non_success"]
            row["total_turns"] = ex["total_turns"] or 0
            row["total_cost_usd"] = round(float(ex["total_cost_usd"] or 0), 4)
        else:
            row["executions"] = 0
            row["completed"] = 0
            row["non_success"] = 0
            row["total_turns"] = 0
            row["total_cost_usd"] = 0.0
        results.append(row)

    # Sort: real jobs first (by commits desc), then operator, then removed-job rows
    def _sort_key(r):
        if r.get("is_operator"):
            return (1, 0, 0)
        if r.get("job_id") is None:
            return (2, -r["commits"], 0)
        return (0, -r["commits"], -r["total_cost_usd"])

    results.sort(key=_sort_key)
    return results
