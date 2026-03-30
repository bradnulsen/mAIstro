"""One-time migration: convert job IDs from TEXT slugs to INTEGER AUTOINCREMENT.

Usage:
    python migrate_job_ids.py <path-to-maistro.db>

Backs up the original DB, creates a fresh one with the new schema,
and copies all data with remapped job IDs.
"""

import json
import os
import shutil
import sqlite3
import sys

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(__file__))


def migrate(db_path: str):
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} not found")
        return

    backup_path = db_path + ".pre-int-migration"
    if os.path.exists(backup_path):
        print(f"Backup already exists at {backup_path} — aborting to avoid overwrite")
        return

    # Back up
    shutil.copy2(db_path, backup_path)
    print(f"Backed up to {backup_path}")

    # Open old DB
    old = sqlite3.connect(backup_path)
    old.row_factory = sqlite3.Row

    # Check if already migrated (jobs.id is INTEGER)
    cols = old.execute("PRAGMA table_info(jobs)").fetchall()
    id_col = [c for c in cols if c["name"] == "id"]
    if id_col and id_col[0]["type"].upper() == "INTEGER":
        print("Database already has INTEGER job IDs — nothing to migrate")
        old.close()
        os.remove(backup_path)
        return

    # Read all old data
    old_goals = old.execute("SELECT id, name, created_at FROM jobs").fetchall()
    old_props = old.execute("SELECT job_id, key, value FROM job_properties").fetchall()
    old_tasks = old.execute("SELECT * FROM tasks").fetchall()
    task_cols = [desc[0] for desc in old.execute("SELECT * FROM tasks LIMIT 0").description]
    old_chat_sessions = old.execute("SELECT * FROM chat_sessions").fetchall()
    cs_cols = [desc[0] for desc in old.execute("SELECT * FROM chat_sessions LIMIT 0").description]
    old_chat_messages = old.execute("SELECT * FROM chat_messages").fetchall()
    cm_cols = [desc[0] for desc in old.execute("SELECT * FROM chat_messages LIMIT 0").description]
    old_chat_events = old.execute("SELECT * FROM chat_events").fetchall()
    ce_cols = [desc[0] for desc in old.execute("SELECT * FROM chat_events LIMIT 0").description]
    old_task_events = old.execute("SELECT * FROM task_events").fetchall()
    te_cols = [desc[0] for desc in old.execute("SELECT * FROM task_events LIMIT 0").description]
    old_mcp = old.execute("SELECT * FROM mcp_servers").fetchall()
    mcp_cols = [desc[0] for desc in old.execute("SELECT * FROM mcp_servers LIMIT 0").description]
    old_config = old.execute("SELECT key, value FROM config").fetchall()
    old_prop_defs = old.execute("SELECT * FROM job_property_defs").fetchall()
    pd_cols = [desc[0] for desc in old.execute("SELECT * FROM job_property_defs LIMIT 0").description]
    old.close()

    print(f"Read {len(old_goals)} jobs, {len(old_tasks)} tasks, {len(old_chat_sessions)} sessions")

    # Build slug → int mapping (assign IDs in creation order)
    slug_to_int = {}
    for i, g in enumerate(old_goals, start=1):
        slug_to_int[g["id"]] = i

    print(f"Job ID mapping: {dict((k, v) for k, v in slug_to_int.items())}")

    # Create new DB as separate file, swap at end
    new_path = db_path + ".new"
    if os.path.exists(new_path):
        os.remove(new_path)
    new = sqlite3.connect(new_path)
    new.execute("PRAGMA journal_mode=WAL")
    new.execute("PRAGMA foreign_keys=OFF")  # Temporarily off for bulk insert

    # Import and run schema
    from backend.database import SCHEMA_SQL, SEED_SQL
    new.executescript(SCHEMA_SQL)
    new.executescript(SEED_SQL)

    # Insert jobs with explicit IDs
    for g in old_goals:
        new_id = slug_to_int[g["id"]]
        new.execute(
            "INSERT INTO jobs (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (new_id, g["id"], g["name"], g["created_at"])
        )

    # Insert job_properties with remapped job_id
    for p in old_props:
        new_gid = slug_to_int.get(p["job_id"])
        if new_gid is None:
            print(f"  SKIP orphaned property: job_id={p['job_id']}, key={p['key']}")
            continue

        value = p["value"]
        # Remap cascades_from and allowed_dispatch_targets from slug arrays to int arrays
        if p["key"] in ("cascades_from", "allowed_dispatch_targets"):
            try:
                slugs = json.loads(value)
                if isinstance(slugs, list):
                    ints = [slug_to_int[s] for s in slugs if s in slug_to_int]
                    value = json.dumps(ints)
            except (json.JSONDecodeError, TypeError):
                pass

        new.execute(
            "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
            (new_gid, p["key"], value)
        )

    # Insert tasks with remapped job_id and trigger_detail
    for t in old_tasks:
        row = dict(t)
        old_gid = row["job_id"]
        new_gid = slug_to_int.get(old_gid)
        if new_gid is None:
            print(f"  SKIP orphaned task #{row['id']}: job_id={old_gid}")
            continue
        row["job_id"] = new_gid

        # Remap trigger_detail for cascades (stores upstream job slug)
        if row["trigger"] == "cascade" and row.get("trigger_detail"):
            detail = row["trigger_detail"]
            if detail in slug_to_int:
                row["trigger_detail"] = str(slug_to_int[detail])

        # Remap trigger_detail for agent dispatch ("slug#task_id" → "int#task_id")
        if row["trigger"] == "agent" and row.get("trigger_detail"):
            detail = row["trigger_detail"]
            idx = detail.rfind("#")
            if idx > 0:
                slug_part = detail[:idx]
                task_part = detail[idx+1:]
                if slug_part in slug_to_int:
                    row["trigger_detail"] = f"{slug_to_int[slug_part]}#{task_part}"

        cols_str = ", ".join(task_cols)
        placeholders = ", ".join("?" * len(task_cols))
        new.execute(
            f"INSERT INTO tasks ({cols_str}) VALUES ({placeholders})",
            [row[c] for c in task_cols]
        )

    # Insert chat_sessions with remapped job_id
    for cs in old_chat_sessions:
        row = dict(cs)
        old_gid = row.get("job_id")
        if old_gid:
            row["job_id"] = slug_to_int.get(old_gid)
            if row["job_id"] is None:
                print(f"  SKIP orphaned chat_session {row['id']}: job_id={old_gid}")
                continue
        cols_str = ", ".join(cs_cols)
        placeholders = ", ".join("?" * len(cs_cols))
        new.execute(
            f"INSERT INTO chat_sessions ({cols_str}) VALUES ({placeholders})",
            [row[c] for c in cs_cols]
        )

    # Copy chat_messages, chat_events, task_events as-is (no job_id column)
    for msg in old_chat_messages:
        cols_str = ", ".join(cm_cols)
        placeholders = ", ".join("?" * len(cm_cols))
        new.execute(f"INSERT INTO chat_messages ({cols_str}) VALUES ({placeholders})",
                    [msg[c] for c in cm_cols])

    for evt in old_chat_events:
        cols_str = ", ".join(ce_cols)
        placeholders = ", ".join("?" * len(ce_cols))
        new.execute(f"INSERT INTO chat_events ({cols_str}) VALUES ({placeholders})",
                    [evt[c] for c in ce_cols])

    for evt in old_task_events:
        cols_str = ", ".join(te_cols)
        placeholders = ", ".join("?" * len(te_cols))
        new.execute(f"INSERT INTO task_events ({cols_str}) VALUES ({placeholders})",
                    [evt[c] for c in te_cols])

    # Copy MCP servers
    for srv in old_mcp:
        cols_str = ", ".join(mcp_cols)
        placeholders = ", ".join("?" * len(mcp_cols))
        new.execute(f"INSERT OR IGNORE INTO mcp_servers ({cols_str}) VALUES ({placeholders})",
                    [srv[c] for c in mcp_cols])

    # Copy config with remapped scheduler keys
    for cfg in old_config:
        key = cfg["key"]
        value = cfg["value"]
        if key.startswith("schedule_last_fire_"):
            old_slug = key[len("schedule_last_fire_"):]
            if old_slug in slug_to_int:
                key = f"schedule_last_fire_{slug_to_int[old_slug]}"
            else:
                print(f"  SKIP orphaned config: {key}")
                continue
        new.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))

    new.execute("PRAGMA foreign_keys=ON")
    new.commit()

    # Verify
    count = new.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    task_count = new.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    print(f"\nMigration complete: {count} jobs, {task_count} tasks")

    # Show new mapping
    for row in new.execute("SELECT id, slug, name FROM jobs ORDER BY id"):
        print(f"  {row[0]}: {row[1]} -> {row[2]}")

    new.close()

    # Swap: rename old DB out, new DB in
    old_renamed = db_path + ".old"
    try:
        os.rename(db_path, old_renamed)
    except PermissionError:
        print(f"\nWARNING: Could not rename {db_path} (backend may be running)")
        print(f"New DB is at {new_path} — stop the backend and manually rename:")
        print(f"  mv {db_path} {old_renamed}")
        print(f"  mv {new_path} {db_path}")
        return
    os.rename(new_path, db_path)
    os.remove(old_renamed)
    print(f"\nSwapped: {db_path} is now the migrated database")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_job_ids.py <path-to-maistro.db>")
        print("  e.g.: python migrate_job_ids.py .maistro/maistro.db")
        sys.exit(1)
    migrate(sys.argv[1])
