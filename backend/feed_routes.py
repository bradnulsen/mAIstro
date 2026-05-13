"""Feed routes — git activity feed enriched with task metadata.

The feed lists commits and overlays task info (trigger type, job ownership)
for commits authored by mAistro jobs.
"""

from fastapi import APIRouter, HTTPException

from backend import database as db, git
from backend.state import require_project

router = APIRouter(tags=["feed"])


@router.get("/api/feed/")
async def get_feed(limit: int = 50, offset: int = 0, job_id: int | None = None, path: str | None = None):
    project_dir = require_project()
    entries = await git.log(project_dir, limit=limit, skip=offset, path=path, with_stats=True)

    # Look up triggers keyed on the commit hashes we're actually rendering,
    # so the feed scan is O(visible window) instead of O(recent task window).
    hashes = [e["hash"] for e in entries if e.get("hash")]
    triggers = await db.get_triggers_by_result_commits(hashes)
    task_by_commit = {t["result_commit"]: t for t in triggers if t.get("result_commit")}

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


@router.get("/api/feed/{commit_hash}")
async def get_feed_item(commit_hash: str):
    project_dir = require_project()
    details = await git.show(project_dir, commit_hash, stat=True)
    if not details:
        raise HTTPException(404, "Commit not found")
    return {
        "details": details,
        "diff": await git.diff(project_dir, commit_hash),
    }
