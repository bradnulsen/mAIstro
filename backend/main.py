"""mAistro backend — FastAPI app with all routes."""

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import database as db
from backend.dispatch import (
    run_dispatch,
    check_watch_triggers,
    invoke_chat_message,
    build_system_prompt,
    build_user_prompt,
)

# ── State ───────────────────────────────────────────────────

PROJECT_DIR: str | None = None


# ── Lifespan ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    mcp_servers: list[str] | None = None
    input_artifacts: list[str] | None = None
    output_artifacts: list[str] | None = None
    mode: str | None = None
    cooldown_seconds: int | None = None
    sort_order: int | None = None

class DispatchRequest(BaseModel):
    instructions: str | None = None

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

    # Check if git repo
    git_dir = os.path.join(path, ".git")
    if not os.path.isdir(git_dir):
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=path, capture_output=True)

    PROJECT_DIR = path
    await db.init_db(path)

    # Install post-commit hook
    _install_post_commit_hook(path)

    # Add .maistro to gitignore
    _ensure_gitignore(path)

    return {"status": "ok", "path": path}


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
    props = req.properties or {}
    agent = await db.create_agent(req.name, props)
    return agent


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
    return {
        "outputs": _resolve_artifact_globs(PROJECT_DIR, props.get("output_artifacts", [])),
        "inputs": _resolve_artifact_globs(PROJECT_DIR, props.get("input_artifacts", [])),
    }


@app.get("/api/agents/{agent_id}/artifacts/manifest")
async def get_artifact_manifest(agent_id: str):
    _require_project()
    agents = await db.list_agents()
    manifest = []
    for a in agents:
        manifest.append({
            "id": a["id"],
            "name": a["name"],
            "input_artifacts": a["properties"].get("input_artifacts", []),
            "output_artifacts": a["properties"].get("output_artifacts", []),
        })
    return manifest


# ── Dispatch Routes ─────────────────────────────────────────

@app.post("/api/dispatch/{agent_id}")
async def dispatch_agent(agent_id: str, req: DispatchRequest | None = None):
    _require_project()
    agent = await db.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent["properties"].get("running"):
        raise HTTPException(409, "Agent is already running")

    instructions = req.instructions if req else None
    dispatch_id = await db.enqueue_dispatch(agent_id, "manual", instructions=instructions)

    async def stream():
        async for event in run_dispatch(dispatch_id, agent, PROJECT_DIR, instructions):
            yield {"event": event["type"], "data": json.dumps(event)}

    return EventSourceResponse(stream())


@app.get("/api/dispatch/queue")
async def get_dispatch_queue():
    _require_project()
    return await db.get_dispatch_queue()


@app.post("/api/dispatch/cancel/{dispatch_id}")
async def cancel_dispatch(dispatch_id: int):
    _require_project()
    await db.update_dispatch(dispatch_id, completed_at=_now(), error="cancelled")
    return {"status": "cancelled"}


# ── Feed Routes ─────────────────────────────────────────────

@app.get("/api/feed/")
async def get_feed(limit: int = 50, offset: int = 0, agent_id: str | None = None, path: str | None = None):
    _require_project()
    # Build git log entries
    cmd = ["git", "log", f"--max-count={limit}", f"--skip={offset}",
           "--format=%H|%an|%ae|%s|%ai", "--name-only"]
    if path:
        cmd.extend(["--", path])

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_DIR)
    if result.returncode != 0:
        return []

    entries = _parse_git_log_with_files(result.stdout)

    # Enhance with dispatch metadata
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
            # Check if author email ends with @maistro.local
            item["trigger"] = "agent" if entry.get("email", "").endswith("@maistro.local") else "human"
        feed.append(item)

    if agent_id:
        feed = [f for f in feed if f.get("dispatch", {}).get("agent_id") == agent_id
                or agent_id in f.get("email", "")]

    return feed


@app.get("/api/feed/{commit_hash}")
async def get_feed_item(commit_hash: str):
    _require_project()
    # Get commit details
    result = subprocess.run(
        ["git", "show", commit_hash, "--format=%H|%an|%ae|%s|%ai", "--stat"],
        capture_output=True, text=True, cwd=PROJECT_DIR
    )
    if result.returncode != 0:
        raise HTTPException(404, "Commit not found")

    # Get diff
    diff_result = subprocess.run(
        ["git", "diff", f"{commit_hash}~1", commit_hash],
        capture_output=True, text=True, cwd=PROJECT_DIR
    )

    return {
        "details": result.stdout,
        "diff": diff_result.stdout if diff_result.returncode == 0 else "",
    }


# ── Chat Routes ─────────────────────────────────────────────

@app.post("/api/chat/")
async def chat(req: ChatRequest):
    _require_project()
    agent = await db.get_agent(req.agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    # Create or fetch session
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

    # Save user message
    await db.add_chat_message(session_id, "user", req.message)

    message = req.message
    if req.context:
        message = f"Context: {req.context}\n\n{req.message}"

    async def stream():
        yield {"event": "session_id", "data": json.dumps({"session_id": session_id})}

        full_response = []
        new_cli_session_id = None

        async for event in invoke_chat_message(agent, message, PROJECT_DIR, session_id, cli_session_id):
            if event.get("type") == "session_id":
                new_cli_session_id = event.get("cli_session_id")
                yield {"event": "session_id", "data": json.dumps({"cli_session_id": new_cli_session_id})}
            elif event.get("type") == "text":
                full_response.append(event.get("content", ""))
                yield {"event": "text", "data": json.dumps({"content": event.get("content", "")})}
            elif event.get("type") == "error":
                yield {"event": "error", "data": json.dumps(event)}
            else:
                yield {"event": event["type"], "data": json.dumps(event)}

        # Save assistant response
        response_text = "".join(full_response)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)

        # Update CLI session ID for resumption
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
    conn = await db.get_db()
    try:
        await conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "deleted"}


