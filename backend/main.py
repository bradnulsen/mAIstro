"""mAistro backend — FastAPI app with all routes."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("maistro")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import appstate, cli, database as db, git, worker
from backend.dispatch import (
    check_watch_triggers,
    resolve_glob_files,
    utcnow,
)

# ── State ───────────────────────────────────────────────────

PROJECT_DIR: str | None = None


# ── Lifespan ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    appstate.init()
    await worker.start()
    yield
    await worker.stop()


app = FastAPI(title="mAistro", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Models ────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    name: str
    properties: dict | None = None

class UpdateTaskRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    base_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    subscriptions: list[str] | None = None
    watch_enabled: bool | None = None
    sort_order: int | None = None

class DispatchRequest(BaseModel):
    context: str | None = None

class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    context: str | None = None

class OpenProjectRequest(BaseModel):
    path: str

class PostCommitRequest(BaseModel):
    commit_hash: str

class ReorderRequest(BaseModel):
    task_ids: list[str]

class FileWriteRequest(BaseModel):
    content: str
    message: str | None = None

class ConfigRequest(BaseModel):
    value: str


# ── System Routes ───────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "project": PROJECT_DIR}


# ── Project Routes ──────────────────────────────────────────

@app.get("/api/project/")
async def get_project():
    if not PROJECT_DIR:
        return {"loaded": False}
    return {
        "loaded": True,
        "path": PROJECT_DIR,
        "name": os.path.basename(PROJECT_DIR),
    }


@app.post("/api/project/open")
async def open_project(req: OpenProjectRequest):
    global PROJECT_DIR
    path = os.path.abspath(req.path)
    if not os.path.isdir(path):
        raise HTTPException(404, "Directory not found")

    git.ensure_repo(path)
    PROJECT_DIR = path
    await db.init_db(path)
    git.install_post_commit_hook(path)
    git.ensure_gitignore(path)
    appstate.touch_project(path)
    worker.notify()

    return {"status": "ok", "path": path}


@app.post("/api/project/browse")
async def browse_project():
    """Open an OS-native directory picker dialog. Returns selected path or null."""
    import subprocess as sp
    import sys

    path = None
    if sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = 'Select a project directory'; "
            "$f.ShowNewFolderButton = $true; "
            "if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath } else { '' }"
        )
        result = sp.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=120,
        )
        path = result.stdout.strip() or None
    elif sys.platform == "darwin":
        result = sp.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select a project directory")'],
            capture_output=True, text=True, timeout=120,
        )
        path = result.stdout.strip().rstrip("/") or None
    else:
        for cmd in [
            ["zenity", "--file-selection", "--directory", "--title=Select a project directory"],
            ["kdialog", "--getexistingdirectory", os.path.expanduser("~")],
        ]:
            try:
                result = sp.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and result.stdout.strip():
                    path = result.stdout.strip()
                    break
            except FileNotFoundError:
                continue

    if not path:
        return {"path": None}
    return {"path": os.path.abspath(path)}


@app.get("/api/project/recent")
async def recent_projects():
    return appstate.list_recent()


@app.delete("/api/project/recent")
async def remove_recent_project(path: str):
    appstate.remove_project(path)
    return {"status": "ok"}


@app.post("/api/project/close")
async def close_project():
    global PROJECT_DIR
    PROJECT_DIR = None
    return {"status": "ok"}


# ── Task Routes ────────────────────────────────────────────

@app.get("/api/tasks/")
async def list_tasks():
    _require_project()
    return await db.list_tasks()


@app.post("/api/tasks/")
async def create_task(req: CreateTaskRequest):
    _require_project()
    return await db.create_task(req.name, req.properties or {})


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    _require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, req: UpdateTaskRequest):
    _require_project()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No updates provided")
    task = await db.update_task(task_id, updates)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    _require_project()
    ok = await db.delete_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found")
    return {"status": "deleted"}


@app.post("/api/tasks/reorder")
async def reorder_tasks(req: ReorderRequest):
    _require_project()
    for i, task_id in enumerate(req.task_ids):
        await db.update_task(task_id, {"sort_order": i})
    return {"status": "ok"}


@app.get("/api/tasks/{task_id}/subscriptions")
async def get_task_subscriptions(task_id: str):
    _require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    props = task["properties"]
    subs = resolve_glob_files(PROJECT_DIR, props.get("subscriptions") or [])
    return {"subscriptions": subs}


# ── Dispatch Routes ─────────────────────────────────────────

@app.post("/api/dispatch/{task_id}")
async def dispatch_task(task_id: str, req: DispatchRequest | None = None):
    """Enqueue a dispatch. The worker processes it."""
    _require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["properties"].get("running"):
        raise HTTPException(409, "Task is already running")

    context = req.context if req else None
    dispatch_id = await db.enqueue_dispatch(task_id, "manual", context=context)
    worker.notify()
    return {"dispatch_id": dispatch_id}


@app.get("/api/dispatch/queue")
async def get_dispatch_queue():
    _require_project()
    return await db.get_dispatch_queue()


@app.get("/api/dispatch/{dispatch_id}/output")
async def get_dispatch_output(dispatch_id: int):
    """Get stored output for a dispatch."""
    _require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    session_id = dispatch.get("session_id")
    if not session_id:
        return {"messages": [], "status": "pending", "dispatch": dispatch}
    messages = await db.get_chat_messages(session_id)
    status = "running" if dispatch.get("started_at") and not dispatch.get("completed_at") else \
             "completed" if dispatch.get("completed_at") else "pending"
    return {"messages": messages, "status": status, "dispatch": dispatch}


@app.post("/api/dispatch/cancel/{dispatch_id}")
async def cancel_dispatch(dispatch_id: int):
    _require_project()
    # Kill the running process if this dispatch is active
    was_running = worker.cancel(dispatch_id)
    # Mark as cancelled in DB (worker will also mark it, but this covers pending dispatches)
    await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="cancelled")
    return {"status": "cancelled", "was_running": was_running}


# ── Queue Control ──────────────────────────────────────────

@app.get("/api/queue/settings")
async def get_queue_settings():
    _require_project()
    auto = await db.get_config("queue_auto_dispatch")
    return {"auto_dispatch": auto == "true"}


@app.post("/api/queue/settings")
async def set_queue_settings(request: Request):
    _require_project()
    data = await request.json()
    value = "true" if data.get("auto_dispatch") else "false"
    await db.set_config("queue_auto_dispatch", value)
    worker.notify()
    return {"status": "ok"}


@app.post("/api/queue/process")
async def queue_process(all: bool = False):
    """Manually crank the queue — process next or all pending dispatches."""
    _require_project()
    if all:
        processed = await worker.process_all()
        return {"processed": processed}
    else:
        dispatch = await worker.process_next()
        return {"processed": [dispatch["id"]] if dispatch else []}


# ── Feed Routes ─────────────────────────────────────────────

@app.get("/api/feed/")
async def get_feed(limit: int = 50, offset: int = 0, task_id: str | None = None, path: str | None = None):
    _require_project()
    entries = git.log(PROJECT_DIR, limit=limit, skip=offset, path=path, name_only=True)

    queue = await db.get_dispatch_queue(limit=200)
    dispatch_by_commit = {d["result_commit"]: d for d in queue if d.get("result_commit")}

    feed = []
    for entry in entries:
        item = {**entry}
        dispatch = dispatch_by_commit.get(entry["hash"])
        if dispatch:
            item["dispatch"] = dispatch
            item["trigger"] = dispatch["trigger"]
        else:
            item["trigger"] = "task" if entry.get("email", "").endswith("@maistro.local") else "human"
        feed.append(item)

    if task_id:
        feed = [f for f in feed if f.get("dispatch", {}).get("task_id") == task_id
                or f.get("email") == f"{task_id}@maistro.local"]

    return feed


@app.get("/api/feed/{commit_hash}")
async def get_feed_item(commit_hash: str):
    _require_project()
    details = git.show(PROJECT_DIR, commit_hash, stat=True)
    if not details:
        raise HTTPException(404, "Commit not found")
    return {
        "details": details,
        "diff": git.diff(PROJECT_DIR, commit_hash),
    }


# ── Chat Routes ─────────────────────────────────────────────

CHAT_SYSTEM_PROMPT = """\
You are the mAistro executive assistant — an intelligent coordinator for a development engine \
where LLM-powered tasks coordinate through git.

