"""Git, file, and post-commit hook routes.

Wraps the synchronous `git.py` subprocess layer into HTTP endpoints, exposes
project file content for the file browser, and receives the post-commit hook
callback that drives watch-trigger evaluation.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db, git, worker
from backend import state
from backend.dispatch import check_watch_triggers, build_trigger_context
from backend.state import require_project

log = logging.getLogger("maistro.git_routes")

router = APIRouter(tags=["git"])


class FileWriteRequest(BaseModel):
    content: str
    message: str | None = None


class PostCommitRequest(BaseModel):
    commit_hash: str


# ── Git Routes ──────────────────────────────────────────────

@router.get("/api/git/log")
async def git_log_route(limit: int = 50, path: str | None = None):
    project_dir = require_project()
    return git.log(project_dir, limit=limit, path=path)


@router.get("/api/git/diff/{commit_hash}")
async def git_diff_route(commit_hash: str):
    project_dir = require_project()
    return {"diff": git.diff(project_dir, commit_hash)}


@router.get("/api/git/status")
async def git_status_route():
    project_dir = require_project()
    return {"status": git.status(project_dir)}


@router.get("/api/git/file/{path:path}")
async def read_git_file(path: str):
    project_dir = require_project()
    content = git.read_file(project_dir, path)
    if content is None:
        raise HTTPException(404, "File not found")
    return {"path": path, "content": content}


@router.put("/api/git/file/{path:path}")
async def write_git_file(path: str, req: FileWriteRequest):
    project_dir = require_project()
    git.write_file(project_dir, path, req.content)
    message = req.message or f"Update {path}"
    commit_hash = git.commit_file(project_dir, path, message)
    return {"path": path, "commit": commit_hash}


# ── Files Route ─────────────────────────────────────────────

@router.get("/api/files/")
async def list_files(pattern: str = "**/*"):
    """List project files matching a glob pattern."""
    project_dir = require_project()
    files = git.resolve_glob_files(project_dir, [pattern])
    return files


# ── Hook Route ─────────────────────────────────────────────

@router.post("/api/hooks/post-commit")
async def post_commit_hook(req: PostCommitRequest):
    project_dir = state.PROJECT_DIR
    if not project_dir:
        return {"status": "no project"}

    # Worktrees share the same .git, so a commit on a per-task branch will
    # also fire this hook from the worktree's cwd. Watch should only fire
    # on commits reachable from the operator's main HEAD; ignore others.
    if not git.is_ancestor(project_dir, req.commit_hash, "HEAD"):
        return {"status": "skipped — commit not on main"}

    triggered = await check_watch_triggers(req.commit_hash, project_dir)
    dispatched = []

    for job in triggered:
        context = build_trigger_context(
            "commit", project_dir=project_dir, commit_hash=req.commit_hash,
        )
        await db.enqueue_task(
            job["id"], "commit", trigger_detail=req.commit_hash, context=context
        )
        dispatched.append(job["id"])

    if dispatched:
        log.info("[hook] Commit %s triggered %d job(s): %s", req.commit_hash[:8], len(dispatched), dispatched)
        worker.notify()

    return {"status": "ok", "triggered": dispatched}
