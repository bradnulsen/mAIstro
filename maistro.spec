# PyInstaller spec for the mAistro single-exe bundle.
#
# Build:
#     pip install pyinstaller
#     pyinstaller maistro.spec
#
# Smoke-test the result:
#     ./dist/maistro/maistro            # Linux/macOS
#     dist\maistro\maistro.exe          # Windows
#     curl http://localhost:8420/health
#
# The bundle is one-folder mode (--onedir equivalent) — produces
# dist/maistro/ with the exe + dependencies. One-file mode (--onefile)
# is rejected because it bloats startup (re-extract on every launch)
# and complicates re-spawning self for the MCP-server subprocess modes.
#
# Routing: run.py inspects argv. The default entry runs uvicorn; passing
# --mcp-server or --governor-mcp routes into the relevant stdio server
# (mcp_config.py / governor.py rewrite their config to use these flags
# in bundled mode so dispatch keeps working).

# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None
ROOT = os.path.abspath(os.path.dirname(SPEC))
FRONTEND_DIST = os.path.join(ROOT, "frontend", "dist")

# Frontend bundle is shipped as a data tree under frontend/dist relative
# to the bundle root. backend/main.py resolves it via the sibling repo
# layout — when frozen, _MEIPASS is the bundle root and that resolution
# still works because we mirror the layout below.
datas = []
if os.path.isdir(FRONTEND_DIST):
    datas.append((FRONTEND_DIST, "frontend/dist"))

# Backend MCP servers are imported lazily by run.py via
# `from backend import mcp_server` / `from backend import governor_mcp`.
# PyInstaller's import analysis will pick them up via run.py's argv
# branches — listing them as hiddenimports is belt-and-suspenders for
# tools that don't follow conditional imports through ast.
hiddenimports = [
    "backend.mcp_server",
    "backend.governor_mcp",
    "backend.main",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "aiosqlite",
]

a = Analysis(
    ["run.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="maistro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,        # backend logs go to the console window for v1.
                         # Tauri/Electron wrappers (v2) can hide the console.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "installer", "maistro.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="maistro",
)