You have full awareness of the project's administrative state (tasks, dispatches, configuration) \
and its content (files, git history). Help the user understand project status, plan work, \
troubleshoot issues, and manage their tasks.

You can read files, search code, and analyze the project. You are NOT running a task dispatch — \
you are having a conversation with the human operator.

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

    git_summary = git.log_oneline(PROJECT_DIR) or "(no commits)"

    return CHAT_SYSTEM_PROMPT.format(
        project_dir=PROJECT_DIR,
        task_summary=task_summary,
        dispatch_summary=dispatch_summary,
        git_summary=git_summary,
    )


# Active chat streams — background tasks push events here, SSE reads from here.
# Key: session_id, Value: asyncio.Queue of SSE event dicts (None = done sentinel)
_active_chats: dict[str, asyncio.Queue] = {}


@app.post("/api/chat/")
async def chat(req: ChatRequest):
    _require_project()

    session_id = req.session_id
    cli_session_id = None
    if session_id:
        sessions = await db.get_chat_sessions()
        for s in sessions:
            if s["id"] == session_id:
                cli_session_id = s.get("cli_session_id")
                break
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
                cwd=PROJECT_DIR,
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


@app.get("/api/chat/sessions/{session_id}/status")
async def chat_session_status(session_id: str):
    """Check if a chat session is currently processing."""
    _require_project()
    return {"processing": session_id in _active_chats}


