"""mAistro backend — FastAPI app shell.

All routes live in dedicated `*_routes.py` modules. This file only wires the
application together: logging, lifespan, CORS, the `/health` check, and the
`include_router` calls.
"""

import logging
import os
from contextlib import asynccontextmanager

# Configure logging — default INFO, set MAISTRO_DEBUG=1 for DEBUG
_log_level = logging.DEBUG if os.environ.get("MAISTRO_DEBUG") else logging.INFO
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy third-party loggers even in debug mode
for _noisy in ("asyncio", "aiosqlite", "watchfiles", "httpcore", "httpx", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("maistro")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import appstate, database as db, scheduler, worker
from backend import state
from backend.config_routes import router as config_router
from backend.dashboard_routes import router as dashboard_router
from backend.feed_routes import router as feed_router
from backend.git_routes import router as git_router
from backend.governor_routes import router as governor_router
from backend.job_routes import router as job_router
from backend.learnings_routes import router as learnings_router
from backend.mcp_routes import router as mcp_router
from backend.project_routes import router as project_router
from backend.queue_routes import router as queue_router
from backend.system_routes import router as system_router


# ── Lifespan ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    appstate.init()
    await worker.start()
    await scheduler.start()
    yield
    await scheduler.stop()
    await worker.stop()
    await db.close_db()


app = FastAPI(title="mAistro", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(governor_router)
app.include_router(queue_router)
app.include_router(job_router)
app.include_router(learnings_router)
app.include_router(project_router)
app.include_router(feed_router)
app.include_router(git_router)
app.include_router(mcp_router)
app.include_router(dashboard_router)
app.include_router(config_router)
app.include_router(system_router)


@app.get("/health")
async def health():
    return {"status": "ok", "project": state.PROJECT_DIR}


# ── Static frontend (production / installed mode) ──────────
#
# In dev, Vite serves the SPA on :5173 with /api proxied here. In an
# installed (PyInstaller-bundled) build the frontend is shipped as a
# pre-built ``frontend/dist/`` next to the backend; mount it here so
# the user opens a single ``localhost:8420`` URL. Set
# ``MAISTRO_FRONTEND_DIST`` to override the resolution (e.g. for a
# bundled build that places dist elsewhere). When the directory is
# absent (the pure dev path), this block is a no-op.
def _resolve_frontend_dist() -> str | None:
    import sys

    override = os.environ.get("MAISTRO_FRONTEND_DIST")
    if override:
        return override if os.path.isdir(override) else None

    # PyInstaller bundles: data files live under _MEIPASS.
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidate = os.path.join(bundle_root, "frontend", "dist")
        if os.path.isdir(candidate):
            return candidate

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(repo_root, "frontend", "dist")
    return candidate if os.path.isdir(candidate) else None


_dist_dir = _resolve_frontend_dist()
if _dist_dir:
    log.info("Serving frontend bundle from %s", _dist_dir)
    _index_path = os.path.join(_dist_dir, "index.html")
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_dist_dir, "assets")),
        name="assets",
    )

    @app.get("/")
    async def _index():
        return FileResponse(_index_path)

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        # API routes are registered above; FastAPI matches them first.
        # This catch-all serves the SPA for any other path so deep
        # links (eg /governor) load index.html and React routes there.
        candidate = os.path.join(_dist_dir, full_path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(_index_path)
else:
    log.info("frontend/dist not found — running in dev mode (Vite serves the UI)")
