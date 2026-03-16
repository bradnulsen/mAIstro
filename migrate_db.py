"""One-shot migration: port maistro_backup.db (old schema) -> maistro.db (new schema).

Run once from repo root:
    python migrate_db.py

Reads from .maistro/maistro_backup.db, writes to .maistro/maistro.db (must not exist).
"""

import json
import os
import sqlite3
import sys

MAISTRO_DIR = os.path.join(os.path.dirname(__file__), ".maistro")
BACKUP = os.path.join(MAISTRO_DIR, "maistro_backup.db")
TARGET = os.path.join(MAISTRO_DIR, "maistro.db")

if not os.path.exists(BACKUP):
    print(f"Backup not found: {BACKUP}")
    sys.exit(1)

if os.path.exists(TARGET):
    print(f"Target already exists: {TARGET}")
    print("Delete it first if you want to re-run the migration.")
    sys.exit(1)

# ── Connect ──────────────────────────────────────────────

old = sqlite3.connect(BACKUP)
old.row_factory = sqlite3.Row

new = sqlite3.connect(TARGET)
new.execute("PRAGMA journal_mode=WAL")
new.execute("PRAGMA foreign_keys=OFF")  # defer FK checks during migration

# ── Create new schema ────────────────────────────────────

new.executescript("""
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE job_property_defs (
    key TEXT PRIMARY KEY,
    default_value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string'
);

CREATE TABLE job_properties (
    job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT REFERENCES job_property_defs(key),
    value TEXT NOT NULL,
    PRIMARY KEY (job_id, key)
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    trigger TEXT NOT NULL,
    trigger_detail TEXT,
    context TEXT,
    session_id TEXT,
    resume_session_id TEXT,
    approval TEXT,
    start_commit TEXT,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    result_commit TEXT,
    error TEXT,
    sort_order INTEGER,
    rating TEXT,
    coalesced_id INTEGER REFERENCES tasks(id)
);

CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    task_id INTEGER REFERENCES tasks(id),
    title TEXT,
    cli_session_id TEXT,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE chat_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE mcp_servers (
    name TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1
);

CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indices
CREATE INDEX idx_tasks_pending ON tasks (started_at, error, sort_order, created_at);
CREATE INDEX idx_tasks_job_coalesce ON tasks (job_id, started_at, error);
CREATE INDEX idx_tasks_running ON tasks (started_at, completed_at);
CREATE INDEX idx_tasks_coalesced ON tasks (coalesced_id);
CREATE INDEX idx_chat_messages_session ON chat_messages (session_id, created_at);
CREATE INDEX idx_chat_events_session ON chat_events (session_id);
CREATE INDEX idx_chat_sessions_cli_session ON chat_sessions (cli_session_id);
CREATE INDEX idx_chat_sessions_task ON chat_sessions (task_id);
CREATE INDEX idx_chat_sessions_job ON chat_sessions (job_id);
CREATE INDEX idx_job_properties_key ON job_properties (key);
""")

# ── Port data ────────────────────────────────────────────

# 1. jobs <- tasks (old config table)
print("Porting jobs...")
for row in old.execute("SELECT * FROM tasks"):
    new.execute("INSERT INTO jobs VALUES (?, ?, ?)",
                (row["id"], row["name"], row["created_at"]))
print(f"  {new.execute('SELECT count(*) FROM jobs').fetchone()[0]} jobs")

# 2. job_property_defs <- task_property_defs (rename coalesce_dispatches -> coalesce_tasks)
print("Porting property defs...")
for row in old.execute("SELECT * FROM task_property_defs"):
    key = row["key"]
    if key == "coalesce_dispatches":
        key = "coalesce_tasks"
    # Skip old keys that are no longer in the schema
    if key in ("base_tools", "disallowed_tools", "cooldown_seconds"):
        continue
    new.execute("INSERT OR IGNORE INTO job_property_defs VALUES (?, ?, ?)",
                (key, row["default_value"], row["type"]))
# Seed any missing defs
new.executescript("""
INSERT OR IGNORE INTO job_property_defs (key, default_value, type) VALUES
    ('description', '', 'string'),
    ('instructions', '', 'string'),
    ('model', 'sonnet', 'string'),
    ('allowed_tools', '[]', 'json'),
    ('mcp_servers', '[]', 'json'),
    ('subscriptions', '[]', 'json'),
    ('coalesce_tasks', 'false', 'boolean'),
    ('sort_order', '0', 'integer'),
    ('schedule', '', 'string'),
    ('timeout', '900', 'integer'),
    ('depends_on', '[]', 'json'),
    ('require_approval', 'false', 'boolean');
""")
print(f"  {new.execute('SELECT count(*) FROM job_property_defs').fetchone()[0]} defs")

