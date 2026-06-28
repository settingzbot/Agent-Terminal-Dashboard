"""
trident_pty_manager.py — Standalone PTY manager for the dashboard's Claude tab.

Why this process exists
-----------------------
The dashboard used to own PTY sessions in-process via trident_terminal.PtySession.
That meant dashboard restart = all terminal sessions killed. This process extracts
PTY ownership into a standalone daemon that survives dashboard restarts.

Architecture
------------
- One PtySession per terminal tab, each owning a PowerShell via ConPTY (pywinpty).
- JSON-lines TCP server on a well-known port (default 58999).
- Short-lived connections: create / list / kill (request → response → close).
- Long-lived connections: attach (bidirectional stream: PTY output → TCP,
  TCP → PTY input/resize).
- Server-side VT screen model per session (pyte HistoryScreen). Replay on
  reattach is the SERIALIZED current grid + scrollback, not a raw-byte log.
  The old ring concatenated output produced at whatever width was live at the
  time, so a second device attaching at a different width scattered the replay
  (lines wrapped at the wrong column; in-place repaints stopped collapsing).
  The pyte screen is the authoritative grid at the CURRENT width, so its
  serialization is always one coherent width. See [[footguns#150]] /
  [[synth/2026-06-15_terminal-replay-screen-model]].
- Rotating log file: logs/pty_manager.log (capped at ~1MB).

Protocol (JSON-lines, one JSON object per line)
-----------------------------------------------
Short-lived:
  → {"type":"list"}
  ← {"type":"sessions","sessions":[{id,label,cwd,created_at,alive,command,tag}]}

  → {"type":"create","label":"Session 2","cwd":"C:\\..."}
       optional: "command" (run instead of the default PowerShell shell) and
       "tag" (opaque string, e.g. "agent:<run-id>", so clients can tell an
       agent-hosted terminal apart from a user shell). Absent → PowerShell at
       cwd, command/tag null (byte-identical to the legacy behavior).
  ← {"type":"created","session":{id,label,cwd,created_at,alive,command,tag}}

  → {"type":"kill","session_id":"abc123"}
  ← {"type":"killed","session_id":"abc123"}

  → {"type":"rename","session_id":"abc123","label":"My Tab"}
  ← {"type":"renamed","session_id":"abc123","label":"My Tab"}

  → {"type":"shutdown"}
  ← {"type":"shutdown","ok":true,"killed_sessions":N}
    (graceful full shutdown: every session is killed via PtySession.kill so
     ConPTY children terminate, the ack is flushed, then the TCP server stops
     and the process exits. The dashboard respawns the manager lazily on the
     next terminal API access.)

Long-lived (attach):
  → {"type":"attach","session_id":"abc123"}           (open the stream)
  → {"type":"attach","session_id":"abc123",           (TAKEOVER variant: claim
       "takeover":true,"rows":40,"cols":56}             the resize lock + size
                                                        the PTY to this device
                                                        BEFORE the replay is
                                                        serialized, so history
                                                        comes back fit to it)
  ← {"type":"out","data":"<screen-state replay>"}    (first message on attach:
                                                      serialized pyte grid +
                                                      scrollback, chunked)
  ← {"type":"out","data":"..."}                       (live PTY output)
  → {"type":"in","data":"keystrokes"}                 (from browser)
  → {"type":"resize","rows":30,"cols":100}
  → {"type":"pause"}                                  (output flow control: stop
                                                       reading the ConPTY)
  → {"type":"resume"}                                 (resume ConPTY reads)
  ← {"type":"exit"}                                   (PowerShell exited)

Flow control (pause/resume) gates OUTPUT only — "in" and "resize" are handled
by the attach input loop, which is never blocked by a paused reader, so
keystrokes (Ctrl-C especially) always reach the PTY immediately. While paused,
unread output backpressures onto the child process through the OS pipe.
Unknown message types on the attach stream are ignored (if/elif with no else),
so an old dashboard talking to a new manager — or vice versa — degrades
cleanly to the pre-flow-control behavior.

Usage: python trident_pty_manager.py [--port 58999] [--workspace C:\\path\\to\\Trident]
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import faulthandler
import json
import logging
import os
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from typing import Optional

import pyte
import pyte.graphics as _pg

# ── constants ─────────────────────────────────────────────────────────────────

DEFAULT_PORT = 58999
DEFAULT_ROWS = 30
DEFAULT_COLS = 100
# Scrollback depth held in the server-side pyte screen, matched to the xterm
# client's own `scrollback: 5000` so a reattach can repaint exactly what the
# client could have shown. Each line is a sparse dict of styled cells (only
# written cells stored), so this is far lighter than 5000 full-width rows.
# Replaces the old 1MB raw-byte ring; see the module docstring for why a grid
# model beats a byte log across device width changes.
SCROLLBACK_LINES = 5000
QUEUE_DEPTH = 512        # max chunks queued per subscriber before dropping

# The client raises its read limit to handle the screen-state replay (see
# trident_terminal.py), but we also chunk the replay here so no single JSON line
# is ever oversized — robust regardless of the client's StreamReader limit, and
# it removes the latent ceiling entirely however large a serialized screen gets.
# 8192 chars stays under 64KB even if every byte escapes 6× (8192·6 = 49152 <
# 65536). See [[footguns#101]].
REPLAY_CHUNK_CHARS = 8192
# Lift the server-side StreamReader limit too, so a large paste arriving from the
# dashboard as one {"type":"in"} line doesn't trip the same 64KB readline wall.
STREAM_LIMIT = 8 * 1024 * 1024  # 8 MB

# ── logging ───────────────────────────────────────────────────────────────────

def _setup_logging(workspace: str) -> logging.Logger:
    log_dir = os.path.join(workspace, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "pty_manager.log")

    handler = RotatingFileHandler(
        log_path, maxBytes=1_048_576, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    log = logging.getLogger("pty_manager")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    # Also log to stderr so subprocess-launcher debugging is possible.
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"
        ))
        log.addHandler(stderr_handler)
    return log


# ═══════════════════════════════════════════════════════════════════════════════
# Screen serialization — pyte grid → ANSI replay
# ═══════════════════════════════════════════════════════════════════════════════
# Turn a pyte screen (the authoritative VT grid we feed every PTY byte into)
# back into an ANSI byte stream that reconstructs the same screen + scrollback
# when written to a freshly-reset client terminal. This is what attach() now
# replays instead of the raw byte ring. The "serialize-back-to-ANSI" step the
# 2026-06-13 diagnosis flagged as Option 2's only real cost lives here.

# Reverse pyte's code→name colour maps so a serialized Char can name its SGR
# code again. pyte stores 256-colour / truecolour cells as 6-hex strings, which
# _color_params emits as truecolour regardless of the map.
_FG_NAME_TO_CODE = {name: code for code, name in _pg.FG.items()}
_BG_NAME_TO_CODE = {name: code for code, name in _pg.BG.items()}

# Attribute identity of a cell, for collapsing styled runs in _render_line.
# Field names are read off pyte's Char (verified: data, fg, bg, bold, italics,
# underscore, strikethrough, reverse, blink).
_DEFAULT_ATTRS = ("default", "default", False, False, False, False, False, False)


def _attrs(char) -> tuple:
    return (char.fg, char.bg, char.bold, char.italics,
            char.underscore, char.reverse, char.strikethrough, char.blink)


def _color_params(color: str, *, is_fg: bool) -> list[str]:
    """SGR parameter(s) for one pyte colour value (Char.fg / Char.bg).

    "default" needs no param — every cell's SGR is emitted with a leading reset
    (see _sgr_for), so the default colour is already in effect. Named colours
    map back through pyte's own tables; 6-hex (256-colour / truecolour) is
    emitted as truecolour so the visible colour reproduces exactly.
    """
    if not color or color == "default":
        return []
    table = _FG_NAME_TO_CODE if is_fg else _BG_NAME_TO_CODE
    code = table.get(color)
    if code is not None:
        return [str(code)]
    if len(color) == 6:
        try:
            r, g, b = (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
        except ValueError:
            return []
        return (["38", "2"] if is_fg else ["48", "2"]) + [str(r), str(g), str(b)]
    return []


def _sgr_for(char) -> str:
    """Full SGR sequence (reset + this cell's attributes) for a pyte Char.

    Always leads with 0 (reset) so we never diff against a previous cell's
    residual attributes; the caller only emits this when the attribute tuple
    changes, so the cost is one sequence per styled run, not per character.
    """
    params = ["0"]
    if char.bold: params.append("1")
    if char.italics: params.append("3")
    if char.underscore: params.append("4")
    if char.blink: params.append("5")
    if char.reverse: params.append("7")
    if char.strikethrough: params.append("9")
    params += _color_params(char.fg, is_fg=True)
    params += _color_params(char.bg, is_fg=False)
    return "\x1b[" + ";".join(params) + "m"


def _render_line(line, columns: int) -> str:
    """Render one pyte line (a mapping x→Char) to ANSI, trimming trailing
    default/blank cells so rows aren't padded to full width. Returns "" for a
    wholly-blank line. A trailing SGR reset keeps styling from bleeding past
    the line end."""
    last_col = -1
    for x in range(columns):
        ch = line[x]
        if ch.data != " " or _attrs(ch) != _DEFAULT_ATTRS:
            last_col = x
    if last_col < 0:
        return ""
    out: list[str] = []
    cur = None
    for x in range(last_col + 1):
        ch = line[x]
        a = _attrs(ch)
        if a != cur:
            out.append(_sgr_for(ch))
            cur = a
        out.append(ch.data)
    out.append("\x1b[0m")
    return "".join(out)


def serialize_screen(screen) -> bytes:
    """Serialize a pyte (History)Screen — scrollback + visible grid + cursor —
    to an ANSI byte stream that reconstructs the current screen state on a
    freshly-reset client terminal.

    Autowrap is disabled around the body: every pyte line is already wrapped to
    the grid width, so a full-width line must occupy exactly one client row and
    not spill onto a second. Scrollback (history.top) is emitted first so it
    lands above the visible screen in the client's scrollback; then exactly
    `lines` visible rows (no newline after the last, which would scroll the
    screen up by one); then autowrap is restored and the cursor placed where
    the live screen has it.
    """
    cols = screen.columns
    rows = screen.lines
    parts: list[str] = ["\x1b[?7l\x1b[H\x1b[2J"]
    history = getattr(screen, "history", None)
    if history is not None:
        for line in list(history.top):
            parts.append(_render_line(line, cols))
            parts.append("\r\n")
    for y in range(rows):
        parts.append(_render_line(screen.buffer[y], cols))
        if y < rows - 1:
            parts.append("\r\n")
    cy = max(0, min(rows - 1, screen.cursor.y))
    cx = max(0, min(cols - 1, screen.cursor.x))
    parts.append("\x1b[?7h")
    parts.append(f"\x1b[{cy + 1};{cx + 1}H")
    return "".join(parts).encode("utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════════════════════
# PtySession — owns one PowerShell PTY
# ═══════════════════════════════════════════════════════════════════════════════

class PtySession:
    """Owns a Windows ConPTY-attached PowerShell. Fans raw output to TCP
    subscribers (live path, byte-faithful) AND feeds every byte into a
    server-side pyte screen whose serialization is the replay sent on
    reattach (see serialize_screen). The grid model is what makes replay
    coherent across device width changes."""

    def __init__(
        self,
        session_id: str,
        label: str,
        cwd: str,
        loop: asyncio.AbstractEventLoop,
        command: str | None = None,
        tag: str | None = None,
    ) -> None:
        from winpty import PtyProcess  # type: ignore

        self.id = session_id
        self.label = label
        self.cwd = cwd
        self.created_at = time.time()
        # Agent-runner extension (#25): an optional arbitrary command run instead
        # of the default PowerShell shell, and an optional opaque tag (e.g.
        # "agent:<run-id>") so clients can distinguish agent-hosted terminals
        # from user shells. Both are surfaced in info(). When `command` is None
        # the spawn is byte-identical to the legacy default shell.
        self.command = command
        self.tag = tag

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # Default: a bare PowerShell shell (unchanged). With a command, run it
        # through PowerShell's -Command so an arbitrary command string spawns
        # and its output flows over the same reader/subscriber path.
        if command:
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command]
        else:
            argv = ["powershell.exe", "-NoLogo"]
        self._proc = PtyProcess.spawn(
            argv,
            cwd=cwd,
            dimensions=(DEFAULT_ROWS, DEFAULT_COLS),
            env=env,
        )

        self._loop = loop
        self._lock = threading.Lock()
        self._subscribers: dict[str, asyncio.Queue] = {}  # sub_id → queue
        # Server-side VT screen: every PTY byte is fed in (reader thread) so
        # the grid + scrollback always reflect the live screen. Serialized on
        # attach for replay. HistoryScreen keeps SCROLLBACK_LINES of off-screen
        # history; ByteStream decodes UTF-8 statefully so a multibyte char
        # split across two 4096-byte reads reassembles correctly.
        self._screen = pyte.HistoryScreen(
            DEFAULT_COLS, DEFAULT_ROWS, history=SCROLLBACK_LINES, ratio=0.5,
        )
        self._stream = pyte.ByteStream(self._screen)
        self._closed = False
        self._claimed_sub: str | None = None  # sub_id of the viewer that "owns" resize

        # ── output flow control ──────────────────────────────────────────
        # Subscribers that have asked us to pause ConPTY reads (ACK-window
        # backpressure from the browser, relayed by the dashboard). The
        # reader thread blocks on _read_gate while ANY subscriber is paused;
        # unread output then backpressures onto the child process via the
        # OS pipe — the child stalls instead of us buffering megabytes.
        # The gate is per-SESSION (one ConPTY = one read stream), so one
        # slow viewer pauses output for all viewers of that session —
        # acceptable for a single-operator dashboard. Input ("in"/"resize")
        # is NEVER gated; only the reader thread is.
        self._paused_subs: set[str] = set()
        self._read_gate = threading.Event()
        self._read_gate.set()  # set = reads allowed

        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"pty-{session_id[:6]}",
            daemon=True,
        )
        self._reader.start()

    # ── public API ──────────────────────────────────────────────────────────

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            return self._proc.isalive()
        except Exception:
            return False

    def write(self, data: str) -> None:
        if self._closed:
            return
        try:
            self._proc.write(data)
        except Exception:
            pass

    def resize(self, rows: int, cols: int) -> None:
        if self._closed:
            return
        rows = max(2, min(500, int(rows)))
        cols = max(10, min(500, int(cols)))
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass
        # Keep the screen model the same size as the ConPTY so its serialized
        # replay matches what the child is now drawing into. Under the lock —
        # the reader thread feeds the same screen.
        with self._lock:
            try:
                self._screen.resize(rows, cols)
            except Exception:
                pass

    def kill(self) -> None:
        """Force-terminate the PTY. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Wake a flow-control-paused reader so it can observe _closed and exit
        # instead of sleeping out its wait timeout.
        self._read_gate.set()
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass

    def pause_output(self, sub_id: str) -> None:
        """Flow control: stop the reader thread from pulling ConPTY output.
        Called when the browser's ACK window is full (too many unprocessed
        bytes in flight). Idempotent per subscriber."""
        with self._lock:
            self._paused_subs.add(sub_id)
            self._read_gate.clear()

    def resume_output(self, sub_id: str) -> None:
        """Flow control: this subscriber's ACK window has drained — clear its
        pause. Reads resume once NO subscriber holds a pause."""
        with self._lock:
            self._paused_subs.discard(sub_id)
            if not self._paused_subs:
                self._read_gate.set()

    def add_subscriber(self) -> tuple[str, asyncio.Queue, bytes]:
        """Register a new subscriber. Returns (sub_id, queue, replay_bytes).
        The caller must drain `queue` and write to the TCP socket.
        `replay_bytes` is the serialized current screen (grid + scrollback +
        cursor) to send before live output."""
        sub_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
        with self._lock:
            self._subscribers[sub_id] = queue
            try:
                replay = serialize_screen(self._screen)
            except Exception:
                # Never let a serialization edge case block an attach — a
                # subscriber with no replay still gets live output.
                replay = b""
        return sub_id, queue, replay

    def remove_subscriber(self, sub_id: str) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)
            # A subscriber that disconnects while holding a flow-control pause
            # (e.g. browser killed mid-burst, dashboard restart) must not wedge
            # the session — drop its pause and reopen the gate if it was the
            # last pauser.
            self._paused_subs.discard(sub_id)
            if not self._paused_subs:
                self._read_gate.set()
        # If the departing subscriber held the claim, release it so remaining
        # viewers aren't locked out of resize forever.
        if self._claimed_sub == sub_id:
            self._claimed_sub = None
            self._broadcast_claim_state()

    def _broadcast_claim_state(self, you_sub: str | None = None) -> None:
        """Push current claim state to all subscribers. The subscriber whose
        sub_id matches `you_sub` gets an extra `you:true` field so the frontend
        can show "you own the lock" vs "someone else owns it." """
        with self._lock:
            subs = list(self._subscribers.items())
        for _sub_id, queue in subs:
            payload: dict
            if self._claimed_sub:
                payload = {"type": "claimed", "by": self._claimed_sub[:6]}
                if _sub_id == you_sub or _sub_id == self._claimed_sub:
                    payload["you"] = True
            else:
                payload = {"type": "unclaimed"}
            blob = (json.dumps(payload) + "\n").encode("utf-8")
            try:
                self._loop.call_soon_threadsafe(
                    queue.put_nowait, ("meta", blob)
                )
            except (RuntimeError, asyncio.QueueFull):
                pass

    def info(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "alive": self.is_alive(),
            # Agent-runner fields (#25). Always present; None for a plain user
            # shell so clients can rely on the keys existing.
            "command": self.command,
            "tag": self.tag,
        }

    # ── internals ───────────────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        _log = logging.getLogger("pty_manager")
        try:
            while True:
                # ── flow control gate ────────────────────────────────────
                # While any subscriber holds a pause, don't read from the
                # ConPTY — the child process backpressures on the OS pipe,
                # which is exactly where we want a `yes`-style burst to
                # stall. The 1s timeout keeps this loop responsive to
                # kill() / session death even if a resume never arrives
                # (belt-and-braces; remove_subscriber also clears stale
                # pauses on disconnect).
                if not self._read_gate.wait(timeout=1.0):
                    if self._closed:
                        break
                    continue
                if self._closed:
                    break
                try:
                    chunk = self._proc.read(4096)
                except EOFError:
                    break
                except Exception:
                    break
                if not chunk:
                    if not self._proc.isalive():
                        break
                    continue

                if isinstance(chunk, str):
                    data = chunk.encode("utf-8", errors="replace")
                else:
                    data = bytes(chunk)

                with self._lock:
                    # Feed the server-side screen model. pyte applies cursor
                    # moves, wraps, and scrolling exactly as the client xterm
                    # would, so the grid + scrollback stay an accurate picture
                    # of the live screen for serialize_screen() to replay. A
                    # malformed sequence must never kill the reader thread.
                    try:
                        self._stream.feed(data)
                    except Exception:
                        pass
                    subs = list(self._subscribers.items())

                # Fan out with a BLOCKING put per subscriber. The old
                # call_soon_threadsafe(put_nowait) silently dropped chunks on
                # a full queue — and its `except asyncio.QueueFull` could
                # never fire, because put_nowait raised on the LOOP thread,
                # not here (footguns #120, manager hop). Blocking the reader
                # thread on the slowest subscriber is the same backpressure
                # philosophy as the read gate above: the ConPTY child stalls
                # on the OS pipe instead of us losing output. The 1s
                # future-wait loop keeps kill() responsive, mirroring the
                # gate's 1s wakeup.
                for sub_id, queue in subs:
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            queue.put(data), self._loop
                        )
                    except RuntimeError:
                        continue  # loop closed
                    while True:
                        try:
                            fut.result(timeout=1.0)
                            break
                        except concurrent.futures.TimeoutError:
                            if self._closed:
                                fut.cancel()
                                break
                        except Exception:
                            break  # subscriber queue/loop went away
        finally:
            self._closed = True
            with self._lock:
                subs = list(self._subscribers.items())
            for sub_id, queue in subs:
                try:
                    self._loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel
                except RuntimeError:
                    pass
            _log.info(f"pty session {self.id[:6]} reader exited")


