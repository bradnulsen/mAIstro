# Proposal: Installer Distribution for Local Single-User Deployment

## Status

Draft. Intent set; design open.

## Decision (iteration 1)

mAistro's production deployment shape is a **locally-run program for an individual end user**, distributed via an **installer**. Not a hosted service, not multi-tenant, not a team-shared backend.

This is the shape the platform was already built for — single `PROJECT_DIR` global, project DB inside the project directory, app DB at `<repo>/.maistro/app.db`, Claude CLI subprocess on the host, localhost ports — but currently it ships as "clone the repo, `pip install`, `npm install`, `python run.py` in one terminal and `npm run dev` in another." That's a developer's setup, not a user's. The installer closes that gap.

## Why

- The existing process model is already single-user single-tenant. Going production doesn't require structural change; it requires packaging.
- Non-technical operators (the controls engineer in the addendum to README.md, for instance) cannot run `pip install -r requirements.txt`. They can run an installer.
- Punting on multi-tenant/server-hosted deployment avoids a much larger structural project (auth, isolated workers, storage layer, project mounting, secrets). That's a separate proposal if and when it's needed; this one explicitly stops at the desktop.
- Decouples mAistro releases from the operator's Python toolchain — a pinned, tested combination of backend Python + frontend bundle ships as one artifact.

## Non-goals

Out of scope for this proposal — flag any creep:

- Multi-user / multi-tenant / hosted SaaS. Different proposal.
- Headless/server mode. mAistro requires a UI; the installer ships the full app.
- Mobile / web-only access. The frontend is served by the local backend.
- Auto-update infrastructure. v1 is "download a new installer to upgrade." Auto-update is a v2 concern.
- Code signing / notarization workflows. Required for distribution-at-scale, but not for the first private builds.
- Bundling the Claude CLI itself. The CLI is the user's responsibility to install + authenticate (per current README); the installer can detect-and-prompt but does not embed it.

## Current single-user assumptions to preserve

These are baked into the architecture and the installer should preserve them, not subvert them:

- `state.PROJECT_DIR` is a single global. One project active at a time.
- Project SQLite at `<project>/.maistro/maistro.db`. Per-project, gitignored.
- App-level SQLite at `<repo>/.maistro/app.db` — recent projects + cross-project job templates. **This must move** in the installed version (see Open Questions): user-app-data, not the repo dir.
- Worktrees at `<project>/.maistro/worktrees/task-<id>/`. Filesystem-local.
- Claude CLI invoked via subprocess (`cli.py`); requires the user's local install + auth.
- Backend on port 8420, frontend dev server on 5173. Production install collapses to one port — backend serves the frontend bundle from `frontend/dist/`.
- Localhost-only binding. No remote access.

## Dimensions to decide

The decision is "ship an installer." The shape of that installer is open. Each axis is independent:

### 1. Bundling technology

Three credible paths:

| Option | Pros | Cons |
|---|---|---|
| **PyInstaller-bundled backend + browser-based UI** | Smallest delta from current. Backend serves `frontend/dist/`. User opens browser to `localhost:8420`. | Browser is an awkward "app." No native window. Port collision possible. |
| **Tauri shell** wrapping the backend | Native window, proper app icon, system-tray, autostart, notifications. Rust-based shell is small. | Adds a Rust toolchain to the build pipeline. Tauri ↔ Python IPC requires sidecar pattern. |
| **Electron shell** wrapping the backend | Same native-app benefits as Tauri, more familiar tooling. | Bundle is fat (~150MB+). STRATEGY.md non-goal historically against Electron specifically. |

Recommended starting point: **PyInstaller-bundled backend + browser-based UI** as v1. Lowest mechanical cost, ships fastest, validates the assumption that an installer is what's needed at all. Native-window wrapper (Tauri preferred) is v2 if the browser-tab UX proves unacceptable.

This revises the existing STRATEGY.md non-goal against Electron/Tauri — that non-goal was written when "Vite + Python works for the target user" assumed the target user *was* a developer. Once the target user is broader, the non-goal needs to be amended (not deleted: it still rules out Electron specifically; Tauri stays open).

### 2. Installer format

