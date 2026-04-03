---
name: cascade-fk-constraints
description: Schema proposal — add ON DELETE CASCADE to tasks and chat_sessions job foreign keys
type: proposal
status: implemented
raised_by: Backend
reviewed-by: Architect
---

# Proposal: CASCADE deletes on job foreign keys

## Problem

`tasks` and `chat_sessions` both reference the jobs table (`jobs`) but **without** `ON DELETE CASCADE`:

```sql
-- current
tasks.job_id INTEGER NOT NULL REFERENCES jobs(id)
chat_sessions.job_id  INTEGER REFERENCES jobs(id)
```

When a job is deleted, `delete_goal()` manually cleans up both tables before deleting the job row:

```python
await db.execute("DELETE FROM chat_sessions WHERE job_id = ?", (job_id,))
await db.execute("DELETE FROM tasks WHERE job_id = ?", (job_id,))
cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
```

This works but has two risks:

1. **Ordering matters**: if `PRAGMA foreign_keys=ON` is active and either cleanup step is missed or reordered, the `DELETE FROM jobs` will fail with a FK violation.
2. **Application-enforced invariant**: the constraint lives in Python code, not the schema. Any future code path that deletes a job (e.g. bulk deletion, migration) must remember to replicate the cleanup sequence.

## Proposed fix

Add `ON DELETE CASCADE` to both FKs:

```sql
tasks.job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
chat_sessions.job_id  INTEGER REFERENCES jobs(id) ON DELETE CASCADE
```

With CASCADE, `DELETE FROM jobs WHERE id = ?` automatically removes dependent rows, and `delete_goal()` can be simplified to a single statement.

## Migration note

SQLite does not support `ALTER TABLE ... ADD CONSTRAINT`. The change requires:
1. Renaming the existing tables
2. Creating new tables with the updated schema
3. Copying data
4. Dropping the old tables

This is a one-time migration run inside `init_db` (which already uses `executescript` with `CREATE TABLE IF NOT EXISTS`). The migration can be gated on a schema version stored in the `config` table.

## Impact

- `delete_goal()` in `database.py` becomes a single `DELETE FROM jobs`
- No behavioral change for normal operation — job deletion is infrequent
- Enforces referential integrity at the DB level rather than relying on application code ordering

## Architect Review

Accepted. The current schema already uses `ON DELETE CASCADE` on `job_properties` (line 115 of database.py) and on `chat_messages`/`chat_events` → `chat_sessions`. The two missing cascades on `tasks.job_id` and `chat_sessions.job_id` are inconsistencies — the same pattern should apply uniformly.

Migration note: the SQLite table-recreation approach is standard. Run it inside a single transaction with `PRAGMA foreign_keys=OFF` temporarily (required because SQLite enforces FKs during the copy step otherwise). Gate on a schema version in the config table as proposed. The migration should preserve all indexes on the recreated tables.
