"""
WebSocket broadcast manager for the Claude Code Dashboard.

Extracted from the TRIDENT dashboard's state.py — only WsManager retained.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Set

from starlette.websockets import WebSocket

# ── Paths ─────────────────────────────────────────────────────────────────────
# server/state.py is one level deeper than the repo root
BOT_DIR  = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════════════════
# WS MANAGER  (WebSocket broadcast manager)
# ═══════════════════════════════════════════════════════════════════════════════

class WsManager:
    def __init__(self):
        self._log_clients:    Set[WebSocket] = set()
        self._market_clients: Set[WebSocket] = set()
        self._terminal_clients: Set[WebSocket] = set()
        self._agent_clients:  Set[WebSocket] = set()

    async def accept_log(self, ws: WebSocket):
        await ws.accept()
        self._log_clients.add(ws)

    async def accept_market(self, ws: WebSocket):
        await ws.accept()
        self._market_clients.add(ws)

    async def accept_agent(self, ws: WebSocket):
        await ws.accept()
        self._agent_clients.add(ws)

    def add_terminal(self, ws: WebSocket):
        """Register a terminal WebSocket that has already been accepted."""
        self._terminal_clients.add(ws)

    def drop(self, ws: WebSocket):
        self._log_clients.discard(ws)
        self._market_clients.discard(ws)
        self._terminal_clients.discard(ws)
        self._agent_clients.discard(ws)

    async def close_all_terminal(self, code: int = 4001):
        """Close every terminal WebSocket with *code*.
        4001 = Service Restart (registered private-use code).
        The browser sees an intentional close, not an abrupt RST, and
        reconnects after the new dashboard instance is up."""
        clients = self._terminal_clients.copy()
        for ws in clients:
            try:
                await ws.close(code=code)
            except Exception:
                pass
        self._terminal_clients.clear()

    async def broadcast_log(self, line: str, instance: str = "15m"):
        """Broadcast a log line to every connected log client.

        The ``instance`` tag discriminates which bot produced the line
        ("15m" or "4h") so multi-bot terminal tabs can filter per-bot.
        Defaults to "15m" for backward-compat with legacy clients that
        predate the multi-bot switcher (issue #131).
        """
        dead: Set[WebSocket] = set()
        for ws in self._log_clients:
            try:
                await ws.send_text(json.dumps({
                    "type": "log", "data": line, "instance": instance,
                }))
            except Exception:
                dead.add(ws)
        self._log_clients -= dead

    async def broadcast_market(self, payload: dict):
        dead: Set[WebSocket] = set()
        for ws in self._market_clients:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.add(ws)
        self._market_clients -= dead

    async def broadcast_agent(self, payload: dict):
        """Push a consolidated agent snapshot to every connected agent-panel client.
        Dead clients are pruned on write failure."""
        dead: Set[WebSocket] = set()
        for ws in self._agent_clients:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.add(ws)
        self._agent_clients -= dead


ws_mgr = WsManager()
