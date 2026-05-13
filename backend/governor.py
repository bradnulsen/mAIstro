"""Governor — autonomous meta-management agent (thread-based).

Two invocation types share a single in-process lock and queue:

- **Survey** (auto): triggered every 10 terminal tasks via the worker
  counter. Reads project state and open threads. Outputs a JSON array
  of operations (`post` to existing threads, `open_thread` for new
  topics). MCP mode = read.
- **Reply** (human acted): triggered when a human creates a thread or
  posts in an existing one. Reads the focal thread + project state.
  Outputs a single message, optionally with an ``action_payload``.
  Per-thread coalescing — if a reply for thread X is already queued or
  in flight, ``enqueue_reply`` is a no-op (the queued/in-flight run
  reads fresh state). MCP mode = write (the Governor's discretion
  decides whether write tools are actually called).

The lock keeps at most one invocation in flight system-wide so the
Governor reasons across threads against a coherent snapshot.
"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile

from backend import cli, database as db, git, state

log = logging.getLogger("maistro.governor")

_run_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()

_reply_queue: list[int] = []
_dispatcher_task: asyncio.Task | None = None


def spawn(coro) -> asyncio.Task:
    """Schedule a Governor coroutine and hold a strong reference until it
    finishes. asyncio's loop only weakly references tasks; without this,
    a fire-and-forget create_task can be garbage-collected."""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


# ── Public queue interface ─────────────────────────────────


def enqueue_reply(thread_id: int) -> None:
    """Mark a thread as needing a reply invocation. Idempotent.

    Per-thread coalescing: if ``thread_id`` is already in the queue,
    this is a no-op. If a reply for ``thread_id`` is currently in
    flight and a new message arrives, calling this again appends the
    thread to the queue so a follow-up invocation addresses the new
    message after the in-flight one completes.
    """
    if thread_id not in _reply_queue:
        _reply_queue.append(thread_id)
        log.info("[governor] queued reply for thread %d (queue=%d)",
                 thread_id, len(_reply_queue))
    _ensure_dispatcher()


def queue_depth() -> int:
    return len(_reply_queue)


def is_running() -> bool:
    return _run_lock.locked()


def _ensure_dispatcher() -> None:
    """Start the dispatcher coroutine if it isn't running."""
    global _dispatcher_task
    if _dispatcher_task is None or _dispatcher_task.done():
        _dispatcher_task = spawn(_dispatcher_loop())


async def _dispatcher_loop() -> None:
    """Pull thread ids off ``_reply_queue`` and run reply invocations.

    The lock serializes against survey invocations — both go through
    ``_run_lock`` so only one invocation runs at a time.
    """
    while _reply_queue:
        thread_id = _reply_queue[0]
        try:
            await _run_reply(thread_id)
        except Exception:
            log.exception("[governor] reply for thread %d crashed", thread_id)
        if _reply_queue and _reply_queue[0] == thread_id:
            _reply_queue.pop(0)


# ── Entrypoints ────────────────────────────────────────────


async def run_governor(trigger: str, task_count: int | None = None) -> None:
    """Auto-trigger entry point for the worker. ``trigger`` is the run
    type passed through to ``governor_runs.trigger`` — currently
    ``'auto'``. Wraps ``_run_survey`` so the worker doesn't have to
    know about the queue/lock."""
    await _run_survey(task_count=task_count, trigger=trigger)


# ── Survey ─────────────────────────────────────────────────


