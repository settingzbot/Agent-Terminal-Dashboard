"""Claude Code Dashboard — FastAPI application factory.

Standalone dashboard for managing Claude Code agent sessions.
"""

import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.routes_terminal import terminal_router, claude_router
from server.routes_agents import router as agents_router
from server.routes_ws import router as ws_router

# ── Paths ────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIST_DIR = BOT_DIR / "web" / "dist"


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def _lifespan(app_):
        logging.info("Claude Code Dashboard starting up")
        yield
        logging.info("Claude Code Dashboard shutting down")

    app = FastAPI(
        title="Claude Code Dashboard",
        version="1.0.0",
        lifespan=_lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────
    _ALLOWED_ORIGINS = [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",  # Vite dev server
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=False,
    )

    # ── Health check ──────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "claude-dashboard"}

    # ── Register route modules ────────────────────────────────────────
    app.include_router(terminal_router)
    app.include_router(claude_router)
    app.include_router(agents_router)
    app.include_router(ws_router)

    # ── Static assets ─────────────────────────────────────────────────
    if WEB_DIST_DIR.is_dir():
        assets_dir = WEB_DIST_DIR / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="dashboard_assets",
            )

        # Serve the SPA entry point at the root.
        index_file = WEB_DIST_DIR / "index.html"
        if index_file.is_file():

            @app.get("/", include_in_schema=False)
            async def serve_index():
                return FileResponse(index_file)

    return app
