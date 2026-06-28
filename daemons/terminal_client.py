"""
trident_terminal.py — TCP client for the PTY manager (trident_pty_manager.py).

Why this module changed
-----------------------
Before 2026-06-02, this module owned PtySession objects directly in the dashboard
process. That meant dashboard restart = all terminal sessions killed. Now the
sessions live in a standalone trident_pty_manager.py process that survives
dashboard restarts. This module is a thin TCP client with the same public API
(now async where it does I/O).

Public API
----------
- manager = TerminalSessionManager()        # module-level singleton
- await manager.create(cwd, label=None)     # → session info dict
- await manager.get(session_id)             # → RemotePtySession or None
- await manager.list()                      # → list of session info dicts
- await manager.kill(session_id)            # → bool
- await manager.restart_manager()           # stop the pty-manager process
                                            #   (kills ALL sessions; lazy
                                            #   respawn on next access)
- manager.shutdown()                        # no-op (sessions survive)

RemotePtySession
- await session.attach(loop, queue)         # → sub_id (str)
- session.detach(sub_id)                    # close TCP stream
- session.write(data: str)                  # send keystrokes (buffered)
- session.resize(rows, cols)               # set terminal size (buffered)
- session.pause_output()                    # flow control: pause ConPTY reads
- session.resume_output()                   # flow control: resume ConPTY reads
- session.is_alive()                        # → bool (cached)
- await session.refresh()                   # → dict (fresh info from manager)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Optional

from shared.proc import windowless_python

_LOG = logging.getLogger(__name__)

MANAGER_PORT = 58999
MANAGER_HOST = "127.0.0.1"
MANAGER_CONNECT_TIMEOUT = 5.0  # seconds for TCP handshake + response
MANAGER_SPAWN_TIMEOUT = 12.0   # seconds to wait for manager to start listening.
                               # A cold Python start (pywinpty + heavy imports)
                               # routinely needs >3s on this laptop; the old 3s
                               # gave up too early, so the create-retry respawned
                               # a SECOND manager that raced the first for the
                               # port and both lost (2026-06-24 self-heal failure).
                               # The wait is a poll loop — it returns the instant
                               # the port is up, so a healthy fast start pays nothing.
_MAX_HEAL_ATTEMPTS = 3         # respawn-and-retry budget for a manager that died
                               # mid-session (see TerminalSessionManager.create).
# Graceful-shutdown RPC budget. An OLD manager (predating the shutdown RPC)
# closes the connection without writing anything for unknown short-lived
# request types, so _send_recv returns None near-instantly on EOF — this
# timeout only gates the pathological case of a HUNG manager that accepts
# the connection but never answers.
MANAGER_SHUTDOWN_TIMEOUT = 3.0
MANAGER_EXIT_TIMEOUT = 5.0     # seconds to wait for the port to free after a stop


# ═══════════════════════════════════════════════════════════════════════════════
# Async TCP helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_recv(msg: dict, timeout: float = MANAGER_CONNECT_TIMEOUT) -> dict | None:
    """Open TCP to manager, send one JSON line, read one JSON line, close.
    Async — safe to call from FastAPI endpoint or event-loop handler."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(MANAGER_HOST, MANAGER_PORT),
            timeout=timeout,
        )
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        if line:
            return json.loads(line.decode("utf-8", errors="replace"))
        return None
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError,
            json.JSONDecodeError, UnicodeDecodeError, Exception) as e:
        _LOG.debug("_send_recv failed: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Manager lifecycle (async — lazy spawn on first access)
# ═══════════════════════════════════════════════════════════════════════════════

async def _manager_port_open() -> bool:
    """Check if the pty-manager is already listening."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(MANAGER_HOST, MANAGER_PORT),
            timeout=0.5,
        )
        writer.close()
        return True
    except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
        return False


async def _spawn_manager(workspace: str) -> None:
    """Spawn trident_pty_manager.py as a detached subprocess.
    Waits asynchronously until the port is listening or timeout expires."""
    manager_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pty_manager.py")
    if not os.path.isfile(manager_script):
        _LOG.warning("pty-manager script not found at %s", manager_script)
        return

    _LOG.info("spawning pty-manager: %s --port %d --workspace %s",
              manager_script, MANAGER_PORT, workspace)

    # DETACHED_PROCESS (0x00000008): don't inherit our console (survives dashboard
    # exit). The actual no-window guarantee comes from launching pythonw.exe below
    # — DETACHED_PROCESS alone still flashes python.exe's console in some setups.
    creationflags = 0x00000008  # DETACHED_PROCESS
    if sys.platform == "win32":
        creationflags |= 0x00000200  # CREATE_NEW_PROCESS_GROUP

    # pythonw.exe (GUI subsystem) so the manager never shows a console window.
    pyexe = windowless_python()

    # Use the synchronous subprocess.Popen here because asyncio.create_subprocess_exec
    # on Windows can interact poorly with DETACHED_PROCESS. Popen returns instantly
    # (it just launches the process), so it doesn't block the event loop meaningfully.
    import subprocess
    # The manager's stderr is captured to a file (not DEVNULL): a native
    # ConPTY/pywinpty crash, or any traceback that escapes the manager's own
    # logger, lands here ([[footguns#188]]). faulthandler dumps native-signal
    # stacks to this fd too. The re-parenting launcher opens it for the target.
    stderr_path = os.path.join(workspace, "logs", "pty_manager.stderr.log")
    os.makedirs(os.path.dirname(stderr_path), exist_ok=True)

    manager_cmd = [pyexe, manager_script,
                   "--port", str(MANAGER_PORT), "--workspace", workspace]

    # RE-PARENT the manager out of our process tree (harden Part 2,
    # [[footguns#192]]). DETACHED_PROCESS does NOT reparent on Windows — the
    # spawner stays the parent, so a taskkill /T up the chain (station_watchdog
    # killing the dashboard, the old #191 agent-manager /T) cascades in and kills
    # the manager + every terminal. We instead spawn through scripts/spawn_detached.py,
    # which launches the manager detached and exits immediately — orphaning the
    # manager so no ancestor tree-kill can reach it. Fallback: if the launcher is
    # somehow absent, spawn directly (loses the orphan benefit but terminals still
    # work — self-heal [[footguns#192]] covers a death either way).
    launcher = os.path.join(workspace, "scripts", "spawn_detached.py")
    if os.path.isfile(launcher):
        spawn_cmd = [pyexe, launcher, "--stderr", stderr_path, "--", *manager_cmd]
        _LOG.info("spawning pty-manager via re-parenting launcher (detached from our tree)")
        subprocess.Popen(
            spawn_cmd,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=workspace,
        )
    else:
        _LOG.warning("re-parenting launcher missing at %s — spawning manager "
                     "directly (it will be a child of this process)", launcher)
        stderr_f = open(stderr_path, "ab")
        try:
            subprocess.Popen(
                manager_cmd,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=stderr_f,
                stdin=subprocess.DEVNULL,
                cwd=workspace,
            )
        finally:
            # The child inherited its own dup of the fd; the parent's copy is
            # no longer needed.
            stderr_f.close()

    # Wait asynchronously for the port to become available.
    deadline = asyncio.get_running_loop().time() + MANAGER_SPAWN_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(MANAGER_HOST, MANAGER_PORT),
                timeout=0.3,
            )
            writer.close()
            _LOG.info("pty-manager is ready on port %d", MANAGER_PORT)
            return
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.1)
    _LOG.warning("pty-manager did not become ready within %.1fs", MANAGER_SPAWN_TIMEOUT)


async def _ensure_manager(workspace: str) -> None:
    """Make sure the pty-manager is running, spawning it if necessary."""
    if not await _manager_port_open():
        await _spawn_manager(workspace)


# ═══════════════════════════════════════════════════════════════════════════════
# Manager restart helpers (graceful-shutdown RPC + verified PID-kill fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _manager_pid_path(workspace: str) -> str:
    """Path of the PID file the pty-manager writes on startup."""
    return os.path.join(workspace, "logs", "pty_manager.pid")


def _read_manager_pid(workspace: str) -> int | None:
    """Read logs/pty_manager.pid. Returns None if missing or unparseable."""
    try:
        with open(_manager_pid_path(workspace), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pid_commandline(pid: int) -> str | None:
    """Best-effort command line of a live process, without psutil.

    Used to verify that the PID named by logs/pty_manager.pid is actually the
    pty-manager before force-killing it — PIDs get recycled, and blindly
    killing whatever holds a stale PID is how you take out an unrelated
    process. Returns None when the lookup fails (caller must treat that as
    "do NOT kill"). Blocking — call via asyncio.to_thread.
    """
    import subprocess
    if sys.platform == "win32":
        # Get-CimInstance is the supported query path on Windows 11 (wmic is
        # removed on recent builds). `pid` is an int, so no injection surface.
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            return out or None
        except Exception as e:
            _LOG.warning("command-line lookup for PID %d failed: %s", pid, e)
            return None
    try:
        raw = open(f"/proc/{int(pid)}/cmdline", "rb").read()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _kill_process_tree(pid: int) -> tuple[bool, str]:
    """Force-kill a process AND its children.

    Tree kill (`taskkill /T`) matters here: the pty-manager's ConPTY children
    (the PowerShells behind each terminal tab) are its direct descendants —
    killing only the manager would orphan them. Blocking — call via
    asyncio.to_thread. Returns (ok, detail).
    """
    import subprocess
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                return True, (proc.stdout or "").strip()
            return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        except Exception as e:
            return False, str(e)
    try:
        import signal as _signal
        os.kill(int(pid), _signal.SIGKILL)
        return True, "SIGKILL sent"
    except OSError as e:
        return False, str(e)


async def _wait_manager_port_closed(timeout: float) -> bool:
    """Poll until the manager port stops accepting connections. True = closed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not await _manager_port_open():
            return True
        await asyncio.sleep(0.1)
    return not await _manager_port_open()


# ═══════════════════════════════════════════════════════════════════════════════
# RemotePtySession — a session that lives in the pty-manager process
# ═══════════════════════════════════════════════════════════════════════════════

class RemotePtySession:
    """Wraps a session that lives in the remote pty-manager. Manages a single
    long-lived TCP connection for attach/detach (used by the WS handler).
    Short-lived RPCs (write, resize) are sent over the active stream."""

    def __init__(self, info: dict) -> None:
        self._info = info
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump_task: asyncio.Task | None = None
        # Lock not needed — reader/writer are only touched from the event loop
        # (attach and detach are called from the WS handler which is async).

    @property
    def id(self) -> str:
        return self._info["id"]

    def is_alive(self) -> bool:
        """Check cached liveness. Fast — no network call."""
        return bool(self._info.get("alive"))

    async def refresh(self) -> dict:
        """Fetch fresh session info from the manager. Returns updated info dict."""
        resp = await _send_recv({"type": "list"})
        if resp and resp.get("type") == "sessions":
            for s in resp["sessions"]:
                if s["id"] == self._info["id"]:
                    self._info = s
                    return dict(s)
        self._info["alive"] = False
        return dict(self._info)

    # ── long-lived stream (attach / detach) ─────────────────────────────────

    async def attach(
        self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue,
        *, takeover: bool = False, rows: int | None = None, cols: int | None = None,
    ) -> str:
        """Open a TCP stream to the manager for this session. Returns a sub_id
        string for detach(). Pushes decoded bytes to `queue` for output,
        and `None` as a sentinel when the stream ends.

        When `takeover` is set with valid rows/cols, the attach asks the manager
        to claim the resize lock for this connection and size the PTY to these
        dimensions before the replay is serialized — the dual-purpose ↻ refresh
        (a device taking control + reflowing history to its own width)."""
        # limit=2MB — a single JSON line must never exceed this. The screen-
        # state replay (serialized pyte grid in trident_pty_manager.py) arrives
        # CHUNKED at REPLAY_CHUNK_CHARS (8192) chars per line, so the limit only
        # has to cover one chunk's worst-case escape inflation, not the whole
        # replay. Python's default StreamReader limit is 64KB; exceeding it
        # causes readline() to raise ValueError, which the pump treats as a
        # fatal error and pushes the exit sentinel.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(MANAGER_HOST, MANAGER_PORT, limit=2 * 1024 * 1024),
            timeout=MANAGER_CONNECT_TIMEOUT,
        )
        attach_msg: dict = {"type": "attach", "session_id": self._info["id"]}
        if takeover and isinstance(rows, int) and isinstance(cols, int):
            attach_msg["takeover"] = True
            attach_msg["rows"] = rows
            attach_msg["cols"] = cols
        writer.write((json.dumps(attach_msg) + "\n").encode("utf-8"))
        await writer.drain()

        self._reader = reader
        self._writer = writer
        sub_id = f"remote-{self._info['id'][:12]}"

        # Pump: read JSON-lines from manager TCP → push to caller's queue.
        #
        # Backpressure note (Phase 2 flow control): output items go through
        # `await queue.put(...)`, NOT put_nowait. This pump is an asyncio task
        # on the same loop as the consumer (the WS handler in routes_ws.py),
        # so awaiting put is both safe and load-bearing: when the WS handler
        # gates its sends on the browser's ACK window and the queue fills,
        # this pump blocks — which stops reading the manager TCP socket —
        # which fills the TCP window — which blocks the manager's drain().
        # The old put_nowait silently DROPPED chunks on QueueFull instead.
        # (`loop` is kept in the signature for API compat; threads used to
        # need call_soon_threadsafe here, the coroutine no longer does.)
        async def pump() -> None:
            cancelled = False
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line.decode("utf-8", errors="replace"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    t = msg.get("type")
                    if t == "out":
                        data = msg.get("data", "")
                        if isinstance(data, str):
                            await queue.put(data.encode("utf-8", errors="replace"))
                    elif t == "exit":
                        await queue.put(None)
                        return
                    elif t in ("claimed", "unclaimed"):
                        # Forward claim-state broadcasts to the browser so the
                        # frontend can show who holds the resize lock. Use the
                        # tuple wrapper so routes_ws.py sends it as a raw JSON
                        # line — no {"type":"out"} envelope.
                        blob = (json.dumps(msg) + "\n").encode("utf-8")
                        await queue.put(("meta", blob))
            except asyncio.CancelledError:
                cancelled = True
            except (ConnectionResetError, OSError):
                pass
            except Exception:
                _LOG.debug("pump error for session %s", self._info["id"][:6])
            finally:
                # Only push the exit sentinel when the PTY actually died
                # (t=="exit" already returned above, so we reach here only
                # on reader EOF, connection loss, or CancelledError). On
                # CancelledError the caller is intentionally detaching and
                # the PTY is still alive — skip the sentinel. Awaited put so
                # the sentinel can't be lost to a momentarily-full queue; if
                # detach cancels us mid-put, the consumer is going away too.
                if not cancelled:
                    try:
                        await queue.put(None)
                    except (RuntimeError, asyncio.CancelledError):
                        pass

        self._pump_task = asyncio.create_task(pump())
        return sub_id

    def detach(self, sub_id: str) -> None:
        """Close the TCP stream and cancel the pump task."""
        pump = self._pump_task
        writer = self._writer
        self._reader = None
        self._writer = None
        self._pump_task = None

        if pump:
            pump.cancel()
        if writer:
            try:
                writer.close()
            except Exception:
                pass

    # ── commands (buffered writes to the active TCP stream) ──────────────────

    def _send_msg(self, msg: dict) -> None:
        """Write an arbitrary JSON message to the pty-manager TCP stream."""
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        except Exception:
            pass

    def write(self, data: str) -> None:
        """Send keystrokes to the PTY. Buffered — no drain needed for small
        terminal input. Must be called from an async context."""
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(
                (json.dumps({"type": "in", "data": data}) + "\n").encode("utf-8")
            )
        except Exception:
            pass

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY. Buffered. Must be called from an async context."""
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(
                (json.dumps({"type": "resize", "rows": rows, "cols": cols}) + "\n")
                .encode("utf-8")
            )
        except Exception:
            pass

    def pause_output(self) -> None:
        """Flow control: ask the pty-manager to stop reading the ConPTY for
        this subscriber (browser ACK window full). A pre-flow-control manager
        ignores unknown attach-stream message types, so this is a safe no-op
        against an old manager process."""
        self._send_msg({"type": "pause"})

    def resume_output(self) -> None:
        """Flow control: ACK window drained — resume ConPTY reads."""
        self._send_msg({"type": "resume"})


# ═══════════════════════════════════════════════════════════════════════════════
# Session manager (module-level singleton)
# ═══════════════════════════════════════════════════════════════════════════════

class TerminalSessionManager:
    """Registry of terminal sessions. Public API is async for I/O methods.
    Internally talks to the pty-manager over TCP.

    Sessions survive the dashboard process. shutdown() is intentionally a no-op.
    """

    def __init__(self) -> None:
        self._workspace: str | None = None
        # Track whether we've already tried to ensure the manager is running on
        # this dashboard instance. Avoids re-spawning when list/get both trigger.
        self._ensured = False
        # Serializes ensure-spawn against restart_manager. Without it, a
        # terminal API call racing a restart can observe the dying manager's
        # port still open and re-latch _ensured=True with no manager behind
        # it — lazy respawn then never happens until the dashboard restarts.
        self._ensure_lock = asyncio.Lock()

    def _resolve_workspace(self) -> str:
        if self._workspace is None:
            # The workspace root is the repo root — the parent of the daemons/
            # dir this client module lives in. logs/ and the re-parenting
            # launcher (<workspace>/scripts/spawn_detached.py) hang off it.
            self._workspace = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
        return self._workspace

    async def _ensure_once(self) -> None:
        """Ensure the manager is running. Only spawns once per dashboard session.

        Double-checked under the lock: the fast path costs nothing once
        latched, and the locked path can't interleave with restart_manager()
        (which holds the same lock for its full duration) or with a second
        concurrent spawn attempt."""
        if self._ensured:
            return
        async with self._ensure_lock:
            if not self._ensured:
                await _ensure_manager(self._resolve_workspace())
                self._ensured = True

    # ── public API ──────────────────────────────────────────────────────────

    async def create(self, cwd: str, label: Optional[str] = None) -> dict:
        """Spawn a fresh PowerShell PTY via the pty-manager. Returns session info.

        Self-heals a pty-manager that died MID-SESSION. The `_ensure_once` latch
        means "spawned once this dashboard life" — so once it's set, a manager
        that later crashed or was killed is never respawned, and every create
        503s until the operator MANUALLY restarts the terminal manager. That was
        the 2026-06-24 "I have to restart it myself every few minutes" pain.

        Fix: on an UNREACHABLE create (``_send_recv`` -> None, the process is
        gone), drop the stale latch and re-ensure (which respawns a dead manager
        — `_ensure_manager` no-ops if the port is actually alive, so a transient
        TCP blip doesn't needlessly respawn), then retry, bounded. A create the
        manager actively REFUSES (an answer that isn't ``created``) is a real
        error, not a dead process — surfaced immediately, no respawn loop.
        """
        workspace = cwd or self._resolve_workspace()
        payload: dict = {"type": "create", "cwd": workspace}
        if label:
            payload["label"] = label.strip()[:64]

        for attempt in range(1, _MAX_HEAL_ATTEMPTS + 1):
            await self._ensure_once()
            resp = await _send_recv(payload)
            if resp and resp.get("type") == "created":
                return resp["session"]
            if resp is not None:
                # Manager answered but didn't create — a genuine refusal a
                # respawn can't fix. Surface it.
                raise RuntimeError(resp.get("message", "pty-manager create failed"))
            # resp is None -> the manager is gone. The latch is stale; drop it so
            # the next _ensure_once respawns, then retry.
            if attempt < _MAX_HEAL_ATTEMPTS:
                _LOG.warning(
                    "pty-manager unreachable on create — self-healing "
                    "(respawn attempt %d/%d)", attempt, _MAX_HEAL_ATTEMPTS)
                self._ensured = False
        raise RuntimeError("pty-manager unreachable")

    async def get(self, session_id: str) -> Optional[RemotePtySession]:
        """Return a RemotePtySession for the given id, or None."""
        await self._ensure_once()
        sessions = await self._list_sessions()
        for s in sessions:
            if s["id"] == session_id and s.get("alive"):
                return RemotePtySession(s)
        return None

    async def list(self) -> list[dict]:
        """Return metadata for all alive sessions."""
        await self._ensure_once()
        return await self._list_sessions()

    async def kill(self, session_id: str) -> bool:
        """Kill a session by id. Returns True if the session was found and killed."""
        resp = await _send_recv({"type": "kill", "session_id": session_id})
        return resp is not None and resp.get("type") == "killed"

    async def rename(self, session_id: str, label: str) -> bool:
        """Rename a session by id. Returns True if the session was found and renamed."""
        resp = await _send_recv({"type": "rename", "session_id": session_id, "label": label})
        return resp is not None and resp.get("type") == "renamed"

    async def restart_manager(self) -> dict:
        """Stop the pty-manager process. Kills EVERY terminal session.

        Two paths, tried in order:
          1. ``rpc`` — the graceful ``{"type":"shutdown"}`` request: the
             manager kills each session through PtySession.kill() (ConPTY
             children terminate, no orphans), acks, and exits.
          2. ``pid-kill`` — fallback for an old manager binary that predates
             the RPC (it closes the connection without responding, so the RPC
             attempt fails fast on EOF) or a hung manager (RPC times out).
             Reads logs/pty_manager.pid, VERIFIES the process command line
             references trident_pty_manager.py (PIDs get recycled — never
             kill an arbitrary process), then taskkills the process TREE so
             the ConPTY children die with it.

        Deliberately does NOT respawn the manager: lazy respawn on next
        terminal access is the established design
        (decisions/2026-06-02_persistent-terminal-sessions). The spawn-once
        latch is reset up front (under the ensure lock) so the next API call
        re-detects and respawns cleanly.

        Returns {"ok": bool, "method": "rpc"|"pid-kill"|"not-running",
        "detail": str}.
        """
        workspace = self._resolve_workspace()

        def _result(ok: bool, method: str, detail: str) -> dict:
            _LOG.info("restart_manager: ok=%s method=%s detail=%s", ok, method, detail)
            return {"ok": ok, "method": method, "detail": detail}

        # Hold the ensure lock for the whole restart so no terminal API call
        # can probe the dying manager's port mid-shutdown and re-latch
        # _ensured against a process that's about to exit. Concurrent calls
        # block here, then re-ensure (and lazily respawn) once we're done.
        async with self._ensure_lock:
            # Invalidate up front — even on a failed restart, leaving the
            # latch False is safe (the next call just re-checks the port).
            self._ensured = False

            if not await _manager_port_open():
                return _result(True, "not-running",
                               "pty-manager is not running — nothing to stop")

            # ── path 1: graceful shutdown RPC ─────────────────────────────
            resp = await _send_recv({"type": "shutdown"}, timeout=MANAGER_SHUTDOWN_TIMEOUT)
            if resp is not None and resp.get("type") == "shutdown" and resp.get("ok"):
                closed = await _wait_manager_port_closed(MANAGER_EXIT_TIMEOUT)
                n = resp.get("killed_sessions", "?")
                detail = f"manager shut down gracefully ({n} session(s) killed)"
                if not closed:
                    detail += f" — warning: port {MANAGER_PORT} still open after ack"
                return _result(True, "rpc", detail)

            # ── path 2: verified PID-kill fallback ────────────────────────
            pid = await asyncio.to_thread(_read_manager_pid, workspace)
            if pid is None:
                return _result(False, "pid-kill",
                               "shutdown RPC failed and logs/pty_manager.pid is "
                               "missing or unreadable — kill the pty-manager "
                               "process manually")
            cmdline = await asyncio.to_thread(_pid_commandline, pid)
            if not cmdline or "pty_manager.py" not in cmdline:
                return _result(False, "pid-kill",
                               f"refusing to kill PID {pid}: its command line "
                               f"does not reference pty_manager.py "
                               f"(stale PID file / recycled PID)")
            killed, kill_detail = await asyncio.to_thread(_kill_process_tree, pid)
            if not killed:
                return _result(False, "pid-kill",
                               f"taskkill on PID {pid} failed: {kill_detail}")
            closed = await _wait_manager_port_closed(MANAGER_EXIT_TIMEOUT)
            detail = f"manager PID {pid} force-killed (process tree)"
            if not closed:
                detail += f" — warning: port {MANAGER_PORT} still open after kill"
            return _result(True, "pid-kill", detail)

    def shutdown(self) -> None:
        """No-op. Sessions are designed to survive dashboard exit.
        The pty-manager process keeps running; only explicit ×-button clicks
        kill sessions."""
        _LOG.info("terminal manager shutdown: sessions preserved (no-op)")

    # ── internal ────────────────────────────────────────────────────────────

    async def _list_sessions(self) -> list[dict]:
        resp = await _send_recv({"type": "list"})
        if resp and resp.get("type") == "sessions":
            return resp.get("sessions", [])
        return []


# Module-level singleton — shared across all FastAPI requests.
manager = TerminalSessionManager()
