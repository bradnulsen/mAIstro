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


@app.get("/health")
async def health():
    return {"status": "ok", "project": state.PROJECT_DIR}
