"""Shared mutable state for the mAistro backend.

Extracted from main.py so that worker.py and scheduler.py can access
PROJECT_DIR without circular imports back into the FastAPI app module.
"""

PROJECT_DIR: str | None = None
