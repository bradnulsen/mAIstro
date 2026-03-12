"""Shared mutable state and utilities for the mAistro backend.

Extracted so that worker.py, scheduler.py, and dispatch.py can share
common state and helpers without circular imports.
"""

from datetime import datetime, timezone

PROJECT_DIR: str | None = None


def utcnow() -> str:
    """UTC timestamp string for database storage."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