# ── Git Routes ──────────────────────────────────────────────

@app.get("/api/git/log")
async def git_log(limit: int = 50, path: str | None = None):
    _require_project()
    cmd = ["git", "log", f"--max-count={limit}", "--format=%H|%an|%ae|%s|%ai"]
    if path:
        cmd.extend(["--", path])
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_DIR)
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 4)
        if len(parts) >= 5:
            entries.append({
                "hash": parts[0], "author": parts[1], "email": parts[2],
                "message": parts[3], "date": parts[4],
            })
    return entries


@app.get("/api/git/diff/{commit_hash}")
async def git_diff(commit_hash: str):
    _require_project()
    result = subprocess.run(
        ["git", "diff", f"{commit_hash}~1", commit_hash],
        capture_output=True, text=True, cwd=PROJECT_DIR
    )
    return {"diff": result.stdout if result.returncode == 0 else ""}


@app.get("/api/git/status")
async def git_status():
    _require_project()
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=PROJECT_DIR
    )
    return {"status": result.stdout if result.returncode == 0 else ""}


@app.get("/api/git/file/{path:path}")
async def read_git_file(path: str):
    _require_project()
    filepath = os.path.join(PROJECT_DIR, path)
    if not os.path.isfile(filepath):
        raise HTTPException(404, "File not found")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return {"path": path, "content": f.read()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.put("/api/git/file/{path:path}")
async def write_git_file(path: str, req: FileWriteRequest):
    _require_project()
    filepath = os.path.join(PROJECT_DIR, path)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(req.content)

    message = req.message or f"Update {path}"
    subprocess.run(["git", "add", path], cwd=PROJECT_DIR, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=PROJECT_DIR, capture_output=True)

    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=PROJECT_DIR)
    commit_hash = result.stdout.strip() if result.returncode == 0 else None

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
        # Run dispatch in background
        asyncio.create_task(_run_background_dispatch(dispatch_id, agent))
        dispatched.append(agent["id"])

    return {"status": "ok", "triggered": dispatched}


async def _run_background_dispatch(dispatch_id: int, agent: dict):
    """Run a dispatch in the background (for watch-triggered agents)."""
    try:
        async for event in run_dispatch(dispatch_id, agent, PROJECT_DIR):
            pass  # Events are consumed but not streamed (background)
    except Exception:
        pass


# ── MCP Server Routes ──────────────────────────────────────

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    _require_project()
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM mcp_servers")
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.post("/api/mcp/servers")
async def create_mcp_server(request: Request):
    _require_project()
    data = await request.json()
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO mcp_servers (name, command, args, env) VALUES (?, ?, ?, ?)",
            (data["name"], data["command"], json.dumps(data.get("args", [])), json.dumps(data.get("env", {})))
        )
        await conn.commit()
    finally:
        await conn.close()
    return {"status": "created"}


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    _require_project()
    conn = await db.get_db()
    try:
        await conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        await conn.commit()
    finally:
        await conn.close()
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


def _install_post_commit_hook(project_dir: str):
    hooks_dir = os.path.join(project_dir, ".git", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "post-commit")

    # Detect port from config or default
    port = 8420

    script = f"""#!/bin/bash
curl -s -X POST http://localhost:{port}/api/hooks/post-commit \\
  -H "Content-Type: application/json" \\
  -d '{{"commit_hash": "'$(git rev-parse HEAD)'"}}' > /dev/null 2>&1 &
"""
    with open(hook_path, "w", newline="\n") as f:
        f.write(script)
    # Make executable on unix
    try:
        os.chmod(hook_path, 0o755)
    except OSError:
        pass


def _ensure_gitignore(project_dir: str):
    gitignore = os.path.join(project_dir, ".gitignore")
    entry = ".maistro/"
    if os.path.exists(gitignore):
        with open(gitignore, "r") as f:
            if entry in f.read():
                return
    with open(gitignore, "a") as f:
        f.write(f"\n{entry}\n")


def _parse_git_log_with_files(output: str) -> list[dict]:
    entries = []
    current = None
    for line in output.strip().split("\n"):
        if not line:
            if current:
                entries.append(current)
                current = None
            continue
        if "|" in line and line.count("|") >= 4:
            if current:
                entries.append(current)
            parts = line.split("|", 4)
            current = {
                "hash": parts[0], "author": parts[1], "email": parts[2],
                "message": parts[3], "date": parts[4], "files": [],
            }
        elif current:
            current["files"].append(line.strip())
    if current:
        entries.append(current)
    return entries


def _resolve_artifact_globs(project_dir: str, patterns: list[str]) -> list[dict]:
    import glob as globmod
    files = []
    seen = set()
    for pattern in patterns:
        full_pattern = os.path.join(project_dir, pattern)
        for filepath in globmod.glob(full_pattern, recursive=True):
            if filepath in seen or not os.path.isfile(filepath):
                continue
            seen.add(filepath)
            rel = os.path.relpath(filepath, project_dir).replace("\\", "/")
            stat = os.stat(filepath)
            files.append({
                "path": rel,
                "pattern": pattern,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
