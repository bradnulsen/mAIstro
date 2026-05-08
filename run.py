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
"""

import os
import sys


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


if __name__ == "__main__":
    if _dispatch_subprocess_mode(sys.argv):
        sys.exit(0)

    if not os.environ.get("MAISTRO_APPDATA"):
        repo_appdata = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".maistro")
        os.environ["MAISTRO_APPDATA"] = repo_appdata

    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8420,
    )
