"""Chat routes — executive assistant conversation interface.

Manages chat sessions, SSE streaming, and CLI invocation for
the conversational chat feature (distinct from task dispatches).
"""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import cli, database as db, git
from backend import state

log = logging.getLogger("maistro.chat")

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

You have full awareness of the project's administrative state (tasks, dispatches, configuration) \
and its content (files, git history). Help the user understand project status, plan work, \
troubleshoot issues, and manage their tasks.

You can read files, search code, and analyze the project. Always commit after making code changes. \
You are NOT running a task dispatch — you are having a conversation with the human operator.

## Working Directory
{project_dir}

## Current Tasks
{task_summary}

## Recent Dispatches
{dispatch_summary}

## Recent Git Activity
{git_summary}"""


async def _build_chat_context() -> str:
    """Build the mAistro executive assistant system prompt with live state."""
    tasks = await db.list_tasks()
    task_lines = []
    for t in tasks:
        props = t["properties"]
        status = "RUNNING" if props.get("running") else "idle"
        desc = props.get("description") or "(no description)"
        watch = "watch" if props.get("watch_enabled") else "manual"
        task_lines.append(f"- **{t['name']}** [{watch}, {status}] — {desc}")
    task_summary = "\n".join(task_lines) if task_lines else "(no tasks configured)"

    queue = await db.get_dispatch_queue()
    dispatch_lines = []
    for d in (queue or [])[:10]:
        status = "error" if d.get("error") else "completed" if d.get("completed_at") else "running" if d.get("started_at") else "pending"
        dispatch_lines.append(f"- #{d['id']} {d.get('task_name', '?')} [{status}] {d.get('created_at', '')}")
    dispatch_summary = "\n".join(dispatch_lines) if dispatch_lines else "(no recent dispatches)"

    git_summary = git.log_oneline(state.PROJECT_DIR) or "(no commits)"

    return CHAT_SYSTEM_PROMPT.format(
        project_dir=state.PROJECT_DIR,
        task_summary=task_summary,
        dispatch_summary=dispatch_summary,
        git_summary=git_summary,
    )


# Active chat streams — background tasks push events here, SSE reads from here.
# Key: session_id, Value: asyncio.Queue of SSE event dicts (None = done sentinel)
_active_chats: dict[str, asyncio.Queue] = {}


def _require_project():
    if not state.PROJECT_DIR:
        raise HTTPException(400, "No project loaded. POST /api/project/open first.")


# ── Routes ─────────────────────────────────────────────────

@router.post("/")
async def chat(req: ChatRequest):
    _require_project()

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

    # Event queue shared between background task and SSE stream
    event_queue: asyncio.Queue = asyncio.Queue()
    _active_chats[session_id] = event_queue

    # Background task: runs CLI and saves to DB regardless of client connection
    async def _run_cli():
        full_response = []
        streaming_text = []
        new_cli_session_id = None
        try:
            async for event in cli.invoke(
                prompt=message,
                system_prompt=system_prompt,
                cwd=state.PROJECT_DIR,
                model="opus",
                resume_session=cli_session_id,
            ):
                etype = event["type"]

                # Store raw events to DB, don't push to SSE
                if etype == "_raw":
                    await db.add_chat_event(
                        session_id, event["event_type"], event["raw_json"]
                    )
                    continue

                # assistant_complete: DB storage only, don't stream
                if etype == "assistant_complete":
                    full_response.append(event.get("content", ""))
                    continue

                if etype == "text":
                    streaming_text.append(event.get("content", ""))
                elif etype == "session_id":
                    new_cli_session_id = event.get("cli_session_id")

                # Push translated events to queue for SSE consumer
                await event_queue.put(event)
        except Exception as e:
            log.exception("[chat] CLI error for session %s: %s", session_id, e)
            await event_queue.put({"type": "error", "message": str(e)})
        finally:
            # Always save to DB — this runs even if client disconnected
            # Prefer assistant_complete (authoritative), fall back to streamed deltas
            response_text = "".join(full_response) or "".join(streaming_text)
            if response_text:
                await db.add_chat_message(session_id, "assistant", response_text)
            if new_cli_session_id:
                await db.update_chat_session(session_id, cli_session_id=new_cli_session_id)
            await event_queue.put(None)  # sentinel: stream done
            _active_chats.pop(session_id, None)

    asyncio.create_task(_run_cli())

    # SSE stream: reads from queue. If client disconnects, background task still runs.
    async def stream():
        yield {"event": "session_id", "data": json.dumps({"session_id": session_id})}
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=60)
            except asyncio.TimeoutError:
                # Send keepalive to prevent proxy/browser timeout
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
    _require_project()
    return {"processing": session_id in _active_chats}


@router.get("/sessions")
async def list_chat_sessions():
    _require_project()
    return await db.get_chat_sessions()


@router.get("/sessions/{session_id}/messages")
async def get_chat_messages(session_id: str):
    _require_project()
    return await db.get_chat_messages(session_id)


@router.delete("/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    _require_project()
    await db.delete_chat_session(session_id)
    return {"status": "deleted"}
