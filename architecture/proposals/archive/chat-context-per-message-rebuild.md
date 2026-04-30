---
title: Cache chat system prompt context instead of rebuilding per message
status: implemented
author: Backend
reviewed-by: Architect
---

## Problem

`_build_chat_context()` in `chat.py` is called on every incoming chat message (line 113). It
executes three queries unconditionally:

```python
jobs = await db.list_jobs()         # 3 SQL queries (jobs + running + all properties)
queue = await db.get_task_queue(limit=10)  # JOIN + GROUP BY
git_summary = git.log_oneline(...)    # subprocess
```

`list_jobs()` itself issues three queries: all jobs, all running task IDs, all job properties.
`get_task_queue()` issues a LEFT JOIN with GROUP BY.
`git.log_oneline()` spawns a subprocess.

For an interactive chat session the user might send several messages in quick succession.
Each one pays the full cost of rebuilding a system prompt that represents state which changes
at most every few seconds.

## Proposed Change

Cache the rendered system prompt with a short TTL (5–10 seconds). Because `_build_chat_context`
is async and the cache is module-level, a simple timestamp check is sufficient:

```python
_chat_context_cache: str | None = None
_chat_context_ts: float = 0.0
_CHAT_CONTEXT_TTL = 8.0  # seconds

async def _build_chat_context() -> str:
    global _chat_context_cache, _chat_context_ts
    now = asyncio.get_event_loop().time()
    if _chat_context_cache is not None and (now - _chat_context_ts) < _CHAT_CONTEXT_TTL:
        return _chat_context_cache
    # ... existing build logic ...
    _chat_context_cache = result
    _chat_context_ts = now
    return result
```

The cache should be invalidated (set to `None`) when the project changes — already handled by
`close_db()` resetting module state on project switch.

## Trade-offs

- **Staleness window**: the system prompt can be up to ~8s out of date. In practice the chat
  assistant is oriented toward status queries, not real-time control, so a brief lag is acceptable.
- **Correctness**: the CLI agent itself reads live state via its own tools (file reads, git log).
  The system prompt is context framing, not authoritative state. Stale framing is low risk.
- **Simplicity**: a TTL cache avoids introducing a pub/sub invalidation mechanism. If tighter
  freshness is needed later, explicit invalidation on task/job mutations can be added.

## Impact

- 3 SQL queries + 1 subprocess eliminated on cache-hit messages.
- Negligible memory cost (one rendered string, typically < 4 KB).
- No schema changes required.
