"""Shared mutable state and utilities for the mAistro backend.

Extracted so that worker.py, scheduler.py, and dispatch.py can share
common state and helpers without circular imports.
"""

from datetime import datetime, timezone

from fastapi import HTTPException

PROJECT_DIR: str | None = None
_switching: bool = False


def utcnow() -> str:
    """UTC timestamp string for database storage."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def require_project() -> str:
    """Raise HTTPException if no project is loaded or a switch is in progress.

    Returns the project directory path, capturing it atomically so callers
    don't need to read state.PROJECT_DIR separately (avoids race during switch).
    """
    if _switching:
        raise HTTPException(503, "Project switch in progress. Please retry.")
    if not PROJECT_DIR:
        raise HTTPException(400, "No project loaded. POST /api/project/open first.")
    return PROJECT_DIR
