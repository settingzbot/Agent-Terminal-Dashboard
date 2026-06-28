"""Claude Code Dashboard — FastAPI application factory.

Standalone dashboard for managing Claude Code agent sessions.
"""

import asyncio
import contextlib
import logging
import os
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

# Process exit code the Settings → "Restart Dashboard" button triggers. The
# launcher scripts (scripts/launch_dashboard.ps1, scripts/start_server.bat) run
# uvicorn in a loop and relaunch in the SAME window on exactly this code — so a
# restart re-serves a freshly-rebuilt web/dist and reloads any edited backend
# code without the operator touching the terminal. Any OTHER exit code (Ctrl-C,
# window close, a real crash) ends the loop as usual. 42 is arbitrary but
# distinctive — picked so a normal 0/1 exit never trips the relaunch.
RESTART_EXIT_CODE = 42

# Delay before the worker exits, so the 200 response is flushed to the browser
# (which then polls /api/health and reloads once the new instance answers).
_RESTART_DELAY_S = 0.7


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

    # ── Restart (Settings → "Restart Dashboard") ──────────────────────
    @app.post("/api/restart")
    async def restart():
        """Exit the worker with RESTART_EXIT_CODE so the launcher relaunches it.

        The dashboard process itself holds no durable state — terminal sessions
        live in the standalone PTY manager daemon (a separate, orphaned process,
        see scripts/spawn_detached.py), so they survive this restart and the
        browser simply reconnects to them after reloading. We schedule the exit
        *after* returning so the HTTP 200 reaches the browser, which then polls
        /api/health and hard-reloads once the new instance answers.

        os._exit (not sys.exit) is deliberate: it bypasses the asyncio teardown
        that would otherwise hang on the still-open WebSockets, giving the
        launcher loop a clean, immediate exit code to act on.
        """
        loop = asyncio.get_running_loop()
        loop.call_later(_RESTART_DELAY_S, os._exit, RESTART_EXIT_CODE)
        return {"ok": True, "restarting": True, "exit_code": RESTART_EXIT_CODE}

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
