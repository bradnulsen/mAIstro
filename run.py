"""mAistro launcher — starts the backend server."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8420,
        reload=True,
        reload_dirs=["backend"],
    )
