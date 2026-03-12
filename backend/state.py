"""Shared mutable state and utilities for the mAistro backend.

Extracted so that worker.py, scheduler.py, dispatch.py, and chat.py can share
common state and helpers without circular imports.
"""

from datetime import datetime, timezone

from fastapi import HTTPException

PROJECT_DIR: str | None = None


def utcnow() -> str:
    """UTC timestamp string for database storage."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def require_project():
    """Raise HTTPException if no project is loaded. Shared across route modules."""
    if not PROJECT_DIR:
        raise HTTPException(400, "No project loaded. POST /api/project/open first.")
