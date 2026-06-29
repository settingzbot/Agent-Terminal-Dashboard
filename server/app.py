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

from agents.json_persist import JsonFlag
from daemons import agent_client, terminal_client
from server.routes_terminal import terminal_router, claude_router
from server.routes_agents import router as agents_router
from server.routes_ws import router as ws_router

_LOG = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIST_DIR = BOT_DIR / "web" / "dist"

# The master watch-gate flag the agent-manager reads on startup (daemons/
# agent_manager.WATCH_GATE_FILENAME). Disarming it here writes the same
# gitignored {"enabled": false} sentinel so a clean shutdown durably stops the
# autonomous issue→PR loop — even if the agent-manager is already down and never
# gets the RPC. Re-stated as a literal (not imported) to keep the heavy daemon
# module out of the web server's import graph; the on-disk filename is a stable
# contract (see agents/json_persist.JsonFlag).
WATCH_GATE_FILE = BOT_DIR / "watch_gate.json"

# Process exit code the Settings → "Restart Dashboard" button triggers. The
# launcher scripts (scripts/launch_dashboard.ps1, scripts/start_server.bat) run
# uvicorn in a loop and relaunch in the SAME window on exactly this code — so a
# restart re-serves a freshly-rebuilt web/dist and reloads any edited backend
# code without the operator touching the terminal. Any OTHER exit code (Ctrl-C,
# window close, a real crash) ends the loop as usual. 42 is arbitrary but
# distinctive — picked so a normal 0/1 exit never trips the relaunch.
RESTART_EXIT_CODE = 42

# Process exit code the Settings → "Exit & Shut Down Everything" button triggers.
# A CLEAN 0 — deliberately NOT 42 — so the launcher loop FALLS THROUGH and ends
# instead of relaunching. This is the full-stop counterpart to the restart above:
# the handler first tears down both daemons + disarms the watch gate, then exits
# with this code so the whole stack stays down.
SHUTDOWN_EXIT_CODE = 0

# Delay before the worker exits, so the 200 response is flushed to the browser
# (which then polls /api/health and reloads once the new instance answers).
_RESTART_DELAY_S = 0.7


async def _full_shutdown_teardown() -> dict:
    """Tear down the whole stack ahead of the process exit. Best-effort: every
    step is independent and never raises, so one failing step still lets the
    others (and the exit) proceed. Returns a summary the UI can show.

    Order matters:
      1. **Disarm the watch gate first**, while the agent-manager is still alive,
         so it stops claiming new GitHub issues on its very next tick rather than
         firing one more run on the way down. The durable file write is the
         guarantee — the RPC is the "take effect immediately" nicety on top.
      2. **Stop the agent-manager** (scheduler + run tracking).
      3. **Stop the PTY/terminal manager**, which kills every terminal session
         (ConPTY children terminate, no orphans).

    The managers are normally orphaned daemons that SURVIVE a dashboard exit;
    this teardown is what makes "Exit" mean "stop everything", not "stop the web
    tier and leave the robots running".
    """
    summary: dict = {}

    # 1. Disarm the autonomous watch loop ("the git checks for issues"). Tell a
    #    live manager over RPC so it stops claiming at once; ALWAYS also write the
    #    durable flag so the disarm survives even if the manager is unreachable.
    if await agent_client._manager_port_open():
        try:
            await agent_client.manager.set_watch_gate(False)
        except Exception as e:  # noqa: BLE001 — never let a flaky RPC block exit
            _LOG.warning("watch-gate RPC disarm failed (writing flag directly): %s", e)
    try:
        JsonFlag(WATCH_GATE_FILE, default=False).save(False)
        summary["watch_gate_disarmed"] = True
    except Exception as e:  # noqa: BLE001
        _LOG.warning("watch-gate file disarm failed: %s", e)
        summary["watch_gate_disarmed"] = False

    # 2. Stop the agent-manager (restart_manager stops without respawning; it
    #    catches its own errors and returns {ok, method, detail}).
    try:
        summary["agent_manager"] = await agent_client.manager.restart_manager()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("agent-manager stop failed: %s", e)
        summary["agent_manager"] = {"ok": False, "method": "error", "detail": str(e)}

    # 3. Stop the PTY/terminal manager — kills every terminal session.
    try:
        summary["terminal_manager"] = await terminal_client.manager.restart_manager()
    except Exception as e:  # noqa: BLE001
        _LOG.warning("terminal-manager stop failed: %s", e)
        summary["terminal_manager"] = {"ok": False, "method": "error", "detail": str(e)}

    return summary


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

    # ── Shut down everything (Settings → "Exit & Shut Down Everything") ─
    @app.post("/api/shutdown")
    async def shutdown():
        """Stop the entire stack, then exit so the launcher loop ends.

        Unlike /api/restart (which exits with RESTART_EXIT_CODE so the launcher
        relaunches), this:
          1. disarms the watch gate so the autonomous issue→PR loop does NOT
             resume on a later launch,
          2. stops the agent-manager and the PTY/terminal manager (killing all
             terminal sessions — the daemons would otherwise outlive this exit),
          3. schedules os._exit(SHUTDOWN_EXIT_CODE=0) so the launcher's relaunch
             loop falls through and the whole dashboard stays down.

        The teardown is awaited BEFORE returning, so the 200 body carries an
        honest summary of what was stopped. The exit is then scheduled *after*
        the response is flushed (same trick as /api/restart). os._exit bypasses
        asyncio teardown that would otherwise hang on still-open WebSockets.
        """
        summary = await _full_shutdown_teardown()
        loop = asyncio.get_running_loop()
        loop.call_later(_RESTART_DELAY_S, os._exit, SHUTDOWN_EXIT_CODE)
        return {"ok": True, "shutting_down": True,
                "exit_code": SHUTDOWN_EXIT_CODE, **summary}

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

        # Root-level static files (favicon) live at the dist root, not /assets.
        favicon_png = WEB_DIST_DIR / "favicon.png"
        if favicon_png.is_file():

            @app.get("/favicon.png", include_in_schema=False)
            async def serve_favicon_png():
                return FileResponse(favicon_png)

        favicon_ico = WEB_DIST_DIR / "favicon.ico"
        if favicon_ico.is_file():

            @app.get("/favicon.ico", include_in_schema=False)
            async def serve_favicon_ico():
                return FileResponse(favicon_ico)

    return app
