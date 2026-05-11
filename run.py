"""mAistro launcher — starts the backend server, or routes into one of the
stdio MCP servers when the bundle is invoked with a mode flag.

Default mode is "serve uvicorn." In a PyInstaller bundle the same exe is
re-spawned by ``mcp_config.py`` / ``governor.py`` to host the platform's
internal stdio MCP server and the Governor's MCP server — they pass
``--mcp-server`` / ``--governor-mcp`` and we route accordingly. Without
this routing, ``sys.executable`` in the bundle would re-launch uvicorn
instead of running the MCP server, breaking dispatch in installed builds.

Dev runs default the app-DB to the repo-local ``.maistro/`` so an active
checkout keeps its recent-projects list and templates without needing to
migrate to the user-app-data location an installed build uses. Override
by setting ``MAISTRO_APPDATA`` before launching.

Installed-build niceties (only when frozen / when ``--launch`` semantics
apply): if port 8420 is already serving mAistro, just open the browser
and exit cleanly — double-clicking the icon a second time should focus
the running app, not error. If something *else* holds the port, surface
that to the user before uvicorn explodes. We also warn (non-fatal) if
``claude`` isn't on PATH, since the platform can't dispatch without it.
"""

import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request


PORT = 8420
HOST = "127.0.0.1"


def _dispatch_subprocess_mode(argv: list[str]) -> bool:
    """Return True if argv selects a subprocess MCP mode and we routed."""
    if "--mcp-server" in argv:
        from backend import mcp_server
        mcp_server.main()
        return True
    if "--governor-mcp" in argv:
        from backend import governor_mcp
        governor_mcp.main()
        return True
    return False


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _is_maistro_on_port(host: str, port: int) -> bool:
    """Probe /health to confirm the listener is mAistro, not some other app."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return '"status"' in body and "ok" in body
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _open_browser_when_ready(url: str, timeout_s: float = 30.0) -> None:
    """Poll /health, then open the user's default browser. Best-effort."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _is_maistro_on_port(HOST, PORT):
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass
            return
        time.sleep(0.3)


def _check_claude_cli() -> None:
    """Warn (non-fatal) if the Claude CLI is missing from PATH."""
    if shutil.which("claude") is None:
        sys.stderr.write(
            "\n[warning] Could not find the 'claude' CLI on PATH.\n"
            "          mAistro dispatches agents by invoking it as a subprocess --\n"
            "          install it from https://claude.ai/code and ensure it's\n"
            "          authenticated before dispatching tasks.\n\n"
        )
        sys.stderr.flush()


def _handle_already_running() -> bool:
    """If port 8420 is taken, decide what to do. Returns True if we handled
    the situation and the caller should exit (cleanly or not)."""
    if not _port_in_use(HOST, PORT):
        return False

    url = f"http://{HOST}:{PORT}/"
    if _is_maistro_on_port(HOST, PORT):
        sys.stderr.write(f"mAistro is already running. Opening {url} in your browser.\n")
        sys.stderr.flush()
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return True

    sys.stderr.write(
        f"\nPort {PORT} on {HOST} is in use by another application.\n"
        f"Close it (or change MAISTRO_PORT in a future build) and try again.\n\n"
    )
    sys.stderr.flush()
    return True


if __name__ == "__main__":
    if _dispatch_subprocess_mode(sys.argv):
        sys.exit(0)

    if not os.environ.get("MAISTRO_APPDATA"):
        repo_appdata = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".maistro")
        os.environ["MAISTRO_APPDATA"] = repo_appdata

    _check_claude_cli()

    if _handle_already_running():
        sys.exit(0)

    # Auto-open the browser once /health responds. Dev runs (Vite on :5173)
    # don't want this — only the bundled / installed build uses :8420 as
    # the user-facing URL.
    if getattr(sys, "frozen", False):
        threading.Thread(
            target=_open_browser_when_ready,
            args=(f"http://{HOST}:{PORT}/",),
            daemon=True,
        ).start()

    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=PORT,
    )
