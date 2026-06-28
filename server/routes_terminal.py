"""Terminal + Claude image-upload routes for the Claude Code Dashboard.

Phase 4 Wave B extraction from trident_dashboard.py — pure relocation,
no logic changes.  Every route handler is lifted verbatim; only the
decorator changes from @app (on the FastAPI app) to @router (on the
APIRouter object) and the route paths lose their /api/terminal and
/api/claude prefixes since the router prefix supplies them.

Two routers:
  terminal_router  — /api/terminal/*         (PTY session lifecycle)
  claude_router    — /api/claude/*           (Claude image upload)
"""

import json
import logging
import re
import secrets
import time
from urllib.parse import unquote
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from fastapi import APIRouter, Body, HTTPException, Request

from server.state import ws_mgr
from daemons import terminal_client


# ── Routers ───────────────────────────────────────────────────────────────────

terminal_router = APIRouter(prefix="/api/terminal", tags=["terminal"])
claude_router   = APIRouter(prefix="/api/claude",   tags=["claude"])


# ── Repo-root path ────────────────────────────────────────────────────────────

BOT_DIR = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════════════════
# Terminal PTY sessions  (/api/terminal/sessions*)
#
# Sessions live in terminal_client.manager (single module-level instance),
# survive websocket disconnects, and are torn down by the dashboard's lifespan
# hook. Each session is a PowerShell at BOT_DIR; the user types `claude` (or
# `claude --resume`) themselves once attached. Frontend is xterm.js over
# /ws/terminal/{id}.
# ═══════════════════════════════════════════════════════════════════════════════


@terminal_router.get("/sessions")
async def api_terminal_list():
    return {"sessions": await terminal_client.manager.list()}


@terminal_router.post("/sessions")
async def api_terminal_create(payload: dict = Body(default={})):
    """Body: {label?: str}. Always spawns a fresh PowerShell at the repo root."""
    label = None
    if isinstance(payload, dict):
        l = payload.get("label")
        if isinstance(l, str) and l.strip():
            label = l.strip()[:64]
    try:
        info = await terminal_client.manager.create(cwd=str(BOT_DIR), label=label)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return info


@terminal_router.delete("/sessions/{session_id}")
async def api_terminal_kill(session_id: str):
    ok = await terminal_client.manager.kill(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"killed": session_id}


@terminal_router.post("/manager/restart")
async def api_terminal_manager_restart():
    """Stop the standalone pty-manager process — kills EVERY terminal session
    (including any Claude session running inside one; ConPTY children are
    terminated, not orphaned).

    Does not respawn: the manager comes back lazily on the next terminal API
    access via terminal_client._ensure_manager(). If the manager isn't
    running at all this is a success no-op (method "not-running").

    Returns {ok, method, detail} where method is "rpc" (graceful shutdown
    RPC), "pid-kill" (verified force-kill fallback), or "not-running".
    """
    result = await terminal_client.manager.restart_manager()
    if not result.get("ok"):
        raise HTTPException(500, result.get("detail") or "terminal manager restart failed")
    return result


@terminal_router.patch("/sessions/{session_id}")
async def api_terminal_rename(session_id: str, payload: dict = Body(default={})):
    """Body: {label: str}. Renames the session tab."""
    label = None
    if isinstance(payload, dict):
        l = payload.get("label")
        if isinstance(l, str) and l.strip():
            label = l.strip()[:64]
    if not label:
        raise HTTPException(400, "label is required and must be a non-empty string")
    ok = await terminal_client.manager.rename(session_id, label)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"renamed": session_id, "label": label}


# ═══════════════════════════════════════════════════════════════════════════════
# Claude image attachments  (/api/claude/upload-image)
#
# Receive an image upload from the mobile dashboard, save it to a temp dir
# under the repo, return the absolute path. The Claude tab's chat box pastes
# that path into the input so the user can send it as part of their message;
# Claude Code reads the file with its Read tool.
#
# Why raw POST body (not multipart/form-data): avoids a python-multipart dep
# on the bot host. We only ever upload one file per request, so the request
# body IS the file. The Content-Type tells us the MIME -> extension.
#
# Storage: BOT_DIR/.tmp/claude_uploads/, gitignored. Files persist across
# sessions; cleanup is manual for now (small surface, low blast radius).
# Cap: 20MB to match Claude's per-image limit.
# ═══════════════════════════════════════════════════════════════════════════════

CLAUDE_UPLOAD_DIR = BOT_DIR / ".tmp" / "claude_uploads"
CLAUDE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20MB
_CLAUDE_IMG_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/heif": "heif",
}


