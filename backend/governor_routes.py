"""Governor routes — findings, manual trigger, run history, status."""

import logging

from fastapi import APIRouter, HTTPException

from backend import database as db, governor
from backend.state import require_project

log = logging.getLogger("maistro.governor")

router = APIRouter(prefix="/api/governor", tags=["governor"])


@router.get("/findings")
async def list_findings(status: str | None = None, type: str | None = None, limit: int = 50):
    require_project()
    return await db.get_governor_findings(status=status, type_=type, limit=limit)


@router.post("/findings/{finding_id}/approve")
async def approve_finding(finding_id: int):
    require_project()
    finding = await db.get_governor_finding(finding_id)
    if not finding:
        raise HTTPException(404, "Finding not found")
    if finding["type"] != "suggestion":
        raise HTTPException(400, "Only suggestions can be approved")
    if finding["status"] != "pending":
        raise HTTPException(400, f"Finding is {finding['status']}, not pending")

    await db.update_governor_finding(finding_id, status="approved")
    governor.spawn(governor.execute_suggestion(finding_id))
    return {"status": "approved"}


@router.post("/findings/{finding_id}/decline")
async def decline_finding(finding_id: int):
    require_project()
    finding = await db.get_governor_finding(finding_id)
    if not finding:
        raise HTTPException(404, "Finding not found")
    if finding["type"] != "suggestion":
        raise HTTPException(400, "Only suggestions can be declined")
    if finding["status"] != "pending":
        raise HTTPException(400, f"Finding is {finding['status']}, not pending")

    await db.update_governor_finding(finding_id, status="declined")
    return {"status": "declined"}


@router.post("/findings/{finding_id}/read")
async def mark_finding_read(finding_id: int):
    require_project()
    finding = await db.get_governor_finding(finding_id)
    if not finding:
        raise HTTPException(404, "Finding not found")

    await db.update_governor_finding(finding_id, status="read")
    return {"status": "read"}


@router.post("/findings/{finding_id}/dismiss")
async def dismiss_finding(finding_id: int):
    require_project()
    finding = await db.get_governor_finding(finding_id)
    if not finding:
        raise HTTPException(404, "Finding not found")

    await db.update_governor_finding(finding_id, status="dismissed")
    return {"status": "dismissed"}


@router.post("/trigger")
async def trigger_governor():
    require_project()
    governor.spawn(governor.run_governor("manual"))
    return {"status": "triggered"}


@router.get("/runs")
async def list_runs(limit: int = 20):
    require_project()
    return await db.get_governor_runs(limit=limit)


@router.get("/status")
async def governor_status():
    require_project()
    return await db.get_governor_status()


@router.get("/recent-tasks")
async def recent_tasks_for_governor(limit: int = 50):
    """Endpoint used by Governor MCP server to fetch recent tasks."""
    require_project()
    return await db.get_recent_tasks_for_governor(limit=limit)
