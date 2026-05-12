"""System-environment routes — no project required.

Exposes information about the host environment the platform depends on
but doesn't manage itself. Today: presence of the Claude Code CLI on
PATH and an aggregated health/go-no-go view that combines that signal
with worker-side liveness and silent-failure detection.
"""

import logging
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter

from backend import worker

log = logging.getLogger("maistro.system_routes")

router = APIRouter(tags=["system"])

# Worker-loop tick freshness threshold. The loop wakes every 2s when
# idle (asyncio.wait_for timeout=2.0 in _loop), so a tick older than
# ~30s while no dispatch is active means something is wedged.
_TICK_STALE_SECONDS = 30


@router.get("/api/system/claude-status")
async def claude_status():
    """Detect whether the `claude` CLI is reachable on PATH.

    Returns ``{installed: bool, path: str | null}``. The launcher already
    emits a stderr warning when missing; this endpoint lets the UI surface
    the same state as a status pill alongside MCP server health.
    """
    path = shutil.which("claude")
    return {"installed": path is not None, "path": path}


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    # state.utcnow() emits "YYYY-MM-DD HH:MM:SS" UTC (no tz suffix).
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@router.get("/api/system/health")
async def system_health():
    """Aggregate go/no-go status for the running platform.

    Combines: Claude CLI presence (fatal if missing), worker-loop
    heartbeat freshness (warn if stale and not actively dispatching),
    and the silent-API-failure halt flag (fatal — worker is paused).

    Response shape (frontend contract):
        {
            ok: bool,                     # convenience: level == "ok"
            level: "ok" | "warn" | "fatal",
            checks: [
                {name, level, detail, ...optional fields},
                ...
            ],
            halt_reason: str | null,      # populated iff worker is halted
            halt_at: str | null,
        }
    """
    checks: list[dict] = []
    highest = "ok"

    def bump(level: str) -> None:
        nonlocal highest
        order = {"ok": 0, "warn": 1, "fatal": 2}
        if order[level] > order[highest]:
            highest = level

    # 1. Claude CLI presence — fatal if missing. Dispatches can't run.
    cli_path = shutil.which("claude")
    if cli_path:
        checks.append({"name": "claude_cli", "level": "ok", "detail": cli_path})
    else:
        checks.append({
            "name": "claude_cli",
            "level": "fatal",
            "detail": "claude CLI not on PATH — agents cannot dispatch.",
        })
        bump("fatal")

    # 2. Worker health snapshot — heartbeat + silent-failure streak + halt.
    snap = worker.get_health_snapshot()
    active_id = snap.get("active_dispatch_id")
    last_tick = _parse_utc(snap.get("last_tick_at"))
    halt_reason = snap.get("halt_reason")
    silent_streak = snap.get("silent_streak", 0)

    if halt_reason:
        checks.append({
            "name": "worker",
            "level": "fatal",
            "detail": halt_reason,
            "halt_at": snap.get("halt_at"),
            "silent_streak": silent_streak,
        })
        bump("fatal")
    elif last_tick is None:
        # Worker hasn't started yet (or hasn't ticked once). Soft signal.
        checks.append({
            "name": "worker",
            "level": "warn",
            "detail": "worker loop has not ticked yet",
        })
        bump("warn")
    else:
        age = (datetime.now(timezone.utc) - last_tick).total_seconds()
        # While a dispatch is active the loop is blocked inside _process_trigger,
        # so a stale tick is expected. Only flag stale ticks when idle.
        if active_id is None and age > _TICK_STALE_SECONDS:
            checks.append({
                "name": "worker",
                "level": "warn",
                "detail": f"loop has not ticked in {int(age)}s",
                "last_tick_at": snap.get("last_tick_at"),
            })
            bump("warn")
        else:
            detail = (f"active task #{active_id}, last idle tick {int(age)}s ago"
                      if active_id else f"idle, last tick {int(age)}s ago")
            d = {"name": "worker", "level": "ok", "detail": detail,
                 "last_tick_at": snap.get("last_tick_at"),
                 "active_dispatch_id": active_id}
            if silent_streak > 0:
                # Streak in progress but not yet at halt threshold — surface
                # as warn so the UI can amber-light pre-emptively.
                d["level"] = "warn"
                d["silent_streak"] = silent_streak
                d["detail"] += f" — {silent_streak} consecutive $0/≤1-turn dispatch(es)"
                bump("warn")
            checks.append(d)

    return {
        "ok": highest == "ok",
        "level": highest,
        "checks": checks,
        "halt_reason": halt_reason,
        "halt_at": snap.get("halt_at"),
    }


@router.post("/api/system/health/clear")
async def clear_system_health():
    """Operator confirms the upstream issue is resolved — resume the worker.

    Resets the silent-failure streak and clears any health halt. Doesn't
    touch the operator's auto_dispatch queue setting.
    """
    worker.clear_health_halt()
    return {"ok": True}