SURVEY_SYSTEM_PROMPT = """\
You are the mAistro Governor — an autonomous meta-management agent that \
reviews how jobs and tasks are performing within a single project, and \
maintains a thread-based conversation with the operator about it.

## Your Scope
You analyze how the platform is being used and surface observations or \
proposals as messages on **threads**. You do NOT touch project code, do \
NOT dispatch tasks. You read and write only Governor surfaces.

## Threads model
A thread is an ordered conversation about one meta-management topic. \
Open threads are visible to you; closed threads are not — the operator \
muted them. If a topic on a previously-closed thread becomes relevant \
again, you may open a new thread; do not assume the closed one will be \
re-read.

## How Coalescing Works (read this before drawing conclusions)
When tasks are coalesced under a root, only the root actually runs. \
The subordinates inherit the root's outcome (status, completed_at, \
etc.) but their *contexts* are merged into the root's user prompt — so \
the agent that ran the root saw all subordinate contexts. Do NOT infer \
that "only the root has execution metrics" means "only the root's \
context was seen." The agent saw all of them.

## What You Analyze
- Job effectiveness — success rates, failure patterns, turn consumption.
- Scope drift — stale subscriptions, jobs watching files that no longer change.
- Configuration friction — turn limits, timeouts, subscription breadth.
- Implied operator intent — manual dispatch patterns, approval decisions.
- Inter-agent coordination — dispatch chains, missing dependencies.

## Output Format
Output a JSON array of operations. Each operation is one of:

```json
{"op": "post", "thread_id": <int>, "body": "...", "action_payload": {...} | null}
{"op": "open_thread", "title": "...", "body": "...", "action_payload": {...} | null}
```

Action payloads are optional and structured:
- `{"action": "update_job_properties", "job_id": <int>, "properties": {...}}`
- `{"action": "create_job", "name": "...", "description": "...", "properties": {...}}`
- `{"action": "update_queue_settings", "settings": {...}}`

Prefer `post` to an existing thread when the new observation is in scope \
of one. Open a new thread only when the topic doesn't fit any existing one.

A silent survey (empty array `[]`) is fine if there's nothing worth saying.

Output ONLY the JSON array as your final message — no preamble.
"""


async def _run_survey(task_count: int | None, trigger: str = "auto") -> None:
    if _run_lock.locked():
        log.info("[governor] skip survey — already running")
        return

    async with _run_lock:
        project_dir = state.PROJECT_DIR
        if not project_dir:
            log.info("[governor] no project open — skipping survey")
            return

        run_id = await db.create_governor_run(trigger, task_count)
        log.info("[governor] survey run #%d starting", run_id)
        mcp_config_path = None

        try:
            ctx = await _build_survey_context()
            user_prompt = _format_survey_context(ctx)
            mcp_config_path = _write_mcp_config(project_dir, mode="read")

            response_text = await _invoke_cli(
                user_prompt, SURVEY_SYSTEM_PROMPT,
                project_dir, mcp_config_path,
            )

            ops = _parse_survey_ops(response_text)
            applied = await _apply_survey_ops(ops, run_id)

            await db.complete_governor_run(run_id, applied)
            log.info("[governor] survey run #%d done — %d operations applied",
                     run_id, applied)

        except Exception as e:
            log.exception("[governor] survey run #%d failed: %s", run_id, e)
            await db.complete_governor_run(run_id, 0, error=str(e))
        finally:
            if mcp_config_path and os.path.exists(mcp_config_path):
                os.unlink(mcp_config_path)


async def _build_survey_context() -> dict:
    project_dir = state.PROJECT_DIR
    git_log = ""
    if project_dir:
        try:
            git_log = await git.log_oneline(project_dir, limit=30)
        except Exception:
            git_log = "(git log unavailable)"

    return {
        "jobs": await db.list_jobs(),
        "recent_tasks": await db.get_recent_tasks_for_governor(limit=50),
        "health": await db.dashboard_health(7),
        "open_threads": await db.list_open_threads_thin(limit=50),
        "git_log": git_log,
    }


def _format_survey_context(ctx: dict) -> str:
    parts = []
    parts.append(_format_jobs(ctx["jobs"]))
    parts.append(_format_recent_tasks(ctx["recent_tasks"]))
    parts.append(_format_health(ctx["health"]))
    parts.append(_format_git_log(ctx["git_log"]))
    parts.append(_format_open_threads(ctx["open_threads"]))
    parts.append(
        "Review the data above and the open threads. Either post on "
        "existing threads or open new threads, per the output format. "
        "Check open threads first — prefer updating an existing thread "
        "when the new observation is in scope of one."
    )
    return "\n".join(parts)


