"""mAistro launcher — starts the backend server.

Dev runs default the app-DB to the repo-local ``.maistro/`` so an active
checkout keeps its recent-projects list and templates without needing to
migrate to the user-app-data location an installed build uses. Override
by setting ``MAISTRO_APPDATA`` before launching.
"""

import os

import uvicorn

if __name__ == "__main__":
    if not os.environ.get("MAISTRO_APPDATA"):
        repo_appdata = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".maistro")
        os.environ["MAISTRO_APPDATA"] = repo_appdata

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8420,
    )
