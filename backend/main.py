"""mAistro backend — FastAPI app, lifespan, and remaining routes.

Route modules: job_routes.py, queue_routes.py, chat.py.
"""

import asyncio
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("maistro")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import appstate, database as db, git, scheduler, worker
from backend.chat import router as chat_router, invalidate_chat_context_cache
from backend.cli import CLI_NATIVE_TOOLS
from backend.mcp_probe import probe_server
from backend.queue_routes import router as queue_router
from backend.job_routes import router as job_router
from backend.dispatch import check_watch_triggers, build_trigger_context
from backend.state import require_project
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
    await db.close_db()


app = FastAPI(title="mAistro", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(queue_router)
app.include_router(job_router)


# ── Pydantic Models ────────────────────────────────────────

class OpenProjectRequest(BaseModel):
    path: str

class PostCommitRequest(BaseModel):
    commit_hash: str

class FileWriteRequest(BaseModel):
    content: str
    message: str | None = None

class CreateMcpServerRequest(BaseModel):
    name: str
    command: str
    args: list | None = None
    env: dict | None = None

class UpdateMcpServerRequest(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    args: list | None = None
    env: dict | None = None

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

    switching = state.PROJECT_DIR and os.path.normpath(path) != os.path.normpath(state.PROJECT_DIR)
    if switching:
        active = worker.get_active_task_id()
        if active is not None:
            raise HTTPException(409, f"Cannot switch projects while task #{active} is running. Cancel it first or wait for completion.")

    # Coordinated project switch: flag prevents new work, close_db drains readers
    if switching:
        state._switching = True
    try:
        git.ensure_repo(path)
        invalidate_chat_context_cache()
        await db.init_db(path)
        state.PROJECT_DIR = path
    finally:
        state._switching = False
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
            "[System.Windows.Forms.Application]::EnableVisualStyles(); "
            "$f = New-Object System.Windows.Forms.OpenFileDialog; "
            "$f.ValidateNames = $false; "
            "$f.CheckFileExists = $false; "
            "$f.CheckPathExists = $true; "
            "$f.FileName = 'Select Folder'; "
            "$f.Title = 'Select a project directory'; "
            "if ($f.ShowDialog() -eq 'OK') { [System.IO.Path]::GetDirectoryName($f.FileName) } else { '' }"
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
    active = worker.get_active_task_id()
    if active is not None:
        raise HTTPException(409, f"Cannot close project while task #{active} is running. Cancel it first or wait for completion.")
    state._switching = True
    try:
        state.PROJECT_DIR = None
        invalidate_chat_context_cache()
        await db.close_db()
    finally:
        state._switching = False
    return {"status": "ok"}


# ── Feed Routes ─────────────────────────────────────────────

@app.get("/api/feed/")
async def get_feed(limit: int = 50, offset: int = 0, job_id: int | None = None, path: str | None = None):
    require_project()
    entries = git.log(state.PROJECT_DIR, limit=limit, skip=offset, path=path, with_stats=True)

    queue = await db.get_task_queue(limit=200)
    task_by_commit = {t["result_commit"]: t for t in queue if t.get("result_commit")}

    feed = []
    for entry in entries:
        item = {**entry}
        task = task_by_commit.get(entry["hash"])
        if task:
            item["task"] = task
            item["trigger"] = task["trigger"]
        else:
            item["trigger"] = "task" if entry.get("email", "").endswith("@maistro.local") else "human"
        feed.append(item)

    if job_id:
        # Look up slug for email matching (git author uses slug@maistro.local)
        job = await db.get_job(job_id)
        slug = job["slug"] if job else None
        feed = [f for f in feed if f.get("task", {}).get("job_id") == job_id
                or (slug and f.get("email") == f"{slug}@maistro.local")]

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

    for job in triggered:
        context = build_trigger_context(
            "commit", project_dir=state.PROJECT_DIR, commit_hash=req.commit_hash,
        )
        await db.enqueue_task(
            job["id"], "commit", trigger_detail=req.commit_hash, context=context
        )
        dispatched.append(job["id"])

    if dispatched:
        worker.notify()

    return {"status": "ok", "triggered": dispatched}


# ── Tool Inventory Route ───────────────────────────────────

INTERNAL_MCP_TOOLS = [
    "git_status", "git_log", "git_diff", "git_commit",
    "git_branch_create", "git_branch_switch", "git_branch_merge",
    "list_files", "read_file", "list_jobs",
    "dispatch_task", "get_queue_status",
]

@app.get("/api/tools/inventory")
async def get_tool_inventory(probe: bool = False):
    """Return the full tool inventory: CLI native, internal MCP, and external server tools.

    External server probing is opt-in via ?probe=true to avoid blocking the
    response on subprocess handshakes. Without probing, external servers are
    listed with status "unknown" — the frontend can probe individual servers
    on demand via GET /api/mcp/servers/{name}/tools.
    """
    result = {
        "cli_native": sorted(CLI_NATIVE_TOOLS),
        "internal_mcp": INTERNAL_MCP_TOOLS,
        "external_servers": {},
    }

    if state.PROJECT_DIR:
        servers = await db.list_mcp_servers()
        enabled = [s for s in servers if s.get("enabled", True)]
        disabled = [s for s in servers if not s.get("enabled", True)]

        for s in disabled:
            result["external_servers"][s["name"]] = {"status": "disabled", "tools": []}

        if probe and enabled:
            import asyncio
            probes = await asyncio.gather(*(
                probe_server(
                    s["command"],
                    json.loads(s.get("args") or "[]"),
                    json.loads(s.get("env") or "{}"),
                ) for s in enabled
            ))
            for s, p in zip(enabled, probes):
                result["external_servers"][s["name"]] = p
        else:
            for s in enabled:
                result["external_servers"][s["name"]] = {"status": "unknown", "tools": []}

    return result


def _validate_mcp_server_config(command: str, args: list | None = None, env: dict | None = None):
    """Validate MCP server configuration eagerly at registration/update time."""
    if not command or not command.strip():
        raise HTTPException(422, "Command is required")
    cmd = command.strip()
    if not os.path.isabs(cmd) and not shutil.which(cmd):
        raise HTTPException(422, f"Command not found: '{cmd}' is not on PATH and is not an absolute path")
    if args is not None and not isinstance(args, list):
        raise HTTPException(422, "Arguments must be a list")
    if env is not None:
        if not isinstance(env, dict):
            raise HTTPException(422, "Environment variables must be a key-value object")
        for k, v in env.items():
            if not isinstance(k, str) or not k.strip():
                raise HTTPException(422, "Environment variable keys must be non-empty strings")
            if not isinstance(v, str):
                raise HTTPException(422, f"Environment variable value for '{k}' must be a string")


# ── MCP Server Routes ──────────────────────────────────────

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    require_project()
    return await db.list_mcp_servers()


@app.post("/api/mcp/servers")
async def create_mcp_server(req: CreateMcpServerRequest):
    require_project()
    _validate_mcp_server_config(req.command, req.args, req.env)
    await db.create_mcp_server(
        name=req.name,
        command=req.command,
        args=req.args,
        env=req.env,
    )
    return {"status": "created"}


@app.patch("/api/mcp/servers/{name}")
async def update_mcp_server(name: str, req: UpdateMcpServerRequest):
    require_project()
    if req.enabled is not None:
        await db.update_mcp_server_enabled(name, req.enabled)
    if req.command is not None or req.args is not None or req.env is not None:
        if req.command is not None:
            _validate_mcp_server_config(req.command, req.args, req.env)
        await db.update_mcp_server_fields(name, req.command, req.args, req.env)
    return {"status": "updated"}


@app.get("/api/mcp/servers/{name}/tools")
async def probe_mcp_server(name: str):
    """Probe an external MCP server and return its discovered tools and health."""
    require_project()
    servers = await db.list_mcp_servers()
    server = next((s for s in servers if s["name"] == name), None)
    if not server:
        raise HTTPException(404, "Server not found")
    if not server.get("enabled", True):
        return {"status": "disabled", "tools": []}
    return await probe_server(
        server["command"],
        json.loads(server.get("args") or "[]"),
        json.loads(server.get("env") or "{}"),
    )


@app.get("/api/mcp/servers/{name}/jobs")
async def get_mcp_server_jobs(name: str):
    """Return jobs that reference this MCP server in their mcp_servers property."""
    require_project()
    jobs = await db.get_jobs_referencing_mcp_server(name)
    return [{"id": j["id"], "name": j["name"]} for j in jobs]


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    require_project()
    await db.delete_mcp_server_cascade(name)
    return {"status": "deleted"}


# ── Files Routes ─────────────────────────────────────────────

@app.get("/api/files/")
async def list_files(pattern: str = "**/*"):
    """List project files matching a glob pattern."""
    require_project()
    files = git.resolve_glob_files(state.PROJECT_DIR, [pattern])
    return files


# ── Dashboard Routes ────────────────────────────────────────

@app.get("/api/dashboard")
async def get_dashboard(window: int = 7):
    """Aggregated operational dashboard — health, timeline, chains, tool usage.

    Window parameter is in days: 1 (today), 7, 30.
    """
    require_project()
    window = max(1, min(window, 90))
    health, timeline, chains, tools = await asyncio.gather(
        db.dashboard_health(window),
        db.dashboard_timeline(window),
        db.dashboard_chains(window),
        db.dashboard_tool_usage(window),
    )
    return {
        "window_days": window,
        "health": health,
        "timeline": timeline,
        "chains": chains,
        "tools": tools,
    }


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
