"""Config and job-template routes.

Project-scoped config keys live in the project DB (`db.get_config` /
`db.set_config`). Job templates live in the app-level DB (`appstate`) — they
are reusable job definitions shared across projects.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend import appstate, database as db
from backend.state import require_project

log = logging.getLogger("maistro.config_routes")

router = APIRouter(tags=["config"])


class ConfigRequest(BaseModel):
    value: str


class SaveTemplateRequest(BaseModel):
    name: str
    properties: dict


# ── Config Routes ───────────────────────────────────────────

@router.get("/api/config/")
async def get_config():
    require_project()
    return await db.get_config()


@router.post("/api/config/{key}")
async def set_config(key: str, req: ConfigRequest):
    require_project()
    await db.set_config(key, req.value)
    return {"status": "ok"}


# ── Job Template Routes ─────────────────────────────────────

@router.get("/api/templates")
async def list_templates():
    """List all saved job templates (app-level, not project-specific)."""
    return appstate.list_templates()


@router.post("/api/templates")
async def save_template(req: SaveTemplateRequest):
    """Save a job definition as a reusable template."""
    tid = appstate.save_template(req.name, req.properties)
    log.info("[templates] Saved template '%s' (id=%d)", req.name, tid)
    return {"id": tid, "status": "created"}


@router.patch("/api/templates/{template_id}")
async def update_template(template_id: int, req: SaveTemplateRequest):
    """Overwrite an existing template."""
    appstate.update_template(template_id, req.name, req.properties)
    log.info("[templates] Updated template '%s' (id=%d)", req.name, template_id)
    return {"status": "updated"}


@router.delete("/api/templates/{template_id}")
async def delete_template(template_id: int):
    """Delete a job template."""
    appstate.delete_template(template_id)
    return {"status": "deleted"}
