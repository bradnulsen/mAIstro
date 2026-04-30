"""Recovery script for project DBs corrupted by the buggy
migrate_to_task_executions.py rename pass.

Symptom: enqueue_task (or any insert into task_events / task_executions /
chat_sessions) fails with `sqlite3.OperationalError: no such table:
main.tasks_old`.

Cause: SQLite ≥ 3.25 with `legacy_alter_table=OFF` (the modern default)
rewrites foreign-key references in dependent tables when their parent is
ALTER TABLE … RENAME'd. The migration script renamed `tasks` to
`tasks_old`, then later dropped `tasks_old`, but the FK references in
`task_events`, `task_executions`, and `chat_sessions` had already been
rewritten to point at `tasks_old` — which now doesn't exist.

Fix: rebuild each affected table with `legacy_alter_table=ON` so the
rename doesn't recurse the corruption, then copy data and re-create
indexes. This is the SQLite-canonical way to repair FK references and
is unconditional (unlike the `writable_schema` PRAGMA approach, which
VACUUM can clobber).

Idempotent — safe to re-run.

Usage:
    python recover_tasks_old_fk.py <project_dir>

Backs up the DB to .maistro/maistro.db.pre-fk-recover before changing it.
"""
import os
import shutil
import sqlite3
import sys


def recover(db_path: str):
    if not os.path.isfile(db_path):
        print(f"[error] {db_path}: not a file")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    affected = cur.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND sql LIKE '%tasks_old%'"
    ).fetchall()

    if not affected:
        print(f"[ok] {db_path}: no tables reference tasks_old, nothing to do")
        conn.close()
        return

    print(f"[detected] {db_path}: {len(affected)} tables reference tasks_old:")
    for name, _ in affected:
        print(f"  - {name}")

    backup_path = db_path + ".pre-fk-recover"
    if not os.path.exists(backup_path):
        shutil.copy2(db_path, backup_path)
        print(f"[backup] {backup_path}")
    else:
        print(f"[backup] already exists: {backup_path} (not overwriting)")

    # Capture the indexes we need to recreate after each rebuild.
    # Renaming and dropping the old table also drops its indexes, so we
    # rebuild them from their original CREATE statements.
    table_indexes: dict[str, list[str]] = {}
    for name, _ in affected:
        idx_rows = cur.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (name,)
        ).fetchall()
        table_indexes[name] = [row[0] for row in idx_rows]

    try:
        cur.execute("PRAGMA foreign_keys = OFF")
        # CRITICAL: tells SQLite NOT to rewrite FK references in dependent
        # tables when we rename — otherwise this script would recreate
        # the very corruption it's trying to fix.
        cur.execute("PRAGMA legacy_alter_table = ON")

        for name, old_sql in affected:
            # Substitute the corrupted FK reference back to `tasks`.
            # Both quoted ("tasks_old") and bare (tasks_old) forms get
            # replaced; SQLite emits the quoted form for FK references
            # but bare elsewhere.
            new_sql = old_sql.replace('"tasks_old"', '"tasks"')
            new_sql = new_sql.replace('tasks_old', 'tasks')
            assert "tasks_old" not in new_sql, f"failed to scrub tasks_old from {name}"

            tmp_name = f"{name}_recover_tmp"
            cur.execute(f'DROP TABLE IF EXISTS "{tmp_name}"')
            cur.execute(f'ALTER TABLE "{name}" RENAME TO "{tmp_name}"')
            cur.execute(new_sql)
            cur.execute(f'INSERT INTO "{name}" SELECT * FROM "{tmp_name}"')
            cur.execute(f'DROP TABLE "{tmp_name}"')

            # Recreate indexes that were on the original table.
            for idx_sql in table_indexes.get(name, []):
                cur.execute(idx_sql)
            print(f"  rebuilt {name} ({len(table_indexes.get(name, []))} indexes restored)")

        cur.execute("PRAGMA legacy_alter_table = OFF")
        cur.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            print(f"[error] database is locked — stop the backend first, then re-run.")
            print(f"        the backup at {backup_path} is safe; this script is idempotent.")
            conn.close()
            sys.exit(3)
        raise

    # Verify
    remaining = cur.execute(
        "SELECT name FROM sqlite_master WHERE sql LIKE '%tasks_old%'"
    ).fetchall()
    if remaining:
        print(f"[error] still references tasks_old after fix: {remaining}")
        conn.close()
        sys.exit(2)

    fk_violations = cur.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        print(f"[warning] foreign-key violations after fix: {fk_violations}")
    conn.close()

    print("[ok] schema repaired — restart the backend so its connection picks up the new schema")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    project = sys.argv[1]
    db_path = os.path.join(project, ".maistro", "maistro.db")
    recover(db_path)