def _format_open_threads(threads: list[dict]) -> str:
    parts = ["## Open Threads\n"]
    if not threads:
        parts.append("(none — surfacing a new topic opens the first thread)\n")
        return "\n".join(parts)
    for t in threads:
        parts.append(
            f"### Thread #{t['id']} — {t['title']}\n"
            f"opener={t['opener']} last_activity={t['last_activity_at']}"
        )
        for m in t.get("recent_messages", []):
            body_preview = m["body"][:300]
            parts.append(f"- [{m['author']}] {body_preview}")
        parts.append("")
    return "\n".join(parts)


def _parse_survey_ops(text: str) -> list[dict]:
    arr = _extract_json_array(text)
    if not isinstance(arr, list):
        return []
    valid = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        if op == "post":
            tid = item.get("thread_id")
            body = str(item.get("body", "")).strip()
            if isinstance(tid, int) and body:
                valid.append({
                    "op": "post",
                    "thread_id": tid,
                    "body": body,
                    "action_payload": item.get("action_payload"),
                })
        elif op == "open_thread":
            title = str(item.get("title", "")).strip()[:200]
            body = str(item.get("body", "")).strip()
            if title and body:
                valid.append({
                    "op": "open_thread",
                    "title": title,
                    "body": body,
                    "action_payload": item.get("action_payload"),
                })
    return valid


async def _apply_survey_ops(ops: list[dict], run_id: int) -> int:
    """Apply parsed survey operations. Returns the count actually applied.

    Posts to closed threads are silently dropped — closed = muted.
    """
    applied = 0
    for op in ops:
        try:
            if op["op"] == "post":
                thread = await db.get_thread(op["thread_id"])
                if not thread or thread["status"] != "open":
                    log.info("[governor] dropping post to thread %s (not open)",
                             op["thread_id"])
                    continue
                await db.add_message(
                    op["thread_id"], author="governor", body=op["body"],
                    action_payload=op.get("action_payload"), run_id=run_id,
                )
                applied += 1
            elif op["op"] == "open_thread":
                thread_id = await db.create_thread(op["title"], opener="governor")
                await db.add_message(
                    thread_id, author="governor", body=op["body"],
                    action_payload=op.get("action_payload"), run_id=run_id,
                )
                applied += 1
        except Exception:
            log.exception("[governor] failed to apply op %s", op)
    return applied


# ── Reply ──────────────────────────────────────────────────


REPLY_SYSTEM_PROMPT = """\
You are the mAistro Governor responding inside a thread.

## Context
You are reading one focal thread in full plus a snapshot of project \
state. The latest human message in the thread is the **operative input** \
— earlier coalesced messages are context, but the latest expresses the \
current intent. Closed threads are not in your context; do not reference \
them.

## Decide
1. If a prior Governor message in this thread carries an unexecuted \
`action_payload` and the human's latest message clearly affirms it (or \
asks you to proceed), **verify current state first** (read the target \
entity's current values; if the world has moved on since the proposal, \
do not blindly apply — post a clarifying message instead). When verified, \
apply the change via the appropriate write tool and post a result \
message describing what you did.

2. Otherwise, post a single response message. Attach a new \
`action_payload` only if your response is itself a fresh proposal.

When you execute, the response message describes what you did and \
`action_payload` is null. Do not chain proposals onto an execution.

## Output Format
Output a single JSON object (no array, no preamble):

```json
{"body": "...", "action_payload": {...} | null}
```

Action payloads are typed:
- `{"action": "update_job_properties", "job_id": <int>, "properties": {...}}`
- `{"action": "create_job", "name": "...", "description": "...", "properties": {...}}`
- `{"action": "update_queue_settings", "settings": {...}}`
"""

REPLY_FALLBACK_BODY = (
    "[The Governor failed to produce a parseable response. The run is "
    "logged for debugging — try posting again or check the debug drawer.]"
)


