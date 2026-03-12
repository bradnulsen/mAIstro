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

from backend import appstate, database as db, git, scheduler, worker
from backend.chat import router as chat_router
from backend.dispatch import check_watch_triggers
from backend.state import utcnow, require_project
from backend import state


# ── Lifespan ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    appstate.init()
    await worker.start()
    await scheduler.start()
    yield
    await scheduler.stop()
    await worker.stop()


app = FastAPI(title="mAistro", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)


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
    coalesce_dispatches: bool | None = None
    schedule: str | None = None
    sort_order: int | None = None

class DispatchRequest(BaseModel):
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
    return {"status": "ok", "project": state.PROJECT_DIR}


# ── Project Routes ──────────────────────────────────────────

@app.get("/api/project/")
async def get_project():
    if not state.PROJECT_DIR:
        return {"loaded": False}
    return {
        "loaded": True,
        "path": state.PROJECT_DIR,
        "name": os.path.basename(state.PROJECT_DIR),
    }


@app.post("/api/project/open")
async def open_project(req: OpenProjectRequest):
    path = os.path.abspath(req.path)
    if not os.path.isdir(path):
        raise HTTPException(404, "Directory not found")

    git.ensure_repo(path)
    state.PROJECT_DIR = path
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
    state.PROJECT_DIR = None
    return {"status": "ok"}


# ── Task Routes ────────────────────────────────────────────

@app.get("/api/tasks/")
async def list_tasks():
    require_project()
    return await db.list_tasks()


@app.post("/api/tasks/")
async def create_task(req: CreateTaskRequest):
    require_project()
    return await db.create_task(req.name, req.properties or {})


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, req: UpdateTaskRequest):
    require_project()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No updates provided")
    task = await db.update_task(task_id, updates)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    require_project()
    ok = await db.delete_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found")
    return {"status": "deleted"}


@app.post("/api/tasks/reorder")
async def reorder_tasks(req: ReorderRequest):
    require_project()
    await db.reorder_tasks(req.task_ids)
    return {"status": "ok"}


@app.get("/api/tasks/{task_id}/subscriptions")
async def get_task_subscriptions(task_id: str):
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    props = task["properties"]
    subs = git.resolve_glob_files(state.PROJECT_DIR, props.get("subscriptions") or [])
    return {"subscriptions": subs}


# ── Dispatch Routes ─────────────────────────────────────────

@app.post("/api/dispatch/{task_id}")
async def dispatch_task(task_id: str, req: DispatchRequest | None = None):
    """Enqueue a dispatch. The worker processes it."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["properties"].get("running"):
        raise HTTPException(409, "Task is already running")

    context = req.context if req else None
    head = git.head_hash(state.PROJECT_DIR)
    dispatch_id = await db.enqueue_dispatch(task_id, "manual", trigger_detail=head, context=context)
    worker.notify()
    return {"dispatch_id": dispatch_id}


@app.get("/api/dispatch/queue")
async def get_dispatch_queue():
    require_project()
    return await db.get_dispatch_queue()


@app.get("/api/dispatch/{dispatch_id}/stream")
async def stream_dispatch(dispatch_id: int):
    """SSE stream of live events for a running dispatch."""
    from sse_starlette.sse import EventSourceResponse
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")

    # If already completed, return immediately with done
    if dispatch.get("completed_at"):
        async def done_stream():
            yield {"event": "done", "data": json.dumps({"status": "completed"})}
        return EventSourceResponse(done_stream())

    # Subscribe to live events from the worker
    q = worker.subscribe(dispatch_id)

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue

                etype = event.get("type", "")
                if etype == "_done":
                    yield {"event": "done", "data": json.dumps({"status": "completed"})}
                    break
                elif etype == "text":
                    yield {"event": "text", "data": json.dumps({"content": event.get("content", "")})}
                elif etype == "thinking":
                    yield {"event": "thinking", "data": json.dumps({"content": event.get("content", "")})}
                elif etype == "tool_use":
                    yield {"event": "tool_use", "data": json.dumps({"tool": event.get("tool", ""), "input": event.get("input", {})})}
                elif etype == "error":
                    yield {"event": "error", "data": json.dumps(event)}
                elif etype == "session_id":
                    yield {"event": "session_id", "data": json.dumps(event)}
        finally:
            worker.unsubscribe(dispatch_id, q)

    return EventSourceResponse(stream())


@app.get("/api/dispatch/{dispatch_id}/output")
async def get_dispatch_output(dispatch_id: int):
    """Get stored output for a dispatch."""
    require_project()
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


class UpdateDispatchRequest(BaseModel):
    context: str | None = None


@app.patch("/api/dispatch/{dispatch_id}")
async def update_dispatch_route(dispatch_id: int, req: UpdateDispatchRequest):
    """Edit a pending dispatch (only before it starts running)."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    if dispatch.get("started_at"):
        raise HTTPException(409, "Cannot edit a dispatch that has already started")

    updates = {}
    if req.context is not None:
        updates["context"] = req.context
        # Also update the triggers JSON array
        triggers = dispatch.get("triggers") or []
        if triggers:
            triggers[-1]["context"] = req.context
        else:
            triggers = [{"trigger": dispatch["trigger"], "detail": dispatch.get("trigger_detail"), "context": req.context}]
        updates["triggers"] = json.dumps(triggers)

    if updates:
        await db.update_dispatch(dispatch_id, **updates)
    return {"status": "ok"}


