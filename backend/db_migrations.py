"""Isolated migration runner for legacy project DBs.

The project has no general-purpose migration system — schema changes are
made by editing SCHEMA_SQL directly and recreating the dev DB. This module
exists only to keep older project DBs (created during early development)
loadable by handling the table renames and PK conversions that happened
in the goals→jobs and slug→INTEGER transitions.

If you are adding a schema change, do NOT add migration logic here. Edit
SCHEMA_SQL in db_core, recreate the project DB, and move on.
"""

import logging

import aiosqlite

from backend.db_jobs import slugify

log = logging.getLogger("maistro.database")


async def run_migrations(db: aiosqlite.Connection):
    """Apply incremental schema changes to existing databases."""
    # M3: Convert jobs.id from TEXT slug to INTEGER AUTOINCREMENT (run first)
    job_col_types = {c["name"]: c["type"] for c in await db.execute_fetchall("PRAGMA table_info(jobs)")}
    if job_col_types.get("id") == "TEXT":
        log.info("[database] Migration M3: converting jobs.id from TEXT slug to INTEGER")
        await db.execute("PRAGMA foreign_keys=OFF")

        old_jobs = await db.execute_fetchall("SELECT id, name, created_at FROM jobs")
        slug_to_int: dict[str, int] = {j["id"]: i + 1 for i, j in enumerate(old_jobs)}

        # Recreate jobs with INTEGER PK; old id column was the slug
        await db.execute("ALTER TABLE jobs RENAME TO _jobs_old")
        await db.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """)
        for j in old_jobs:
            await db.execute(
                "INSERT INTO jobs (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                (slug_to_int[j["id"]], j["id"], j["name"], j["created_at"])
            )
        await db.execute("DROP TABLE _jobs_old")

        # Recreate job_properties with INTEGER job_id
        old_props = await db.execute_fetchall("SELECT job_id, key, value FROM job_properties")
        await db.execute("ALTER TABLE job_properties RENAME TO _job_properties_old")
        await db.execute("""
            CREATE TABLE job_properties (
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                key TEXT REFERENCES job_property_defs(key),
                value TEXT NOT NULL,
                PRIMARY KEY (job_id, key)
            )
        """)
        for p in old_props:
            new_jid = slug_to_int.get(p["job_id"])
            if new_jid:
                await db.execute(
                    "INSERT OR IGNORE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
                    (new_jid, p["key"], p["value"])
                )
        await db.execute("DROP TABLE _job_properties_old")

        # Recreate tasks with job_id INTEGER
        old_tasks = await db.execute_fetchall("SELECT * FROM tasks")
        task_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(tasks)")]
        await db.execute("ALTER TABLE tasks RENAME TO _tasks_old")
        await db.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                trigger TEXT NOT NULL,
                trigger_detail TEXT,
                context TEXT,
                session_id TEXT,
                resume_session_id TEXT,
                approval TEXT,
                start_commit TEXT,
                stop_reason TEXT,
                num_turns INTEGER,
                cost_usd REAL,
                created_at DATETIME DEFAULT (datetime('now')),
                started_at DATETIME,
                completed_at DATETIME,
                result_commit TEXT,
                error TEXT,
                queued_at DATETIME,
                sort_order INTEGER,
                coalesced_id INTEGER REFERENCES tasks(id)
            )
        """)
        new_task_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(tasks)")]
        copy_cols = [c for c in task_cols if c in set(new_task_cols) and c != "job_id"]
        for t in old_tasks:
            row = dict(t)
            new_jid = slug_to_int.get(row["job_id"])
            if new_jid is None:
                continue
            row["job_id"] = new_jid
            all_cols = ["job_id"] + copy_cols
            vals = [row[c] for c in all_cols]
            ph = ", ".join("?" * len(all_cols))
            await db.execute(f"INSERT INTO tasks ({', '.join(all_cols)}) VALUES ({ph})", vals)
        await db.execute("DROP TABLE _tasks_old")

        # Recreate chat_sessions with job_id INTEGER
        old_sessions = await db.execute_fetchall("SELECT * FROM chat_sessions")
        cs_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")]
        await db.execute("ALTER TABLE chat_sessions RENAME TO _chat_sessions_old")
        await db.execute("""
            CREATE TABLE chat_sessions (
                id TEXT PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                task_id INTEGER REFERENCES tasks(id),
                title TEXT,
                cli_session_id TEXT,
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """)
        new_cs_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")]
        copy_cs_cols = [c for c in cs_cols if c in set(new_cs_cols) and c != "job_id"]
        for s in old_sessions:
            row = dict(s)
            old_jid = row.get("job_id")
            new_jid = slug_to_int.get(old_jid) if old_jid else None
            row["job_id"] = new_jid
            all_cols = ["job_id"] + copy_cs_cols
            vals = [row[c] for c in all_cols]
            ph = ", ".join("?" * len(all_cols))
            await db.execute(f"INSERT INTO chat_sessions ({', '.join(all_cols)}) VALUES ({ph})", vals)
        await db.execute("DROP TABLE _chat_sessions_old")

        await db.execute("PRAGMA foreign_keys=ON")
        log.info("[database] Migration M3: done — %d jobs converted", len(old_jobs))

    # M2: Rename goals→jobs tables and goal_id→job_id columns (run before M1)
    tables = {r["name"] for r in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    if "goals" in tables:
        log.info("[database] Migration M2: renaming goals→jobs tables and columns")
        # SCHEMA_SQL may have already created empty jobs/job_property_defs/job_properties
        # tables — drop them so we can rename the real ones from goals.
        for empty_table in ("jobs", "job_property_defs", "job_properties"):
            if empty_table in tables:
                await db.execute(f"DROP TABLE {empty_table}")
        await db.execute("ALTER TABLE goals RENAME TO jobs")
        await db.execute("ALTER TABLE goal_property_defs RENAME TO job_property_defs")
        await db.execute("ALTER TABLE goal_properties RENAME TO job_properties")
        await db.execute("ALTER TABLE tasks RENAME COLUMN goal_id TO job_id")
        await db.execute("ALTER TABLE chat_sessions RENAME COLUMN goal_id TO job_id")
        # job_properties.goal_id column rename (table was just renamed from goal_properties)
        await db.execute("ALTER TABLE job_properties RENAME COLUMN goal_id TO job_id")
        log.info("[database] Migration M2: done")

    # Cleanup: drop any leftover _*_old tables from previous migration runs
    tables = {r["name"] for r in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    for stale in ("_tasks_old", "_jobs_old", "_job_properties_old", "_chat_sessions_old"):
        if stale in tables:
            await db.execute(f"DROP TABLE {stale}")
            log.warning("[database] Dropped stale migration table: %s", stale)

    # Add cli_session_id to chat_sessions if missing (needed for session resume)
    cs_cols = {c["name"] for c in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")}
    if "cli_session_id" not in cs_cols:
        log.info("[database] Migration: adding cli_session_id to chat_sessions")
        await db.execute("ALTER TABLE chat_sessions ADD COLUMN cli_session_id TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_cli_session "
            "ON chat_sessions (cli_session_id)"
        )
        await db.commit()

    # M1: Add slug column to jobs if missing (added after INTEGER PK migration)
    cols = await db.execute_fetchall("PRAGMA table_info(jobs)")
    col_names = {c["name"] for c in cols}
    if "slug" not in col_names:
        log.info("[database] Migration M1: adding slug column to jobs")
        await db.execute("ALTER TABLE jobs ADD COLUMN slug TEXT")
        rows = await db.execute_fetchall("SELECT id, name FROM jobs")
        for row in rows:
            slug = slugify(row["name"])
            # Ensure uniqueness by appending ID on collision
            existing = await db.execute_fetchall(
                "SELECT id FROM jobs WHERE slug = ? AND id != ?", (slug, row["id"])
            )
            if existing:
                slug = f"{slug}-{row['id']}"
            await db.execute("UPDATE jobs SET slug = ? WHERE id = ?", (slug, row["id"]))
        log.info("[database] Migration M1: done")
