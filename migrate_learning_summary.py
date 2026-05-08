"""One-shot migration: add the `summary` column to `job_learnings`.

Run against an existing project DB after the introduction of the
breadth/depth learnings access pattern (summary index + on-demand body
fetch). Idempotent — safe to re-run.

Usage:
    python migrate_learning_summary.py <path-to-project>

For each existing row with no summary (the column default is empty
string after the ALTER), the script seeds the summary with a truncated
prefix of the body so operators have something readable to start with.
The agent can refine these via update_learning, or the operator via
the UI.
"""

import os
import sqlite3
import sys


SUMMARY_PREFIX_CHARS = 100


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


NEW_PROPERTY_DEFS = [
    ("max_learnings", "10", "integer"),
    ("max_learning_chars", "1000", "integer"),
]


def migrate(project_dir: str) -> None:
    db_path = os.path.join(project_dir, ".maistro", "maistro.db")
    if not os.path.exists(db_path):
        print(f"No DB at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    try:
        if not _column_exists(conn, "job_learnings", "summary"):
            conn.execute(
                "ALTER TABLE job_learnings ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
            )
            print("Added job_learnings.summary column.")

        rows = conn.execute(
            "SELECT id, body FROM job_learnings WHERE summary = ''"
        ).fetchall()
        for row_id, body in rows:
            seed = (body or "").strip().splitlines()[0] if body else ""
            seed = seed[:SUMMARY_PREFIX_CHARS]
            conn.execute(
                "UPDATE job_learnings SET summary = ? WHERE id = ?",
                (seed, row_id),
            )
        if rows:
            print(f"Seeded summaries for {len(rows)} existing rows.")

        for key, default_value, type_ in NEW_PROPERTY_DEFS:
            conn.execute(
                "INSERT OR IGNORE INTO job_property_defs (key, default_value, type) "
                "VALUES (?, ?, ?)",
                (key, default_value, type_),
            )
        print("Ensured max_learnings / max_learning_chars property defs.")

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1])