@app.get("/api/chat/sessions")
async def list_chat_sessions():
    _require_project()
    return await db.get_chat_sessions()


@app.get("/api/chat/sessions/{session_id}/messages")
async def get_chat_messages(session_id: str):
    _require_project()
    return await db.get_chat_messages(session_id)


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    _require_project()
    await db.delete_chat_session(session_id)
    return {"status": "deleted"}


# ── Git Routes ──────────────────────────────────────────────

@app.get("/api/git/log")
async def git_log_route(limit: int = 50, path: str | None = None):
    _require_project()
    return git.log(PROJECT_DIR, limit=limit, path=path)


@app.get("/api/git/diff/{commit_hash}")
async def git_diff_route(commit_hash: str):
    _require_project()
    return {"diff": git.diff(PROJECT_DIR, commit_hash)}


@app.get("/api/git/status")
async def git_status_route():
    _require_project()
    return {"status": git.status(PROJECT_DIR)}


@app.get("/api/git/file/{path:path}")
async def read_git_file(path: str):
    _require_project()
    content = git.read_file(PROJECT_DIR, path)
    if content is None:
        raise HTTPException(404, "File not found")
    return {"path": path, "content": content}


@app.put("/api/git/file/{path:path}")
async def write_git_file(path: str, req: FileWriteRequest):
    _require_project()
    git.write_file(PROJECT_DIR, path, req.content)
    message = req.message or f"Update {path}"
    commit_hash = git.commit_file(PROJECT_DIR, path, message)
    return {"path": path, "commit": commit_hash}


# ── Hook Routes ─────────────────────────────────────────────

@app.post("/api/hooks/post-commit")
async def post_commit_hook(req: PostCommitRequest):
    if not PROJECT_DIR:
        return {"status": "no project"}

    triggered = await check_watch_triggers(req.commit_hash, PROJECT_DIR)
    dispatched = []

    for task in triggered:
        await db.enqueue_dispatch(
            task["id"], "commit", trigger_detail=req.commit_hash
        )
        dispatched.append(task["id"])

    if dispatched:
        worker.notify()

    return {"status": "ok", "triggered": dispatched}


# ── MCP Server Routes ──────────────────────────────────────

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    _require_project()
    return await db.list_mcp_servers()


@app.post("/api/mcp/servers")
async def create_mcp_server(request: Request):
    _require_project()
    data = await request.json()
    await db.create_mcp_server(
        name=data["name"],
        command=data["command"],
        args=data.get("args"),
        env=data.get("env"),
    )
    return {"status": "created"}


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    _require_project()
    await db.delete_mcp_server(name)
    return {"status": "deleted"}


# ── Config Routes ───────────────────────────────────────────

@app.get("/api/config/")
async def get_config():
    _require_project()
    return await db.get_config()


@app.post("/api/config/{key}")
async def set_config(key: str, req: ConfigRequest):
    _require_project()
    await db.set_config(key, req.value)
    return {"status": "ok"}


# ── Helpers ─────────────────────────────────────────────────

def _require_project():
    if not PROJECT_DIR:
        raise HTTPException(400, "No project loaded. POST /api/project/open first.")
