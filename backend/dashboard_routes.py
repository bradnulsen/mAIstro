"""Dashboard route — operational analytics aggregation."""

import asyncio

from fastapi import APIRouter

from backend import database as db
from backend.state import require_project

router = APIRouter(tags=["dashboard"])


@router.get("/api/dashboard")
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