async def _run_reply(thread_id: int) -> None:
    async with _run_lock:
        project_dir = state.PROJECT_DIR
        if not project_dir:
            log.info("[governor] no project open — skipping reply for thread %d",
                     thread_id)
            return

        thread = await db.get_thread(thread_id)
        if not thread:
            log.warning("[governor] reply target thread %d gone", thread_id)
            return
        if thread["status"] != "open":
            log.info("[governor] thread %d is closed — skipping reply", thread_id)
            return

        run_id = await db.create_governor_run("reply", task_count=None)
        log.info("[governor] reply run #%d starting (thread=%d)", run_id, thread_id)
        mcp_config_path = None

        try:
            ctx = await _build_reply_context(thread)
            user_prompt = _format_reply_context(ctx)
            mcp_config_path = _write_mcp_config(project_dir, mode="write")

            response_text = await _invoke_cli(
                user_prompt, REPLY_SYSTEM_PROMPT,
                project_dir, mcp_config_path,
            )

            msg = _parse_reply_message(response_text)
            if msg is None:
                await db.add_message(
                    thread_id, author="governor",
                    body=REPLY_FALLBACK_BODY, run_id=run_id,
                )
                await db.complete_governor_run(
                    run_id, 1, error="reply parse failure",
                )
                return

            await db.add_message(
                thread_id, author="governor",
                body=msg["body"],
                action_payload=msg.get("action_payload"),
                run_id=run_id,
            )
            await db.complete_governor_run(run_id, 1)
            log.info("[governor] reply run #%d done", run_id)

        except Exception as e:
            log.exception("[governor] reply run #%d failed: %s", run_id, e)
            try:
                await db.add_message(
                    thread_id, author="governor",
                    body=f"[The Governor encountered an error: {e}. The run is logged.]",
                    run_id=run_id,
                )
            except Exception:
                pass
            await db.complete_governor_run(run_id, 0, error=str(e))
        finally:
            if mcp_config_path and os.path.exists(mcp_config_path):
                os.unlink(mcp_config_path)


async def _build_reply_context(thread: dict) -> dict:
    project_dir = state.PROJECT_DIR
    git_log = ""
    if project_dir:
        try:
            git_log = await git.log_oneline(project_dir, limit=30)
        except Exception:
            git_log = "(git log unavailable)"

    open_threads = await db.list_open_threads_thin(limit=50)
    other_open = [t for t in open_threads if t["id"] != thread["id"]]

    return {
        "thread": thread,
        "jobs": await db.list_jobs(),
        "recent_tasks": await db.get_recent_tasks_for_governor(limit=50),
        "health": await db.dashboard_health(7),
        "other_open_threads": other_open,
        "git_log": git_log,
    }


def _format_reply_context(ctx: dict) -> str:
    parts = []
    thread = ctx["thread"]
    parts.append(f"## Active Thread #{thread['id']} — {thread['title']}\n")
    parts.append(f"opener={thread['opener']}  status={thread['status']}\n")
    parts.append("Messages (oldest first):\n")
    for m in thread.get("messages", []):
        parts.append(f"### [{m['author']}] {m['created_at']}")
        parts.append(m["body"])
        if m.get("action_payload"):
            payload_str = json.dumps(m["action_payload"], indent=2)
            parts.append(f"action_payload:\n```json\n{payload_str}\n```")
        parts.append("")

    parts.append(_format_jobs(ctx["jobs"]))
    parts.append(_format_recent_tasks(ctx["recent_tasks"]))
    parts.append(_format_health(ctx["health"]))
    parts.append(_format_git_log(ctx["git_log"]))

    if ctx["other_open_threads"]:
        parts.append("## Other Open Threads (titles only — for awareness)\n")
        for t in ctx["other_open_threads"]:
            parts.append(f"- #{t['id']}: {t['title']}")
        parts.append("")

    parts.append(
        "Respond inside the active thread per the output format. "
        "If you execute a previously-proposed change, verify current "
        "state first; if it has drifted, post a clarifying message "
        "instead of applying."
    )
    return "\n".join(parts)


def _parse_reply_message(text: str) -> dict | None:
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        return None
    body = str(obj.get("body", "")).strip()
    if not body:
        return None
    return {
        "body": body,
        "action_payload": obj.get("action_payload"),
    }


# ── Shared formatters / helpers ────────────────────────────


