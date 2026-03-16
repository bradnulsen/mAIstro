---
name: cascade-fk-constraints
description: Schema proposal — add ON DELETE CASCADE to dispatch_queue and chat_sessions task_id FKs
type: proposal
status: open
raised_by: Backend
---

# Proposal: CASCADE deletes on task_id foreign keys

## Problem

`dispatch_queue` and `chat_sessions` both reference `tasks(id)` but **without** `ON DELETE CASCADE`:

```sql
-- current
dispatch_queue.task_id TEXT NOT NULL REFERENCES tasks(id)
chat_sessions.task_id  TEXT REFERENCES tasks(id)
```

When a task is deleted, `delete_task()` manually cleans up both tables before deleting the task row:

```python
await db.execute("DELETE FROM chat_sessions WHERE task_id = ?", (task_id,))
await db.execute("DELETE FROM dispatch_queue WHERE task_id = ?", (task_id,))
cursor = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
```

This works but has two risks:

1. **Ordering matters**: if `PRAGMA foreign_keys=ON` is active and either cleanup step is missed or reordered, the `DELETE FROM tasks` will fail with a FK violation.
2. **Application-enforced invariant**: the constraint lives in Python code, not the schema. Any future code path that deletes a task (e.g. bulk deletion, migration) must remember to replicate the cleanup sequence.

## Proposed fix

Add `ON DELETE CASCADE` to both FKs:

```sql
dispatch_queue.task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
chat_sessions.task_id  TEXT REFERENCES tasks(id) ON DELETE CASCADE
```

With CASCADE, `DELETE FROM tasks WHERE id = ?` automatically removes dependent rows, and `delete_task()` can be simplified to a single statement.

## Migration note

SQLite does not support `ALTER TABLE ... ADD CONSTRAINT`. The change requires:
1. Renaming the existing tables
2. Creating new tables with the updated schema
3. Copying data
4. Dropping the old tables

This is a one-time migration run inside `init_db` (which already uses `executescript` with `CREATE TABLE IF NOT EXISTS`). The migration can be gated on a schema version stored in the `config` table.

## Impact

- `delete_task()` in `database.py` becomes a single `DELETE FROM tasks`
- No behavioral change for normal operation — task deletion is infrequent
- Enforces referential integrity at the DB level rather than relying on application code ordering
