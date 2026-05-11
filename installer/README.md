# mAistro Installer

Windows installer for mAistro. Wraps the PyInstaller one-folder bundle in an
Inno Setup `.exe` that drops the app under `Program Files\mAistro\` and creates
a Start-Menu entry.

## Building

```powershell
# from repo root
pip install pyinstaller
# (and Inno Setup 6 from https://jrsoftware.org/isdl.php)

./installer/build.ps1                 # full build
./installer/build.ps1 -Version 0.1.0  # stamp the installer filename
```

The full build runs:

1. `npm run build` (produces `frontend/dist/`)
2. `python -m PyInstaller maistro.spec` (produces `dist/maistro/`)
3. `ISCC installer/maistro.iss` (produces `dist/installer/maistro-setup-<version>.exe`)

Re-run individual stages with `-SkipFrontend`, `-SkipBundle`, `-SkipInstaller`.

## What gets installed

- `Program Files\mAistro\maistro.exe` — the launcher (also handles `--mcp-server`
  and `--governor-mcp` modes for dispatched agents)
- `Program Files\mAistro\_internal\` — bundled Python runtime, deps, frontend
- Start-Menu shortcut (and optional Desktop shortcut)

## What is *not* bundled

- **The Claude Code CLI.** mAistro invokes `claude` as a subprocess; the
  launcher prints a non-fatal warning if it isn't on PATH. Users install it
  separately from <https://claude.ai/code>.
- **Python.** The runtime is frozen into the bundle.

## What survives uninstall

Per-user app-data at `%APPDATA%\mAistro\`:

- `app.db` — recent projects, cross-project job templates

Per-project state lives at `<project>\.maistro\` and is independent of the
install — uninstall doesn't touch project DBs.

## App-data location

The bundle resolves the app-data directory in this order:

1. `MAISTRO_APPDATA` env var (override; useful for portable installs)
2. `%APPDATA%\mAistro\` on Windows
3. `~/Library/Application Support/mAistro/` on macOS
4. `$XDG_DATA_HOME/mAistro/` (default `~/.local/share/mAistro/`) on Linux

Dev runs (`python run.py` from a checkout) default to a repo-local
`.maistro/` so the active checkout keeps its own app state.
