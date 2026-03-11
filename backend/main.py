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

from backend import appstate, cli, database as db, git
from backend.dispatch import (
    run_dispatch,
    check_watch_triggers,
    build_system_prompt,
    resolve_glob_files,
    utcnow,
)

# ── State ───────────────────────────────────────────────────

PROJECT_DIR: str | None = None


# ── Lifespan ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    appstate.init()
    yield


app = FastAPI(title="mAistro", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Models ────────────────────────────────────────

class CreateAgentRequest(BaseModel):
    name: str
    properties: dict | None = None

class UpdateAgentRequest(BaseModel):
    name: str | None = None
    persona: str | None = None
    model: str | None = None
    base_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    subscriptions: list[str] | None = None
    mode: str | None = None
    cooldown_seconds: int | None = None
    sort_order: int | None = None

class DispatchRequest(BaseModel):
    context: str | None = None

class ChatRequest(BaseModel):
    agent_id: str
    session_id: str | None = None
    message: str
    context: str | None = None

class OpenProjectRequest(BaseModel):
    path: str

class PostCommitRequest(BaseModel):
    commit_hash: str

class ReorderRequest(BaseModel):
    agent_ids: list[str]

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

    return {"status": "ok", "path": path}


@app.post("/api/project/browse")
async def browse_project():
    """Open an OS-native directory picker dialog. Returns selected path or null."""
    import subprocess as sp
    import sys

    path = None
    if sys.platform == "win32":
        # PowerShell folder browser dialog
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
        # Linux — try zenity, kdialog, or xdg
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
    """List recently opened projects."""
    return appstate.list_recent()


@app.delete("/api/project/recent")
async def remove_recent_project(path: str):
    """Remove a project from recent list."""
    appstate.remove_project(path)
    return {"status": "ok"}


@app.post("/api/project/close")
async def close_project():
    global PROJECT_DIR
    PROJECT_DIR = None
    return {"status": "ok"}


# ── Agent Routes ────────────────────────────────────────────

@app.get("/api/agents/")
async def list_agents():
    _require_project()
    return await db.list_agents()


@app.post("/api/agents/")
async def create_agent(req: CreateAgentRequest):
    _require_project()
    return await db.create_agent(req.name, req.properties or {})


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    _require_project()
    agent = await db.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@app.patch("/api/agents/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgentRequest):
    _require_project()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No updates provided")
    agent = await db.update_agent(agent_id, updates)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    _require_project()
    ok = await db.delete_agent(agent_id)
    if not ok:
        raise HTTPException(404, "Agent not found")
    return {"status": "deleted"}


@app.post("/api/agents/reorder")
async def reorder_agents(req: ReorderRequest):
    _require_project()
    for i, agent_id in enumerate(req.agent_ids):
        await db.update_agent(agent_id, {"sort_order": i})
    return {"status": "ok"}


@app.get("/api/agents/{agent_id}/artifacts")
async def get_agent_artifacts(agent_id: str):
    _require_project()
    agent = await db.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    props = agent["properties"]
    # Authored files: derived from git log --author
    email = f"{agent_id}@maistro.local"
    authored = git.authored_files(PROJECT_DIR, email)
    # Subscriptions: resolved from glob patterns
    subs = resolve_glob_files(PROJECT_DIR, props.get("subscriptions") or [])
    return {
        "authored": authored,
        "subscriptions": subs,
    }


@app.get("/api/agents/{agent_id}/artifacts/manifest")
async def get_artifact_manifest(agent_id: str):
    _require_project()
    agents = await db.list_agents()
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "subscriptions": a["properties"].get("subscriptions") or [],
        }
        for a in agents
    ]


# ── Dispatch Routes ─────────────────────────────────────────

@app.post("/api/dispatch/{agent_id}")
async def dispatch_agent(agent_id: str, req: DispatchRequest | None = None):
    _require_project()
    agent = await db.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent["properties"].get("running"):
        raise HTTPException(409, "Agent is already running")

    context = req.context if req else None
    dispatch_id = await db.enqueue_dispatch(agent_id, "manual", context=context)

    async def stream():
        async for event in run_dispatch(dispatch_id, agent, PROJECT_DIR, context):
            yield {"event": event["type"], "data": json.dumps(event)}

    return EventSourceResponse(stream())


@app.get("/api/dispatch/queue")
async def get_dispatch_queue():
    _require_project()
    return await db.get_dispatch_queue()


@app.post("/api/dispatch/cancel/{dispatch_id}")
async def cancel_dispatch(dispatch_id: int):
    _require_project()
    await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="cancelled")
    return {"status": "cancelled"}


# ── Feed Routes ─────────────────────────────────────────────

@app.get("/api/feed/")
async def get_feed(limit: int = 50, offset: int = 0, agent_id: str | None = None, path: str | None = None):
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
            item["trigger"] = "agent" if entry.get("email", "").endswith("@maistro.local") else "human"
        feed.append(item)

    if agent_id:
        feed = [f for f in feed if f.get("dispatch", {}).get("agent_id") == agent_id
                or agent_id in f.get("email", "")]

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

@app.post("/api/chat/")
async def chat(req: ChatRequest):
    _require_project()
    agent = await db.get_agent(req.agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    session_id = req.session_id
    cli_session_id = None
    if session_id:
        sessions = await db.get_chat_sessions(req.agent_id)
        for s in sessions:
            if s["id"] == session_id:
                cli_session_id = s.get("cli_session_id")
                break
    else:
        session = await db.create_chat_session(req.agent_id, title=req.message[:50])
        session_id = session["id"]

    await db.add_chat_message(session_id, "user", req.message)

    message = req.message
    if req.context:
        message = f"Context: {req.context}\n\n{req.message}"

    props = agent["properties"]
    system_prompt = build_system_prompt(agent, PROJECT_DIR)

    async def stream():
        yield {"event": "session_id", "data": json.dumps({"session_id": session_id})}

        full_response = []
        new_cli_session_id = None

        async for event in cli.invoke(
            prompt=message,
            system_prompt=system_prompt,
            cwd=PROJECT_DIR,
            model=props.get("model"),
            allowed_tools=props.get("base_tools") or None,
            disallowed_tools=props.get("disallowed_tools") or None,
            resume_session=cli_session_id,
        ):
            if event["type"] == "session_id":
                new_cli_session_id = event.get("cli_session_id")
                yield {"event": "session_id", "data": json.dumps({"cli_session_id": new_cli_session_id})}
            elif event["type"] == "text":
                full_response.append(event.get("content", ""))
                yield {"event": "text", "data": json.dumps({"content": event.get("content", "")})}
            elif event["type"] == "error":
                yield {"event": "error", "data": json.dumps(event)}
            else:
                yield {"event": event["type"], "data": json.dumps(event)}

        response_text = "".join(full_response)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)
        if new_cli_session_id:
            await db.update_chat_session(session_id, cli_session_id=new_cli_session_id)

    return EventSourceResponse(stream())


@app.get("/api/chat/sessions")
async def list_chat_sessions(agent_id: str | None = None):
    _require_project()
    return await db.get_chat_sessions(agent_id)


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

    for agent in triggered:
        dispatch_id = await db.enqueue_dispatch(
            agent["id"], "commit", trigger_detail=req.commit_hash
        )
        asyncio.create_task(_run_background_dispatch(dispatch_id, agent))
        dispatched.append(agent["id"])

    return {"status": "ok", "triggered": dispatched}


async def _run_background_dispatch(dispatch_id: int, agent: dict):
    try:
        async for _ in run_dispatch(dispatch_id, agent, PROJECT_DIR):
            pass
    except Exception:
        pass


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
