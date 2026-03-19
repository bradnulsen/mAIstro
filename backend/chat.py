"""Chat routes — executive assistant conversation interface.

Manages chat sessions, SSE streaming, and CLI invocation for
the conversational chat feature (distinct from task dispatches).
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import cli, database as db, git
from backend import state
from backend.state import require_project

log = logging.getLogger("maistro.chat")

# ── Chat context cache (TTL-based) ───────────────────────
_chat_context_cache: str | None = None
_chat_context_ts: float = 0.0
_CHAT_CONTEXT_TTL = 30.0  # seconds — context only changes on job edits or commits

def invalidate_chat_context_cache():
    """Clear the cached system prompt. Call on project close/switch."""
    global _chat_context_cache, _chat_context_ts
    _chat_context_cache = None
    _chat_context_ts = 0.0


router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Pydantic Models ────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    context: str | None = None


# ── System Prompt ──────────────────────────────────────────

CHAT_SYSTEM_PROMPT = """\
You are the mAistro executive assistant — an intelligent coordinator for a development engine \
where LLM-powered tasks coordinate through git.

You have full awareness of the project's administrative state (jobs, tasks, configuration) \
and its content (files, git history). Help the user understand project status, plan work, \
troubleshoot issues, and manage their jobs.

You can read files, search code, and analyze the project. Always commit after making code changes. \
You are NOT running a task dispatch — you are having a conversation with the human operator.

## Working Directory
{project_dir}

## Current Jobs
{job_summary}

## Recent Tasks
{task_summary}

## Recent Git Activity
{git_summary}"""


async def _build_chat_context() -> str:
    """Build the mAistro executive assistant system prompt with live state.

    Uses a short TTL cache to avoid rebuilding on every chat message —
    the underlying state (jobs, queue, git log) changes at most every
    few seconds.
    """
    global _chat_context_cache, _chat_context_ts
    now = time.monotonic()
    if _chat_context_cache is not None and (now - _chat_context_ts) < _CHAT_CONTEXT_TTL:
        return _chat_context_cache

    jobs = await db.list_jobs()
    job_lines = []
    for j in jobs:
        props = j["properties"]
        status = "RUNNING" if props.get("running") else "idle"
        desc = props.get("description") or "(no description)"
        watch = "watch" if props.get("subscriptions") else "manual"
        job_lines.append(f"- **{j['name']}** [{watch}, {status}] — {desc}")
    job_summary = "\n".join(job_lines) if job_lines else "(no jobs configured)"

    queue = await db.get_task_queue(limit=10)
    task_lines = []
    for t in (queue or []):
        status = "error" if t.get("error") else "completed" if t.get("completed_at") else "running" if t.get("started_at") else "pending"
        task_lines.append(f"- #{t['id']} {t.get('job_name', '?')} [{status}] {t.get('created_at', '')}")
    task_summary = "\n".join(task_lines) if task_lines else "(no recent tasks)"

    git_summary = git.log_oneline(state.PROJECT_DIR) or "(no commits)"

    result = CHAT_SYSTEM_PROMPT.format(
        project_dir=state.PROJECT_DIR,
        job_summary=job_summary,
        task_summary=task_summary,
        git_summary=git_summary,
    )
    _chat_context_cache = result
    _chat_context_ts = now
    return result


# Active chat streams — background tasks push events here, SSE reads from here.
_active_chats: dict[str, asyncio.Queue] = {}


# ── Routes ─────────────────────────────────────────────────

@router.post("/")
async def chat(req: ChatRequest):
    require_project()

    session_id = req.session_id
    cli_session_id = None
    if session_id:
        session = await db.get_chat_session(session_id)
        if session:
            cli_session_id = session.get("cli_session_id")
    else:
        session = await db.create_chat_session(title=req.message[:50])
        session_id = session["id"]

    await db.add_chat_message(session_id, "user", req.message)

    message = req.message
    if req.context:
        message = f"Context: {req.context}\n\n{req.message}"

    system_prompt = await _build_chat_context()

    event_queue: asyncio.Queue = asyncio.Queue()
    _active_chats[session_id] = event_queue

    async def _run_cli():
        full_response = []
        streaming_text = []
        new_cli_session_id = None
        raw_event_buffer: list[tuple[str, str]] = []
        try:
            async for event in cli.invoke(
                prompt=message,
                system_prompt=system_prompt,
                cwd=state.PROJECT_DIR,
                model="opus",
                resume_session=cli_session_id,
            ):
                etype = event["type"]

                if etype == "_raw":
                    raw_event_buffer.append((event["event_type"], event["raw_json"]))
                    continue

                if etype == "assistant_complete":
                    full_response.append(event.get("content", ""))
                    continue

                if etype == "text":
                    streaming_text.append(event.get("content", ""))
                elif etype == "session_id":
                    new_cli_session_id = event.get("cli_session_id")

                await event_queue.put(event)
        except Exception as e:
            log.exception("[chat] CLI error for session %s: %s", session_id, e)
            await event_queue.put({"type": "error", "message": str(e)})
        finally:
            await db.add_chat_events_batch(session_id, raw_event_buffer)
            response_text = "".join(full_response) or "".join(streaming_text)
            if response_text:
                await db.add_chat_message(session_id, "assistant", response_text)
            if new_cli_session_id:
                await db.update_chat_session(session_id, cli_session_id=new_cli_session_id)
            await event_queue.put(None)
            _active_chats.pop(session_id, None)

    asyncio.create_task(_run_cli())

    async def stream():
        yield {"event": "session_id", "data": json.dumps({"session_id": session_id})}
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            if event is None:
                break
            etype = event["type"]
            if etype == "text":
                yield {"event": "text", "data": json.dumps({"content": event.get("content", "")})}
            elif etype == "thinking":
                yield {"event": "thinking", "data": json.dumps({"content": event.get("content", "")})}
            elif etype == "session_id":
                yield {"event": "session_id", "data": json.dumps({"cli_session_id": event.get("cli_session_id")})}
            elif etype == "error":
                yield {"event": "error", "data": json.dumps(event)}
            else:
                yield {"event": etype, "data": json.dumps(event)}

    return EventSourceResponse(stream())


@router.get("/sessions/{session_id}/status")
async def chat_session_status(session_id: str):
    """Check if a chat session is currently processing."""
    require_project()
    return {"processing": session_id in _active_chats}


@router.get("/sessions")
async def list_chat_sessions():
    require_project()
    return await db.get_chat_sessions()


@router.get("/sessions/{session_id}/messages")
async def get_chat_messages(session_id: str):
    require_project()
    return await db.get_chat_messages(session_id)


@router.delete("/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    require_project()
    await db.delete_chat_session(session_id)
    return {"status": "deleted"}