def _format_jobs(jobs: list[dict]) -> str:
    parts = ["## Current Jobs\n"]
    if not jobs:
        return "## Current Jobs\n(no jobs configured)\n"
    for j in jobs:
        props = j.get("properties", {})
        parts.append(f"### {j['name']} (id={j['id']})")
        if props.get("summary"):
            parts.append(f"Summary: {props['summary']}")
        if props.get("description"):
            parts.append(f"Description: {props['description'][:500]}")
        parts.append(f"Model: {props.get('model', 'sonnet')}")
        parts.append(f"Max turns: {props.get('max_turns', 100)}")
        parts.append(f"Timeout: {props.get('timeout', 900)}s")
        subs = props.get("subscriptions", [])
        if subs:
            parts.append(f"Subscriptions: {', '.join(subs)}")
        parts.append("")
    return "\n".join(parts)


def _format_recent_tasks(tasks: list[dict]) -> str:
    parts = ["## Recent Tasks (last 50 terminal)\n"]
    parts.append(
        "Note: tasks marked `coalesced→#N` were folded into task #N's "
        "execution. The displayed turns/cost are inherited from #N's "
        "single run; do NOT sum metrics across coalesced subordinates.\n"
    )
    if not tasks:
        parts.append("(no recent tasks)\n")
        return "\n".join(parts)
    for t in tasks:
        line = f"- Task #{t['id']} [{t['job_name']}] status={t['status']}"
        if t.get("is_subordinate"):
            line += f" coalesced→#{t['effective_root_id']}"
        if t.get("num_turns"):
            line += f" turns={t['num_turns']}"
        if t.get("cost_usd"):
            line += f" cost=${t['cost_usd']:.4f}"
        if t.get("error"):
            line += f" error=\"{t['error'][:100]}\""
        line += f" trigger={t.get('trigger', '?')}"
        start = (t.get("start_commit") or "")[:8]
        result = (t.get("result_commit") or "")[:8]
        if start and result:
            line += f" commits={start}..{result}"
        parts.append(line)
    parts.append("")
    return "\n".join(parts)


def _format_health(health: list[dict]) -> str:
    parts = ["## Job Health (7-day window)\n"]
    if not health:
        parts.append("(no health data)\n")
        return "\n".join(parts)
    for h in health:
        total = h.get("total", 0)
        completed = h.get("completed", 0)
        rate = f"{completed/total*100:.0f}%" if total > 0 else "n/a"
        parts.append(
            f"- {h.get('job_name', '?')}: {total} tasks, "
            f"{completed} completed ({rate}), "
            f"{h.get('failed', 0)} failed, "
            f"{h.get('exhausted', 0)} exhausted, "
            f"{h.get('timed_out', 0)} timed out"
        )
    parts.append("")
    return "\n".join(parts)


def _format_git_log(git_log: str) -> str:
    return f"## Recent Git Activity\n{git_log or '(no git log)'}\n"


def _write_mcp_config(project_dir: str, mode: str = "read") -> str:
    """Write a Governor-specific MCP config to a temp file."""
    # See mcp_config.py for the bundled-mode rationale.
    if getattr(sys, "frozen", False):
        cmd, args = sys.executable, ["--governor-mcp"]
    else:
        server_script = os.path.join(os.path.dirname(__file__), "governor_mcp.py")
        cmd, args = sys.executable, [server_script]
    config = {
        "mcpServers": {
            "governor": {
                "type": "stdio",
                "command": cmd,
                "args": args,
                "env": {
                    "MAISTRO_PROJECT_DIR": project_dir,
                    "MAISTRO_BACKEND_PORT": "8420",
                    "MAISTRO_GOVERNOR_MODE": mode,
                },
            }
        }
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="maistro-governor-mcp-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


async def _invoke_cli(
    prompt: str,
    system_prompt: str,
    cwd: str,
    mcp_config_path: str,
) -> str:
    full_response = []
    async for event in cli.invoke(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=cwd,
        model="sonnet",
        mcp_config_path=mcp_config_path,
        max_turns=20,
    ):
        etype = event.get("type", "")
        if etype == "text":
            full_response.append(event.get("content", ""))
        elif etype == "assistant_complete":
            full_response.append(event.get("content", ""))
        elif etype == "error":
            raise RuntimeError(event.get("message", "CLI error"))
    return "".join(full_response)


def _extract_json_array(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _extract_json_object(text: str):
    text = text.strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            v = json.loads(match.group(1).strip())
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(text[start:end + 1])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    return None