OS-specific. Windows-first (the user is on Windows; the README's Quick Start currently assumes Windows-friendly tooling).

- **Windows**: Inno Setup or WiX/MSI. Inno is simpler; MSI is more enterprise-friendly. Start with Inno.
- **macOS**: `.dmg` with a notarized `.app`. Defer until Windows works.
- **Linux**: Defer entirely. Desktop Linux is a small slice of the audience and adds packaging complexity (deb / rpm / AppImage / Flatpak — pick one, none fits everyone).

### 3. Where per-user data lives

Currently the app DB is at `<repo>/.maistro/app.db`, which only makes sense when running from a checkout. Installed builds need a stable, OS-conventional location:

- Windows: `%APPDATA%\mAistro\` (e.g. `C:\Users\<user>\AppData\Roaming\mAistro\app.db`)
- macOS: `~/Library/Application Support/mAistro/`
- Linux: `~/.local/share/mAistro/` (XDG)

`backend/appstate.py` is the single owner of the app-DB path. One change point, governed by a config or env override (`MAISTRO_APPDATA`) so dev runs can still use the repo-local path.

Project DBs stay where they are — inside each project's `.maistro/` directory. That's git-aware and the right shape regardless of how the binary is distributed.

### 4. Claude CLI dependency

The CLI is required, can't be embedded (Anthropic-licensed), and requires the user's own auth. Three ways to handle it:

- **Detect-and-prompt at startup**. If `claude` is not on PATH, the UI shows a setup screen with installation instructions. Continues into the rest of the app. Recommended.
- **Bundle the install step into the installer**. Probably violates the CLI's license; requires investigation. Skip unless explicitly cleared.
- **Defer to README**. User installs CLI separately. Status quo. Acceptable for technical-leaning early adopters but not ideal for the broader audience the installer is meant for.

Recommended default: detect-and-prompt. Add a "CLI status" indicator in the UI (similar to MCP server health pills) that shows authenticated / missing / unauthenticated.

### 5. Single-instance behavior

If a user runs the installer twice (or double-clicks the start-menu icon while it's already running), the second backend hits an "address already in use" error on port 8420. The installer/launcher should handle this:

- Check whether something already binds 8420 → if so, open the browser to the existing instance and exit.
- Use a per-user lock file or named pipe for the same purpose.

Trivial but easy to miss; calling it out so it's not a v1.1 surprise.

### 6. Auto-start, system tray, autoupdate

All v2. Don't bake into v1. The installer's job is "make it runnable"; making it run continuously in the background is a distinct UX decision and shouldn't gate the first installer release.

## Sequencing

This is a **future** priority. Strictly ordered after current in-flight work:

- P1 (Task Workspace Isolation) — done.
- P2 (External MCP Polish) — in flight per STRATEGY.md.
- P3 (Task Session Interrogation) — open.
- P4 (Structural Remediation: R7 async git, R8 glob matching, R5 read-side residual) — open.
- **Then** this proposal becomes a candidate for promotion.

When promoted, suggested step order:

1. Move app-DB path resolution into a single config-driven location (`appstate.py`); add `MAISTRO_APPDATA` override; verify dev mode still works.
2. Wire backend to serve `frontend/dist/` in production mode (likely already works via FastAPI static; confirm and document the prod-vs-dev split).
3. PyInstaller spec for the backend; smoke-test the bundled exe binds 8420 and serves the UI.
4. Inno Setup script for Windows; produce a one-click installer that drops the bundled backend into Program Files, adds a Start Menu entry, opens the browser.
5. Detect-and-prompt for Claude CLI on first launch.
6. Single-instance launcher.
7. Code signing (deferred until distribution path is clear).
8. macOS / Linux only after Windows is stable.

## Open Questions

1. **Browser-based UI vs. native window for v1.** Recommendation above is browser-based. Worth a deliberate call before starting — it determines whether the build pipeline learns Tauri/Rust or stays pure Python.
2. **Where does the binary's own state live vs. the user's project state?** Bundled exe should be read-only; per-user state in `%APPDATA%`. Per-project state in `.maistro/`. Worktrees stay project-local. Worth diagramming once the bundling tech is chosen.
3. **What does "open a project" look like for a user who doesn't have one yet?** Currently the file picker assumes the user has a git repo somewhere. The installer audience may not. Out of scope here, but worth flagging — first-run UX may need a "create new project" wizard.
4. **Telemetry / crash reports.** Distinct from auto-update. None today, none required for v1; flagging because once the binary is in users' hands without dev observation, "did it crash?" becomes hard to answer without it.
5. **License / pricing model.** Out of scope for this proposal; worth noting that the installer is what makes any commercial story possible.

## Second-order effects

- The "no migration system" stance (per memory) survives intact — installer upgrades that include schema changes still use the bespoke-script-via-`migrate_db.py` pattern. The installer can ship that script and the user can run it from the UI ("upgrade project DB" button).
- Once an installer exists, the README's Quick Start needs a parallel section for end users vs. developers. The current Quick Start assumes a developer.
- Parallel dispatch (currently deferred) is unaffected — it's a same-process concurrency change, orthogonal to packaging.
- Multi-project orchestration (currently deferred) becomes *more* defensible to defer once the deployment shape is locked as single-user-single-project. If multi-project is ever wanted, it's a per-process change, not a multi-tenant one.
