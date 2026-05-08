"""Project lifecycle routes — open, close, browse, recent.

Handles target project directory selection and switching, including the
OS-native directory picker and the readers-draining coordination on switch.
"""

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import appstate, database as db, git, mcpb_import, worker
from backend import state

log = logging.getLogger("maistro.project_routes")

router = APIRouter(tags=["project"])


class OpenProjectRequest(BaseModel):
    path: str


@router.get("/api/project/")
async def get_project():
    if not state.PROJECT_DIR:
        return {"loaded": False}
    return {
        "loaded": True,
        "path": state.PROJECT_DIR,
        "name": os.path.basename(state.PROJECT_DIR),
    }


@router.post("/api/project/open")
async def open_project(req: OpenProjectRequest):
    path = os.path.abspath(req.path)
    if not os.path.isdir(path):
        raise HTTPException(404, "Directory not found")

    switching = state.PROJECT_DIR and os.path.normpath(path) != os.path.normpath(state.PROJECT_DIR)
    if switching:
        active = worker.get_active_dispatch_id()
        if active is not None:
            raise HTTPException(409, f"Cannot switch projects while task #{active} is running. Cancel it first or wait for completion.")
        log.info("[project] Switching from %s to %s", state.PROJECT_DIR, path)

    # Coordinated project switch: flag prevents new work, close_db drains readers
    if switching:
        state._switching = True
    try:
        await git.ensure_repo(path)

        await db.init_db(path)
        state.PROJECT_DIR = path
    finally:
        state._switching = False
    added_ignore = git.ensure_gitignore(path)
    # Bootstrap before installing the hook so the initial commit doesn't fire it.
    # ensure_initial_commit handles fresh repos; for existing repos that just
    # had .gitignore amended, commit that change too so project-open never
    # leaves the working tree dirty (which would trip the integration path's
    # stash flow on the next merge).
    await git.ensure_initial_commit(path)
    if added_ignore:
        await git.commit_gitignore_additions_if_safe(path, added_ignore)
    git.install_post_commit_hook(path)
    appstate.touch_project(path)
    worker.notify()

    # Reap orphan staging dirs left from prior crashed bundle imports.
    reaped = mcpb_import.reap_staging(path)
    if reaped:
        log.info("[project] Reaped %d orphan bundle staging dir(s)", reaped)

    log.info("[project] Opened %s", path)
    return {"status": "ok", "path": path}


def _pick_directory_sync() -> str | None:
    """Blocking directory picker. Runs via asyncio.to_thread so it doesn't block the event loop."""
    import subprocess as sp
    import sys

    if sys.platform == "win32":
        import base64
        # Explorer-style OpenFileDialog (supports typing a path). Forcing it to
        # the foreground from a backend-spawned PowerShell requires explicit
        # Win32 calls — Windows blocks SetForegroundWindow from processes that
        # don't already own the foreground, but SwitchToThisWindow is exempt.
        # Script is passed via -EncodedCommand (base64 UTF-16LE) to avoid all
        # the quote-mangling that happens with -Command + subprocess.
        ps_script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$sig = @"
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SwitchToThisWindow(System.IntPtr hWnd, bool fAltTab);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
"@
$w = Add-Type -MemberDefinition $sig -Name W32 -Namespace Maistro -PassThru

$owner = New-Object System.Windows.Forms.Form
$owner.FormBorderStyle = 'None'
$owner.ShowInTaskbar = $false
$owner.Opacity = 0.01
$owner.Size = New-Object System.Drawing.Size(1,1)
$owner.StartPosition = 'Manual'
$owner.Location = New-Object System.Drawing.Point(-30000,-30000)
$owner.TopMost = $true
$owner.Show()
[void]$w::ShowWindow($owner.Handle, 9)
[void]$w::SwitchToThisWindow($owner.Handle, $true)
[void]$w::SetForegroundWindow($owner.Handle)
$owner.Activate()

$f = New-Object System.Windows.Forms.OpenFileDialog
$f.ValidateNames = $false
$f.CheckFileExists = $false
$f.CheckPathExists = $true
$f.FileName = 'Select Folder'
$f.Title = 'Select a project directory'
$result = $f.ShowDialog($owner)
$owner.Dispose()
if ($result -eq 'OK') { [System.IO.Path]::GetDirectoryName($f.FileName) } else { '' }
'''
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        result = sp.run(
            ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=600,
        )
        return result.stdout.strip() or None

    if sys.platform == "darwin":
        result = sp.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select a project directory")'],
            capture_output=True, text=True, timeout=600,
        )
        return result.stdout.strip().rstrip("/") or None

    for cmd in [
        ["zenity", "--file-selection", "--directory", "--title=Select a project directory"],
        ["kdialog", "--getexistingdirectory", os.path.expanduser("~")],
    ]:
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except FileNotFoundError:
            continue
    return None


@router.post("/api/project/browse")
async def browse_project():
    """Open an OS-native directory picker dialog. Returns selected path or null."""
    path = await asyncio.to_thread(_pick_directory_sync)
    if not path:
        return {"path": None}
    return {"path": os.path.abspath(path)}


@router.get("/api/project/recent")
async def recent_projects():
    return appstate.list_recent()


@router.delete("/api/project/recent")
async def remove_recent_project(path: str):
    appstate.remove_project(path)
    return {"status": "ok"}


@router.post("/api/project/close")
async def close_project():
    active = worker.get_active_dispatch_id()
    if active is not None:
        raise HTTPException(409, f"Cannot close project while task #{active} is running. Cancel it first or wait for completion.")
    state._switching = True
    try:
        state.PROJECT_DIR = None

        await db.close_db()
    finally:
        state._switching = False
    return {"status": "ok"}