# ═══════════════════════════════════════════════════════════════════════════════
# PtyManagerServer — TCP server
# ═══════════════════════════════════════════════════════════════════════════════

class PtyManagerServer:
    def __init__(self, port: int, workspace: str) -> None:
        self._port = port
        self._workspace = workspace
        self._sessions: dict[str, PtySession] = {}
        self._next_seq = 1
        self._lock = threading.Lock()
        self._log = logging.getLogger("pty_manager")
        # Set in start(); the shutdown RPC closes it to unblock serve_forever.
        self._server: asyncio.AbstractServer | None = None

    # ── session registry ────────────────────────────────────────────────────

    def _make_label(self) -> str:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
        return f"Session {seq}"

    def _create_session(
        self,
        label: str | None,
        cwd: str,
        command: str | None = None,
        tag: str | None = None,
    ) -> dict:
        session_id = uuid.uuid4().hex
        display_label = label or self._make_label()
        loop = asyncio.get_running_loop()
        session = PtySession(
            session_id=session_id,
            label=display_label,
            cwd=cwd or self._workspace,
            loop=loop,
            command=command,
            tag=tag,
        )
        with self._lock:
            self._sessions[session_id] = session
        self._log.info(
            "session created: %s (%s)%s",
            session_id[:6], display_label, f" tag={tag}" if tag else "",
        )
        return session.info()

    def _list_sessions(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        out = []
        dead_ids = []
        for s in sessions:
            info = s.info()
            if info["alive"]:
                out.append(info)
            else:
                dead_ids.append(s.id)
        if dead_ids:
            with self._lock:
                for d in dead_ids:
                    self._sessions.pop(d, None)
            self._log.debug("cleaned up %d dead sessions", len(dead_ids))
        return out

    def _get_session(self, session_id: str) -> PtySession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _kill_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if not session:
            return False
        session.kill()
        self._log.info("session killed: %s", session_id[:6])
        return True

    # ── TCP server ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Write PID file so the dashboard can find us.
        pid_path = os.path.join(self._workspace, "logs", "pty_manager.pid")
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))
        self._log.info("PID %d written to %s", os.getpid(), pid_path)

        server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=self._port,
            limit=STREAM_LIMIT,
        )
        self._server = server
        self._log.info("listening on 127.0.0.1:%d", self._port)

        async with server:
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                # server.close() (the shutdown RPC) cancels the serve_forever
                # future — this is the graceful-exit path, not an error.
                pass
        self._log.info("TCP server stopped — pty-manager exiting")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError:
            writer.close()
            return
        if not line:
            writer.close()
            return

        try:
            msg = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.close()
            return

        msg_type = msg.get("type")

        if msg_type == "list":
            await self._cmd_list(writer)
        elif msg_type == "create":
            await self._cmd_create(writer, msg)
        elif msg_type == "kill":
            await self._cmd_kill(writer, msg)
        elif msg_type == "rename":
            await self._cmd_rename(writer, msg)
        elif msg_type == "shutdown":
            await self._cmd_shutdown(writer)
        elif msg_type == "attach":
            await self._cmd_attach(reader, writer, msg)
        else:
            writer.close()

    # ── command handlers ────────────────────────────────────────────────────

    async def _cmd_list(self, writer: asyncio.StreamWriter) -> None:
        sessions = self._list_sessions()
        await self._send_line(writer, {"type": "sessions", "sessions": sessions})
        writer.close()

    async def _cmd_create(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        label = msg.get("label")
        cwd = msg.get("cwd", self._workspace)
        if isinstance(label, str):
            label = label.strip()[:64] or None
        # Agent-runner extension (#25): optional command + tag. Both are only
        # honored when they arrive as non-empty strings — a non-string (a client
        # bug) degrades silently to the legacy default-shell behavior rather than
        # crashing the handler.
        command = msg.get("command")
        if not isinstance(command, str) or not command.strip():
            command = None
        tag = msg.get("tag")
        if isinstance(tag, str):
            tag = tag.strip()[:128] or None
        else:
            tag = None
        try:
            info = self._create_session(label=label, cwd=cwd, command=command, tag=tag)
            await self._send_line(writer, {"type": "created", "session": info})
        except ImportError:
            await self._send_line(
                writer,
                {"type": "error", "message": "pywinpty not installed"},
            )
        writer.close()

    async def _cmd_kill(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        session_id = msg.get("session_id")
        if not isinstance(session_id, str):
            await self._send_line(writer, {"type": "error", "message": "missing session_id"})
            writer.close()
            return
        ok = self._kill_session(session_id)
        if ok:
            await self._send_line(writer, {"type": "killed", "session_id": session_id})
        else:
            await self._send_line(writer, {"type": "error", "message": "session not found"})
        writer.close()

    async def _cmd_rename(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        session_id = msg.get("session_id")
        label = msg.get("label")
        if not isinstance(session_id, str) or not isinstance(label, str) or not label.strip():
            await self._send_line(writer, {"type": "error", "message": "missing session_id or label"})
            writer.close()
            return
        label = label.strip()[:64]
        session = self._get_session(session_id)
        if session is None:
            await self._send_line(writer, {"type": "error", "message": "session not found"})
            writer.close()
            return
        session.label = label
        self._log.info("session renamed: %s → %s", session_id[:6], label)
        await self._send_line(writer, {"type": "renamed", "session_id": session_id, "label": label})
        writer.close()

    async def _cmd_shutdown(self, writer: asyncio.StreamWriter) -> None:
        """Graceful full shutdown requested by the dashboard.

        Kill every session through the normal PtySession.kill() path so the
        ConPTY children (PowerShells, anything running inside them) terminate
        instead of orphaning, ack the request, flush the socket, then stop the
        TCP server — start()'s serve_forever unblocks and the process exits.

        The PID file is intentionally left in place: no other exit path
        removes it (matching existing behavior), and the next manager start
        overwrites it. The dashboard-side fallback verifies the command line
        of whatever PID the file names before ever killing it, so a stale
        file is harmless.
        """
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                s.kill()
            except Exception:
                self._log.exception("session %s kill failed during shutdown", s.id[:6])
        self._log.info("shutdown RPC: killed %d session(s)", len(sessions))

        await self._send_line(
            writer,
            {"type": "shutdown", "ok": True, "killed_sessions": len(sessions)},
        )
        # Make sure the ack is fully written before tearing the server down —
        # wait_closed() drains the transport, so the exit is scheduled strictly
        # after the response bytes have left this process.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        if self._server is not None:
            # close() cancels the serve_forever future; start() then returns
            # and the process exits (PTY reader threads are daemons).
            self._server.close()

    async def _cmd_attach(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        msg: dict,
    ) -> None:
        session_id = msg.get("session_id")
        if not isinstance(session_id, str):
            await self._send_line(writer, {"type": "error", "message": "missing session_id"})
            writer.close()
            return

        session = self._get_session(session_id)
        if session is None or not session.is_alive():
            await self._send_line(writer, {"type": "error", "message": "session not found or dead"})
            writer.close()
            return

        # Takeover attach (the dual-purpose ↻ refresh): this device claims the
        # resize lock AND sizes the shared PTY to its own display. We resize
        # BEFORE add_subscriber() serializes the replay, so the replay — grid
        # AND scrollback — comes back already fit to THIS device's width. Open
        # on a phone after working on a wide PC, hit ↻, and history reflows to
        # the phone instead of running off the right edge. A single ConPTY has
        # one size, so this also reshapes the PC's live view (intended: the
        # last device to take over wins). See [[footguns#150]] /
        # [[decisions/2026-06-15_terminal-replay-screen-model]].
        takeover = bool(msg.get("takeover"))
        to_rows, to_cols = msg.get("rows"), msg.get("cols")
        takeover = takeover and isinstance(to_rows, int) and isinstance(to_cols, int)
        if takeover:
            session.resize(to_rows, to_cols)

        sub_id, queue, replay = session.add_subscriber()

        if takeover:
            # Hand the lock to this subscriber and tell the others they lost it.
            session._claimed_sub = sub_id
            session._broadcast_claim_state(you_sub=sub_id)

        # Send current claim state to the new subscriber so their lock button
        # shows the correct initial state (who owns the resize lock, if anyone).
        claim_payload: dict
        if session._claimed_sub:
            claim_payload = {"type": "claimed", "by": session._claimed_sub[:6]}
            if session._claimed_sub == sub_id:
                claim_payload["you"] = True
        else:
            claim_payload = {"type": "unclaimed"}
        try:
            writer.write(
                (json.dumps(claim_payload) + "\n").encode("utf-8")
            )
            await writer.drain()
        except Exception:
            session.remove_subscriber(sub_id)
            writer.close()
            return

        # Send the screen-state replay before live output, chunked so no single
        # JSON line exceeds a default 64KB StreamReader limit on the client —
        # the oversized-replay-line bug read as a fake "[session exited]" on
        # reattach ([[footguns#101]]). The client also raises its own limit, but
        # chunking at the source is what makes this robust for any replay size.
        #
        # `replay` is the SERIALIZED pyte screen (grid + scrollback + cursor),
        # not raw bytes — see serialize_screen() and add_subscriber(). This
        # supersedes the 2026-06-13 byte-faithful raw-ring replay, which was
        # correct for one device at one width but scattered once a second device
        # attached at a different width (the ring glued together output produced
        # at multiple widths; nothing can reflow that into one width). The grid
        # model is the authoritative screen at the CURRENT width, so its replay
        # is always one coherent width. See [[footguns#150]] and
        # [[synth/2026-06-15_terminal-replay-screen-model]].
        if replay:
            text = replay.decode("utf-8", errors="replace")
            try:
                for i in range(0, len(text), REPLAY_CHUNK_CHARS):
                    piece = text[i:i + REPLAY_CHUNK_CHARS]
                    writer.write(
                        (json.dumps({"type": "out", "data": piece}) + "\n").encode("utf-8")
                    )
                await writer.drain()
            except Exception:
                session.remove_subscriber(sub_id)
                writer.close()
                return

        # Start the output pump: drain session queue → TCP.
        async def pump_out() -> None:
            try:
                while True:
                    item = await queue.get()
                    if item is None:  # PTY exited — sentinel
                        try:
                            writer.write(
                                (json.dumps({"type": "exit"}) + "\n").encode("utf-8")
                            )
                            await writer.drain()
                        except Exception:
                            pass
                        return
                    # Tuple items are metadata (claim/unclaimed) — write the
                    # pre-encoded JSON line directly, no {"type":"out"} wrap.
                    if isinstance(item, tuple) and item[0] == "meta":
                        try:
                            writer.write(item[1])
                            await writer.drain()
                        except Exception:
                            return
                        continue
                    try:
                        text = item.decode("utf-8", errors="replace")
                        writer.write(
                            (json.dumps({"type": "out", "data": text}) + "\n").encode("utf-8")
                        )
                        await writer.drain()
                    except Exception:
                        return
            except asyncio.CancelledError:
                pass

        pump_task = asyncio.create_task(pump_out())

        try:
            # Input loop: read from TCP, forward to PTY.
            while True:
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=3600)
                except asyncio.TimeoutError:
                    # No input for an hour — send a keepalive-like check.
                    # Just continue; connection is still good.
                    if not session.is_alive():
                        break
                    continue
                if not raw:
                    break  # TCP closed
                try:
                    m = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                t = m.get("type")
                if t == "in":
                    data = m.get("data")
                    if isinstance(data, str):
                        session.write(data)
                elif t == "resize":
                    # Only honor resize from the claimed viewer. If nobody has
                    # claimed the session, the FIRST viewer to send a resize
                    # implicitly claims it (stops the multi-device resize war).
                    if session._claimed_sub is None:
                        session._claimed_sub = sub_id
                        session._broadcast_claim_state()
                    if session._claimed_sub != sub_id:
                        continue  # silently ignore — another viewer owns resize
                    rows = m.get("rows")
                    cols = m.get("cols")
                    if isinstance(rows, int) and isinstance(cols, int):
                        session.resize(rows, cols)
                elif t == "claim":
                    # Explicitly take over the resize lock.
                    session._claimed_sub = sub_id
                    session._broadcast_claim_state(you_sub=sub_id)
                elif t == "release":
                    # Release the lock if this viewer holds it.
                    if session._claimed_sub == sub_id:
                        session._claimed_sub = None
                        session._broadcast_claim_state()
                elif t == "pause":
                    # Output flow control from the dashboard: the browser's
                    # ACK window is full. Stop reading the ConPTY; input keeps
                    # flowing through this loop untouched (Ctrl-C stays instant).
                    session.pause_output(sub_id)
                elif t == "resume":
                    session.resume_output(sub_id)
                # Unknown types ignored — forward-compat.
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            session.remove_subscriber(sub_id)
            pump_task.cancel()
            with contextlib_nullcontext():
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                writer.close()
            except Exception:
                pass

    async def _send_line(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        try:
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass


# Python 3.10+ has contextlib.nullcontext; 3.9 doesn't. Provide a fallback.
try:
    from contextlib import nullcontext as contextlib_nullcontext
except ImportError:
    import contextlib as _contextlib

    @_contextlib.contextmanager
    def contextlib_nullcontext():
        yield


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Trident PTY Manager")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--workspace",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Trident repo root (default: directory containing this script)",
    )
    args = parser.parse_args()

    log = _setup_logging(args.workspace)
    log.info("pty-manager starting — port=%d workspace=%s", args.port, args.workspace)

    # Dump a stack trace on a fatal native signal (SIGSEGV/SIGABRT/SIGFPE/etc.)
    # before the process dies. This catches the crash class that bypasses
    # Python's own except-hook entirely — e.g. a ConPTY/pywinpty native fault,
    # the suspected cause of the 2026-06-24 silent deaths. faulthandler writes
    # to a DEDICATED file this process opens itself (not stderr), so a native
    # crash is captured on a plain manager respawn — it does NOT depend on the
    # spawner having redirected stderr (which needs a dashboard restart). The
    # handle is bound to a local that lives for the whole process (asyncio.run
    # blocks below), keeping the fd valid until the crash. Append mode preserves
    # prior dumps across respawns.
    try:
        fault_path = os.path.join(args.workspace, "logs", "pty_manager.faulthandler.log")
        _fault_log = open(fault_path, "a")
        faulthandler.enable(file=_fault_log, all_threads=True)
    except Exception:
        log.warning("faulthandler.enable() failed — native crashes won't dump")

    # Verify pywinpty is available before starting the server.
    try:
        from winpty import PtyProcess  # noqa: F401
    except ImportError:
        log.critical("pywinpty is not installed. Run: pip install pywinpty")
        sys.exit(1)

    manager = PtyManagerServer(port=args.port, workspace=args.workspace)

    try:
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        log.info("pty-manager shutting down (KeyboardInterrupt)")
    except Exception:
        log.exception("pty-manager crashed")
        raise


if __name__ == "__main__":
    main()