# 3. job_properties <- task_properties (rename key coalesce_dispatches -> coalesce_tasks, skip dead keys)
print("Porting job properties...")
for row in old.execute("SELECT * FROM task_properties"):
    key = row["key"]
    if key == "coalesce_dispatches":
        key = "coalesce_tasks"
    if key in ("base_tools", "disallowed_tools", "cooldown_seconds"):
        continue
    new.execute("INSERT OR IGNORE INTO job_properties VALUES (?, ?, ?)",
                (row["task_id"], key, row["value"]))
print(f"  {new.execute('SELECT count(*) FROM job_properties').fetchone()[0]} properties")

# 4. tasks <- dispatch_queue (atomize: extract trigger/context from triggers JSON)
print("Porting tasks (dispatch_queue -> tasks)...")
for row in old.execute("SELECT * FROM dispatch_queue ORDER BY id"):
    row = dict(row)
    trigger = row.get("trigger") or "manual"
    trigger_detail = row.get("trigger_detail")
    context = None

    # Extract context from the old triggers JSON array
    triggers_json = row.get("triggers")
    if triggers_json:
        try:
            entries = json.loads(triggers_json)
            if entries and isinstance(entries, list):
                # Combine all context entries
                contexts = [e.get("context") for e in entries if e.get("context")]
                if contexts:
                    context = "\n\n".join(contexts)
        except (json.JSONDecodeError, TypeError):
            pass

    new.execute(
        """INSERT INTO tasks (id, job_id, trigger, trigger_detail, context,
           session_id, resume_session_id, approval, start_commit,
           created_at, started_at, completed_at, result_commit,
           error, sort_order, rating, coalesced_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], row["task_id"], trigger, trigger_detail, context,
         row.get("session_id"), row.get("resume_session_id"),
         row.get("approval"), row.get("start_commit"),
         row.get("created_at"), row.get("started_at"),
         row.get("completed_at"), row.get("result_commit"),
         row.get("error"), row.get("sort_order"),
         row.get("rating"), row.get("coalesced_id"))
    )
print(f"  {new.execute('SELECT count(*) FROM tasks').fetchone()[0]} tasks")

# 5. chat_sessions (old: task_id=job ref, dispatch_id=task ref -> new: job_id, task_id)
print("Porting chat sessions...")
for row in old.execute("SELECT * FROM chat_sessions"):
    row = dict(row)
    new.execute(
        "INSERT INTO chat_sessions (id, job_id, task_id, title, cli_session_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], row.get("task_id"), row.get("dispatch_id"), row.get("title"),
         row.get("cli_session_id"), row.get("created_at"))
    )
print(f"  {new.execute('SELECT count(*) FROM chat_sessions').fetchone()[0]} sessions")

# 6. chat_messages (1:1 copy)
print("Porting chat messages...")
for row in old.execute("SELECT * FROM chat_messages"):
    new.execute("INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["session_id"], row["role"], row["content"], row["created_at"]))
print(f"  {new.execute('SELECT count(*) FROM chat_messages').fetchone()[0]} messages")

# 7. chat_events (1:1 copy)
print("Porting chat events...")
for row in old.execute("SELECT * FROM chat_events"):
    new.execute("INSERT INTO chat_events VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["session_id"], row["event_type"], row["raw_json"], row["created_at"]))
print(f"  {new.execute('SELECT count(*) FROM chat_events').fetchone()[0]} events")

# 8. mcp_servers (1:1 copy)
print("Porting MCP servers...")
for row in old.execute("SELECT * FROM mcp_servers"):
    new.execute("INSERT INTO mcp_servers VALUES (?, ?, ?, ?, ?)",
                (row["name"], row["command"], row["args"], row["env"], row["enabled"]))
print(f"  {new.execute('SELECT count(*) FROM mcp_servers').fetchone()[0]} servers")

# 9. config (1:1 copy, rename schedule keys)
print("Porting config...")
for row in old.execute("SELECT * FROM config"):
    new.execute("INSERT OR IGNORE INTO config VALUES (?, ?)", (row["key"], row["value"]))
# Ensure default
new.execute("INSERT OR IGNORE INTO config VALUES ('queue_auto_dispatch', 'false')")
print(f"  {new.execute('SELECT count(*) FROM config').fetchone()[0]} config entries")

# ── Finalize ─────────────────────────────────────────────

new.execute("PRAGMA foreign_keys=ON")
new.commit()
new.close()
old.close()

print("\nMigration complete!")
print(f"  {BACKUP} -> {TARGET}")