@claude_router.post("/upload-image")
async def api_claude_upload_image(request: Request):
    """Save a posted image to .tmp/claude_uploads/ and return its absolute path.

    Body: raw image bytes. Content-Type must start with `image/`.
    Returns: {path, filename, size}.
    """
    ct = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not ct.startswith("image/"):
        raise HTTPException(400, "expected image/* Content-Type")

    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > CLAUDE_UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"image > {CLAUDE_UPLOAD_MAX_BYTES} bytes")

    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if len(body) > CLAUDE_UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"image > {CLAUDE_UPLOAD_MAX_BYTES} bytes")

    ext = _CLAUDE_IMG_EXT.get(ct, "bin")
    fname = f"img-{int(time.time())}-{secrets.token_hex(4)}.{ext}"
    CLAUDE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fpath = CLAUDE_UPLOAD_DIR / fname
    with open(fpath, "wb") as f:
        f.write(body)
    return {"path": str(fpath), "filename": fname, "size": len(body)}


# ═══════════════════════════════════════════════════════════════════════════════
# Claude file attachments  (/api/claude/upload-file)
#
# Generalization of /upload-image (2026-06-10): the desktop Claude tab can
# attach zips, text/markdown notes, and images — anything Claude Code can read
# off disk (or unzip itself). Same raw-body transport (no python-multipart
# dep); the original filename rides in the X-Filename header (URI-encoded by
# the client) so the saved file keeps a recognizable name + extension.
#
# The extension allowlist is the security boundary: paths are never taken
# from the client (basename only, sanitized), the write dir is fixed, and
# anything outside the list is rejected with the list in the error message.
# ═══════════════════════════════════════════════════════════════════════════════

CLAUDE_FILE_MAX_BYTES = 50 * 1024 * 1024  # 50MB — zips are the big case
_CLAUDE_FILE_EXT_ALLOW = {
    # images (same set as /upload-image)
    "png", "jpg", "jpeg", "webp", "gif", "heic", "heif",
    # archives
    "zip",
    # text-ish things Claude reads directly
    "txt", "md", "markdown", "json", "csv", "tsv", "log",
    "yaml", "yml", "toml", "html", "pdf",
}
# Content-Type → extension fallback for blobs with no filename (e.g. a
# clipboard screenshot pasted on desktop arrives as a nameless image blob).
_CLAUDE_FILE_CT_EXT = {
    **_CLAUDE_IMG_EXT,
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",  # what Windows Chrome sends for .zip
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
    "text/csv": "csv",
    "application/pdf": "pdf",
}


def _sanitize_upload_name(raw: str) -> str:
    """Basename-only, filesystem-safe stem. Empty result means 'unusable'."""
    # Strip any path components the client (or an attacker) sent.
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base[:80]


@claude_router.post("/upload-file")
async def api_claude_upload_file(request: Request):
    """Save a posted file to .tmp/claude_uploads/ and return its absolute path.

    Body: raw file bytes. Headers: Content-Type (MIME), X-Filename (optional,
    URI-encoded original filename — supplies the extension when present).
    Returns: {path, filename, size}.
    """
    raw_name = unquote(request.headers.get("x-filename") or "").strip()
    safe_name = _sanitize_upload_name(raw_name) if raw_name else ""

    if "." in safe_name:
        stem, ext = safe_name.rsplit(".", 1)
        ext = ext.lower()
    else:
        # No usable filename — fall back to the Content-Type for the extension.
        ct = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        stem, ext = (safe_name or "upload"), _CLAUDE_FILE_CT_EXT.get(ct, "")

    if ext not in _CLAUDE_FILE_EXT_ALLOW:
        allowed = ", ".join(sorted(_CLAUDE_FILE_EXT_ALLOW))
        raise HTTPException(400, f"file type .{ext or '?'} not allowed (allowed: {allowed})")

    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > CLAUDE_FILE_MAX_BYTES:
        raise HTTPException(413, f"file > {CLAUDE_FILE_MAX_BYTES} bytes")

    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if len(body) > CLAUDE_FILE_MAX_BYTES:
        raise HTTPException(413, f"file > {CLAUDE_FILE_MAX_BYTES} bytes")

    # Keep the original stem visible (helps Claude infer what the file is)
    # but suffix with time + random hex so repeat uploads never collide.
    fname = f"{stem or 'upload'}-{int(time.time())}-{secrets.token_hex(4)}.{ext}"
    CLAUDE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fpath = CLAUDE_UPLOAD_DIR / fname
    with open(fpath, "wb") as f:
        f.write(body)
    return {"path": str(fpath), "filename": fname, "size": len(body)}