@app.post("/api/dispatch/cancel/{dispatch_id}")
async def cancel_dispatch(dispatch_id: int):
    require_project()
    # Kill the running process if this dispatch is active
    was_running = worker.cancel(dispatch_id)
    # Mark as cancelled in DB (worker will also mark it, but this covers pending dispatches)
    await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="cancelled")
    return {"status": "cancelled", "was_running": was_running}


# ── Queue Control ──────────────────────────────────────────

@app.get("/api/queue/settings")
async def get_queue_settings():
    require_project()
    auto = await db.get_config("queue_auto_dispatch")
    return {"auto_dispatch": auto == "true"}


@app.post("/api/queue/settings")
async def set_queue_settings(request: Request):
    require_project()
    data = await request.json()
    value = "true" if data.get("auto_dispatch") else "false"
    await db.set_config("queue_auto_dispatch", value)
    worker.notify()
    return {"status": "ok"}


@app.post("/api/queue/process")
async def queue_process(all: bool = False):    # noqa: A002 — matches frontend query param
    """Manually crank the queue — process next or all pending dispatches."""
    require_project()
    if all:
        processed = await worker.process_all()
        return {"processed": processed}
    else:
        dispatch = await worker.process_next()
        return {"processed": [dispatch["id"]] if dispatch else []}


# ── Feed Routes ─────────────────────────────────────────────

@app.get("/api/feed/")
async def get_feed(limit: int = 50, offset: int = 0, task_id: str | None = None, path: str | None = None):
    require_project()
    entries = git.log(state.PROJECT_DIR, limit=limit, skip=offset, path=path, with_stats=True)

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
    require_project()
    details = git.show(state.PROJECT_DIR, commit_hash, stat=True)
    if not details:
        raise HTTPException(404, "Commit not found")
    return {
        "details": details,
        "diff": git.diff(state.PROJECT_DIR, commit_hash),
    }


# ── Git Routes ──────────────────────────────────────────────

@app.get("/api/git/log")
async def git_log_route(limit: int = 50, path: str | None = None):
    require_project()
    return git.log(state.PROJECT_DIR, limit=limit, path=path)


@app.get("/api/git/diff/{commit_hash}")
async def git_diff_route(commit_hash: str):
    require_project()
    return {"diff": git.diff(state.PROJECT_DIR, commit_hash)}


@app.get("/api/git/status")
async def git_status_route():
    require_project()
    return {"status": git.status(state.PROJECT_DIR)}


@app.get("/api/git/file/{path:path}")
async def read_git_file(path: str):
    require_project()
    content = git.read_file(state.PROJECT_DIR, path)
    if content is None:
        raise HTTPException(404, "File not found")
    return {"path": path, "content": content}


@app.put("/api/git/file/{path:path}")
async def write_git_file(path: str, req: FileWriteRequest):
    require_project()
    git.write_file(state.PROJECT_DIR, path, req.content)
    message = req.message or f"Update {path}"
    commit_hash = git.commit_file(state.PROJECT_DIR, path, message)
    return {"path": path, "commit": commit_hash}


# ── Hook Routes ─────────────────────────────────────────────

@app.post("/api/hooks/post-commit")
async def post_commit_hook(req: PostCommitRequest):
    if not state.PROJECT_DIR:
        return {"status": "no project"}

    triggered = await check_watch_triggers(req.commit_hash, state.PROJECT_DIR)
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
    require_project()
    return await db.list_mcp_servers()


@app.post("/api/mcp/servers")
async def create_mcp_server(request: Request):
    require_project()
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
    require_project()
    await db.delete_mcp_server(name)
    return {"status": "deleted"}


# ── Config Routes ───────────────────────────────────────────

@app.get("/api/config/")
async def get_config():
    require_project()
    return await db.get_config()


@app.post("/api/config/{key}")
async def set_config(key: str, req: ConfigRequest):
    require_project()
    await db.set_config(key, req.value)
    return {"status": "ok"}
