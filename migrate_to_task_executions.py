"""One-shot migration: split execution data out of `tasks` into `task_executions`.

Run against an existing project DB to bring it up to the post-normalization
schema. Idempotent — safe to re-run.

Usage:
    python migrate_to_task_executions.py <path-to-project>

The script:
1. Creates `task_executions` if missing.
2. Copies per-execution columns from `tasks` into `task_executions` for any
   task that ran (i.e. has any non-null execution column).
3. Rebuilds `tasks` without the moved columns.
4. Reconciles any tasks whose events log diverged from the column status
   (the bug that motivated this normalization).

After running this, restart the backend — its `init_db` will recreate the
view and indexes against the new schema.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone


# Columns that move from tasks to task_executions
EXEC_COLS = [
    "session_id", "start_commit", "result_commit",
    "stop_reason", "num_turns", "cost_usd",
    "started_at", "completed_at", "error",
    "worktree_path", "task_branch", "orphan_stash_ref",
]

# Columns that stay on tasks
TASK_COLS = [
    "id", "job_id", "status", "trigger", "trigger_detail", "context",
    "resume_session_id", "approval", "created_at", "queued_at",
    "sort_order", "coalesced_id",
]


def existing_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate(db_path: str):
    if not os.path.isfile(db_path):
        print(f"[skip] no DB at {db_path}")
        return

    print(f"[migrate] {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cols_before = existing_columns(conn, "tasks")
    moved_cols_present = [c for c in EXEC_COLS if c in cols_before]
    print(f"  tasks has {len(cols_before)} columns; {len(moved_cols_present)} will be moved")

    # 1. Create task_executions if it doesn't exist.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_executions (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            session_id TEXT,
            start_commit TEXT,
            result_commit TEXT,
            stop_reason TEXT,
            num_turns INTEGER,
            cost_usd REAL,
            started_at DATETIME,
            completed_at DATETIME,
            error TEXT,
            worktree_path TEXT,
            task_branch TEXT,
            orphan_stash_ref TEXT
        )
    """)
    print("  task_executions table ready")

    # 2. Copy execution data for any task that has any execution column populated.
    if moved_cols_present:
        # Build a SELECT that pulls only the columns that exist (older DBs may
        # be missing the Phase 2/3 columns I added).
        select_cols = ["id"] + moved_cols_present
        placeholders = ", ".join("?" * (1 + len(moved_cols_present)))
        cols_sql = "task_id, " + ", ".join(moved_cols_present)
        # Heuristic for "this task ran": has at least one outcome column populated.
        where_clauses = " OR ".join(f"{c} IS NOT NULL" for c in moved_cols_present)

        rows = cur.execute(
            f"SELECT {', '.join(select_cols)} FROM tasks WHERE {where_clauses}"
        ).fetchall()
        print(f"  copying execution data for {len(rows)} tasks")

        for row in rows:
            cur.execute(
                f"INSERT OR REPLACE INTO task_executions ({cols_sql}) VALUES ({placeholders})",
                tuple(row),
            )

    # 3. Reconcile divergent tasks (events say active but column says queued —
    #    the bug from the original report).
    event_to_status = {
        "dispatched": "pending", "restored": "pending", "queued": "queued",
        "activated": "active", "active": "active",
        "completed": "completed", "exhausted": "exhausted", "failed": "failed",
        "cancelled": "cancelled", "timed_out": "timed_out",
        "interrupted": "interrupted", "rejected": "rejected",
        "created": "pending", "unqueued": "pending",
    }
    rows = cur.execute("""
        SELECT t.id, t.status, (
            SELECT event FROM task_events
            WHERE task_id = t.id
              AND event IN ('dispatched','restored','queued','activated','completed',
                            'exhausted','failed','cancelled','timed_out','interrupted',
                            'rejected','created','unqueued','active')
            ORDER BY id DESC LIMIT 1
        ) AS last_event
        FROM tasks t
    """).fetchall()
    divergent = [
        (r["id"], r["status"], r["last_event"])
        for r in rows
        if r["last_event"] and event_to_status.get(r["last_event"]) != r["status"]
    ]
    if divergent:
        print(f"  reconciling {len(divergent)} divergent tasks:")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for tid, col, ev in divergent:
            derived = event_to_status[ev]
            # If the events log says the task is active or in some intermediate
            # state but the agent never finished, the safe reconciliation is
            # to mark it as interrupted so it leaves the active set cleanly.
            if derived == "active":
                cur.execute(
                    "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                    (tid, "interrupted", "reconciled by migration: column->active divergence", now)
                )
                cur.execute(
                    "UPDATE tasks SET status = 'interrupted' WHERE id = ?",
                    (tid,)
                )
                cur.execute(
                    """INSERT OR IGNORE INTO task_executions (task_id, error, completed_at)
                       VALUES (?, ?, ?)""",
                    (tid, "interrupted: reconciled by migration", now)
                )
                cur.execute(
                    """UPDATE task_executions SET error = COALESCE(error, ?), completed_at = COALESCE(completed_at, ?)
                       WHERE task_id = ?""",
                    ("interrupted: reconciled by migration", now, tid)
                )
                print(f"    #{tid}: column={col} events={ev} -> interrupted")
            else:
                # Column says X, events say Y. Trust the events log.
                cur.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", (derived, tid)
                )
                print(f"    #{tid}: column={col} -> {derived} (matches events)")

    # 4. Rebuild tasks without the moved columns (only if they're still present).
    if moved_cols_present:
        print("  rebuilding tasks table without moved columns")
        # Foreign-key checks have to be off for the rename dance because
        # task_events has FK on tasks(id).
        cur.execute("PRAGMA foreign_keys = OFF")

        # Drop the view + triggers that reference tasks columns; init_db
        # recreates them against the new schema on next backend startup.
        cur.execute("DROP VIEW IF EXISTS tasks_resolved")
        cur.execute("DROP TRIGGER IF EXISTS tasks_depth1_insert")
        cur.execute("DROP TRIGGER IF EXISTS tasks_depth1_update_target")
        cur.execute("DROP TRIGGER IF EXISTS tasks_depth1_update_self")

        # If a prior run was interrupted between the rename and the drop,
        # tasks_old will still exist. Clean it up before renaming.
        cur.execute("DROP TABLE IF EXISTS tasks_old")
        cur.execute("ALTER TABLE tasks RENAME TO tasks_old")
        cur.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                trigger TEXT NOT NULL,
                trigger_detail TEXT,
                context TEXT,
                resume_session_id TEXT,
                approval TEXT,
                created_at DATETIME DEFAULT (datetime('now')),
                queued_at DATETIME,
                sort_order INTEGER,
                coalesced_id INTEGER REFERENCES tasks(id)
            )
        """)
        # Copy only the kept columns.
        kept_in_old = [c for c in TASK_COLS if c in cols_before]
        cur.execute(
            f"INSERT INTO tasks ({', '.join(kept_in_old)}) "
            f"SELECT {', '.join(kept_in_old)} FROM tasks_old"
        )
        cur.execute("DROP TABLE tasks_old")

        # Recreate the indexes that lived on tasks. The view + triggers will
        # be recreated by the backend's init_db on next startup.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_coalesced ON tasks (coalesced_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_job_status ON tasks (job_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_worker ON tasks (status, approval, coalesced_id)")
        # idx_tasks_completed_at moves implicitly: completed_at is no longer on tasks.
        # Drop it so init_db doesn't try to re-create against a missing column.
        cur.execute("DROP INDEX IF EXISTS idx_tasks_completed_at")

        cur.execute("PRAGMA foreign_keys = ON")
        print("  tasks rebuilt")

    conn.commit()
    cur.execute("VACUUM")
    conn.close()
    print("[ok]")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    project = sys.argv[1]
    db_path = os.path.join(project, ".maistro", "maistro.db")
    migrate(db_path)
