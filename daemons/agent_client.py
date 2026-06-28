"""
trident_agent_client.py — TCP client for the agent manager (trident_agent_manager.py).

Why this module exists
----------------------
The agent runner's processes live in a standalone trident_agent_manager.py
process that survives dashboard restarts (issue #26, parent #24). This module is
the thin async TCP client the dashboard uses to talk to it — the agent-side
sibling of trident_terminal.py, which plays the same role for the pty-manager.

This slice's public surface matches the manager's RPC:
- manager = AgentSessionManager()        # module-level singleton
- await manager.ping()                    # → bool (manager reachable)
- await manager.list_agents()             # → list[dict] (agent definitions)
- await manager.get_agent(id)             # → dict | None
- await manager.upsert_agent(defn)        # → dict (persisted) | raises ValueError
- await manager.delete_agent(id)          # → bool (True if it existed)
- await manager.set_enabled(id, enabled)  # → dict | None (toggle, #39)
- await manager.restart_manager()         # stop the agent-manager process
                                          #   (lazy respawn on next access)

The lifecycle mechanics (lazy spawn-on-first-access via _ensure_once, the
detached DETACHED_PROCESS spawn, the graceful-shutdown RPC with a verified
PID-kill fallback) mirror trident_terminal.py line-for-line; only the port,
script name, and RPC verbs differ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from shared.proc import windowless_python

_LOG = logging.getLogger(__name__)

# CREATE_NO_WINDOW — the inspect/kill helpers below shell out to the console apps
# powershell.exe and taskkill.exe. Spawned from the windowless dashboard process
# they'd each flash a console window; this suppresses it. Windows-only flag (the
# repo idiom is the bare literal — see trident_github_gateway._NO_WINDOW); only
# applied under sys.platform == "win32".
_NO_WINDOW = 0x08000000


class AgentDisabledError(ValueError):
    """Run-now was refused because the agent is DISABLED (OFF) — a hard stop
    (issue #40). Subclasses ValueError so a caller that only catches ValueError
    still treats it as a client-side rejection; the dashboard route catches it
    FIRST to return a 409 (distinct from the unknown-agent 404)."""


# Dedicated port — DISTINCT from trident_terminal.MANAGER_PORT (58999) so the
# agent-manager and pty-manager daemons coexist on this one production host.
MANAGER_PORT = 58998
MANAGER_HOST = "127.0.0.1"
MANAGER_CONNECT_TIMEOUT = 5.0  # seconds for TCP handshake + response
# StreamReader buffer ceiling for a SINGLE response line. asyncio.open_connection
# defaults this to 64KB (2**16); a single line longer than the limit makes
# reader.readline() raise ValueError ("chunk is longer than limit"). The manager
# frames every reply as one JSON line, and list_runs over many fat land-runs (each
# carrying ~20KB of stage output) crossed 64KB — readline() raised, _send_recv
# swallowed it as None, and the dashboard read a healthy manager as "unreachable",
# emptying every agent panel. The list payload is now trimmed server-side
# (_cmd_list_runs drops activity[].log), but we ALSO lift the ceiling to 16 MiB so
# a large-but-legitimate frame is never silently dropped again. Bounded (not
# unlimited) so a runaway/corrupt frame can't balloon dashboard memory.
MANAGER_READ_LIMIT = 16 * 1024 * 1024  # 16 MiB
MANAGER_SPAWN_TIMEOUT = 3.0    # seconds to wait for manager to start listening
# Graceful-shutdown RPC budget. An OLD manager (predating the shutdown RPC)
# closes the connection without writing anything for unknown short-lived
# request types, so _send_recv returns None near-instantly on EOF — this
# timeout only gates the pathological case of a HUNG manager that accepts
# the connection but never answers.
# The consolidated snapshot RPC (#149) gathers runs + 5 status sections in one
# lock-held call. On a cache MISS its two gh-backed sections (pill_counts +
# loop_status) re-shell to GitHub and the whole call runs ~4.8s — right at the 5s
# default, so it intermittently 503'd and blanked the panel. StatusAggregator now
# memoizes those sections (~10s TTL) so most polls are ~0.3s cache hits, but the
# periodic cache-miss recompute still needs headroom over 5s. Only this one RPC
# gets the longer budget; every other RPC keeps the tight 5s so a genuinely-down
# manager still surfaces fast.
MANAGER_SNAPSHOT_TIMEOUT = 15.0
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
            asyncio.open_connection(MANAGER_HOST, MANAGER_PORT,
                                    limit=MANAGER_READ_LIMIT),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError, Exception) as e:
        # Connect never succeeded — no writer to close. Expected during the
        # manager's lazy spawn/restart blip, so this stays at debug.
        _LOG.debug("_send_recv connect failed: %s", e)
        return None
    try:
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if line:
            return json.loads(line.decode("utf-8", errors="replace"))
        return None
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError,
            json.JSONDecodeError, UnicodeDecodeError, ValueError, Exception) as e:
        # The connection SUCCEEDED but the exchange failed — a read timeout against
        # a hung manager, an over-limit/garbled frame (ValueError from readline or
        # json), etc. Distinct from a downed manager (the connect path above), and
        # historically the silent failure mode: an over-64KB list_runs reply raised
        # here, got swallowed as None, and the dashboard mislabeled a healthy
        # manager "unreachable". Log at WARNING so the next such case is visible
        # instead of invisible.
        _LOG.warning("_send_recv exchange failed for %r: %s",
                     msg.get("type"), e)
        return None
    finally:
        # Close on EVERY exit path. The close used to sit INSIDE the try AFTER the
        # readline, so a read-timeout against a HUNG manager jumped straight to the
        # except and leaked the socket — one fd per call (#121, L6). try/finally
        # guarantees the writer is closed even on the timeout path.
        try:
            writer.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Manager lifecycle (async — lazy spawn on first access)
# ═══════════════════════════════════════════════════════════════════════════════

async def _manager_port_open() -> bool:
    """Check if the agent-manager is already listening."""
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
    """Spawn trident_agent_manager.py as a detached subprocess.
    Waits asynchronously until the port is listening or timeout expires."""
    manager_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "agent_manager.py")
    if not os.path.isfile(manager_script):
        _LOG.warning("agent-manager script not found at %s", manager_script)
        return

    _LOG.info("spawning agent-manager: %s --port %d --workspace %s",
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
    # Capture the manager's stderr to a file instead of discarding it (was
    # DEVNULL). A native crash, or any traceback that escapes the manager's own
    # logger, lands here — the pty-manager's sibling crash on 2026-06-24 left
    # zero diagnostics for exactly this reason. faulthandler in the manager
    # dumps native-signal stacks to this fd too.
    stderr_path = os.path.join(workspace, "logs", "agent_manager.stderr.log")
    os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
    stderr_f = open(stderr_path, "ab")
    try:
        subprocess.Popen(
            [pyexe, manager_script,
             "--port", str(MANAGER_PORT),
             "--workspace", workspace],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f,
            stdin=subprocess.DEVNULL,
            cwd=workspace,
        )
    finally:
        # The child inherited its own dup of the fd; this handle is no longer
        # needed in the parent.
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
            _LOG.info("agent-manager is ready on port %d", MANAGER_PORT)
            return
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.1)
    _LOG.warning("agent-manager did not become ready within %.1fs", MANAGER_SPAWN_TIMEOUT)


async def _ensure_manager(workspace: str) -> None:
    """Make sure the agent-manager is running, spawning it if necessary."""
    if not await _manager_port_open():
        await _spawn_manager(workspace)


# ═══════════════════════════════════════════════════════════════════════════════
# Manager restart helpers (graceful-shutdown RPC + verified PID-kill fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _manager_pid_path(workspace: str) -> str:
    """Path of the PID file the agent-manager writes on startup."""
    return os.path.join(workspace, "logs", "agent_manager.pid")


def _read_manager_pid(workspace: str) -> int | None:
    """Read logs/agent_manager.pid. Returns None if missing or unparseable."""
    try:
        with open(_manager_pid_path(workspace), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pid_commandline(pid: int) -> str | None:
    """Best-effort command line of a live process, without psutil.

    Used to verify that the PID named by logs/agent_manager.pid is actually the
    agent-manager before force-killing it — PIDs get recycled, and blindly
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
                creationflags=_NO_WINDOW,
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


def _force_kill_pid(pid: int) -> tuple[bool, str]:
    """Force-kill a SINGLE process by PID — deliberately NOT its process tree.

    This kills ONLY the agent-manager brain. It must NOT be a tree kill
    (`taskkill /T`): the agent-manager's descendants are the pty-manager (when
    the loop's self-heal respawned it — [[footguns#189]]) and, through that,
    every live agent terminal session. A `/T` here cascades into them and takes
    the terminals down — which is EXACTLY what the whole design promises will
    *survive* a manager restart ("restarting the manager never kills a running
    agent", top of this module). That cascade is the 2026-06-24 "restart the
    agent manager → all terminals 503" bug. So we kill only the brain; the
    orphaned pty-manager keeps hosting its terminals, and the next agent access
    reconnects to the already-running pty-manager. Blocking — call via
    asyncio.to_thread. Returns (ok, detail).
    """
    import subprocess
    if sys.platform == "win32":
        try:
            # NO /T — single process only. See the docstring: a tree kill here
            # is the terminal-cascade bug. Guard test: test_restart_kill_is_not_a_tree_kill.
            proc = subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/F"],
                capture_output=True, text=True, timeout=15,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode == 0:
                return True, (proc.stdout or "").strip()
            return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        except Exception as e:
            return False, str(e)
    try:
        # POSIX: os.kill targets the single PID, not the process group — the
        # intended single-process semantics (no killpg).
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
# Session manager (module-level singleton)
# ═══════════════════════════════════════════════════════════════════════════════

class AgentSessionManager:
    """Registry-facing client for the agent runner. Public API is async for I/O
    methods. Internally talks to the agent-manager over TCP.

    Agents survive the dashboard process. There is no shutdown() that kills
    them — only an explicit restart_manager() stops the daemon.
    """

    def __init__(self) -> None:
        self._workspace: str | None = None
        # Track whether we've already tried to ensure the manager is running on
        # this dashboard instance. Avoids re-spawning when ping/list both trigger.
        self._ensured = False
        # Serializes ensure-spawn against restart_manager. Without it, an agent
        # API call racing a restart can observe the dying manager's port still
        # open and re-latch _ensured=True with no manager behind it — lazy
        # respawn then never happens until the dashboard restarts.
        self._ensure_lock = asyncio.Lock()

    def _resolve_workspace(self) -> str:
        if self._workspace is None:
            # The data/workspace root is the repo root — the parent of the
            # daemons/ dir this client module lives in. The manager stores
            # agents/, agent_runs/, logs/, watch_gate.json etc. under it, and
            # the re-parenting launcher lives at <workspace>/scripts/.
            self._workspace = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
        return self._workspace

    async def _ensure_once(self) -> None:
        """Ensure the manager is running, respawning it if it died.

        The name is historical — "once" originally meant one spawn per dashboard
        session.  But the agent-manager can deliberately exit (stale-pre-launch
        recycle, auto-recycle, crash), and the dashboard must bring it back.
        So the fast path checks liveness first: a dead manager resets the latch
        and re-spawns.

        Double-checked under the lock: the fast path is a cheap port check
        (<1ms when alive), and the locked path can't interleave with
        restart_manager() (which holds the same lock for its full duration) or
        with a second concurrent spawn attempt."""
        if self._ensured and await _manager_port_open():
            return  # manager is alive — fast path
        # Manager is dead or never spawned.
        async with self._ensure_lock:
            if self._ensured and await _manager_port_open():
                return  # another caller already resurrected it
            self._ensured = False  # force re-spawn
            await _ensure_manager(self._resolve_workspace())
            self._ensured = True

    # ── public API ──────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Lazy-spawn the manager if needed, then ping it. True = reachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "ping"})
        return resp is not None and resp.get("type") == "pong"

    async def list_agents(self) -> list[dict]:
        """Return all agent definitions from the store."""
        await self._ensure_once()
        resp = await _send_recv({"type": "list_agents"})
        if resp and resp.get("type") == "agents":
            return resp.get("agents", [])
        return []

    async def get_agent(self, agent_id: str) -> dict | None:
        """Return one agent definition, or None if missing / manager unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "get_agent", "id": agent_id})
        if resp and resp.get("type") == "agent":
            return resp.get("agent")
        return None

    async def upsert_agent(self, defn: dict) -> dict:
        """Validate + persist an agent definition (create or update).

        Returns the normalized definition the manager wrote. Raises ValueError
        with the manager's reason on a validation failure, or RuntimeError if
        the manager is unreachable.
        """
        await self._ensure_once()
        resp = await _send_recv({"type": "upsert_agent", "agent": defn})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "invalid agent definition")
        if resp.get("type") == "agent":
            return resp.get("agent")
        raise RuntimeError(f"unexpected response from agent-manager: {resp!r}")

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent definition. True if it existed, False otherwise."""
        await self._ensure_once()
        resp = await _send_recv({"type": "delete_agent", "id": agent_id})
        if resp and resp.get("type") == "deleted":
            return bool(resp.get("deleted"))
        return False

    async def set_enabled(self, agent_id: str, enabled: bool) -> dict | None:
        """Flip an agent's ``enabled`` flag (issue #39).

        Cheap one-field toggle that avoids a full upsert just to flip a boolean.
        Returns the updated normalized definition, or ``None`` if the agent does
        not exist (the manager replied with an error → the route maps that to a
        404). Raises RuntimeError if the manager is unreachable (a null reply) —
        mirroring upsert_agent/run_now so the route surfaces a 503. A downed
        manager must never masquerade as 'agent not found'.
        """
        await self._ensure_once()
        resp = await _send_recv(
            {"type": "set_enabled", "id": agent_id, "enabled": bool(enabled)}
        )
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "agent":
            return resp.get("agent")
        return None

    # ── master watch-gate (the dashboard ARM toggle) ──────────────────────────

    async def get_watch_gate(self) -> dict:
        """The master watch-gate arm state: ``{enabled, env_override, effective}``.
        Raises RuntimeError if the manager is unreachable (so the route 503s rather
        than reporting a misleading 'disarmed')."""
        await self._ensure_once()
        resp = await _send_recv({"type": "get_watch_gate"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("watch_gate") or {}

    async def set_watch_gate(self, enabled: bool) -> dict:
        """Arm/disarm the autonomous watch loop. Returns the new arm status.
        Raises RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv(
            {"type": "set_watch_gate", "enabled": bool(enabled)})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "set_watch_gate failed")
        return resp.get("watch_gate") or {}

    # ── supervised-mode toggle (the 🟡 PM-gate switch, #66) ────────────────────

    async def get_staleness(self) -> dict:
        """The manager staleness snapshot (#182): ``{loaded_sha, repo_head, stale}``.
        Raises RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "get_staleness"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("staleness") or {}

    async def get_supervised(self) -> dict:
        """The per-project supervised-mode state: ``{enabled, effective}``.
        Raises RuntimeError if the manager is unreachable (so the route 503s
        rather than reporting a misleading 'off')."""
        await self._ensure_once()
        resp = await _send_recv({"type": "get_supervised"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("supervised") or {}

    async def set_supervised(self, enabled: bool) -> dict:
        """Turn supervised mode ON/OFF. Returns the new status. Raises
        RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv(
            {"type": "set_supervised", "enabled": bool(enabled)})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "set_supervised failed")
        return resp.get("supervised") or {}

    async def get_pill_counts(self) -> dict:
        """The dashboard four notification-pill counts:
        ``{green, yellow, purple, red}`` (#65; see derive_pill_counts).
        Raises RuntimeError if the manager is unreachable (so the route 503s
        rather than reporting misleading zeros)."""
        await self._ensure_once()
        resp = await _send_recv({"type": "get_pill_counts"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("pill_counts") or {}

    async def pulse_status(self) -> dict:
        """The cheap Claude-tab AGENTS-button glow/pulse signal:
        ``{active, tick_seq}`` (active = any run RUNNING/QUEUED; tick_seq = the
        monotonic scheduler-check counter — see AgentManagerServer.pulse_status).
        Raises RuntimeError if the manager is unreachable so the route 503s rather
        than reporting a misleading idle tab."""
        await self._ensure_once()
        resp = await _send_recv({"type": "pulse_status"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("pulse") or {}

    async def get_snapshot(self, sections: str = "full") -> dict:
        """The consolidated agent-panel view-model (#149): runs (log-stripped),
        pill_counts, pulse, loop_health, watch_gate, and supervised — in one
        object. ``sections`` selects which half: ``"full"`` (default — all six),
        ``"cheap"`` (the in-memory half: runs, pulse, watch_gate, supervised), or
        ``"gh"`` (the GitHub half: pill_counts + loop_health). The live push
        fetches ``cheap`` then ``gh`` so cards render before the slow gh reads
        land (#151). Raises RuntimeError if the agent-manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv(
            {"type": "get_snapshot", "sections": sections},
            timeout=MANAGER_SNAPSHOT_TIMEOUT)
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("snapshot") or {}

    async def loop_status(self) -> dict:
        """The loop-health snapshot (arm/freeze state, in-flight/PR-cap, per-stage
        running flags, blockers — see AgentManagerServer.loop_status).
        Raises RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "loop_status"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("status") or {}

    async def resolve_blocker(self, action: dict) -> dict:
        """Perform one mechanical unblock (clear_freeze / close_pr — see
        AgentManagerServer.resolve_blocker). Returns the result dict. Raises
        ValueError on a bad/failed action, RuntimeError if the manager is
        unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "resolve_blocker", "action": action})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "resolve_blocker failed")
        return {k: v for k, v in resp.items() if k != "type"}

    # ── PM approve / reject (Slice C) ──────────────────────────────────────────

    async def approve_issue(self, number: int) -> None:
        """PM sign-off APPROVE: flip issue #number needs-signoff → approved and
        log the decision. Raises ValueError on a bad-request error payload,
        RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "approve_issue", "number": int(number)})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "approve_issue failed")

    async def reject_issue(self, number: int) -> None:
        """PM sign-off REJECT: flip issue #number needs-signoff → rejected and
        log the decision. Same error contract as approve_issue."""
        await self._ensure_once()
        resp = await _send_recv({"type": "reject_issue", "number": int(number)})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "reject_issue failed")

    # ── 🟣 trading-surface approve + 🟢 completed ack (#65) ─────────────────────

    async def trade_approve_issue(self, number: int) -> None:
        """🟣 TRADING-SURFACE APPROVE: clear the #63 money wall on issue #number by
        applying the trade-approved clearance (+ approved) labels so Eli re-polls
        and proceeds. Same error contract as approve_issue. NO reject counterpart —
        approving is the only way to clear the wall."""
        await self._ensure_once()
        resp = await _send_recv(
            {"type": "trade_approve_issue", "number": int(number)})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "trade_approve_issue failed")

    async def list_trade_approval_issues(self) -> list:
        """The 🟣 inbox: ``[{number, title}]`` for issues at the #63 money wall.
        Raises RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "list_trade_approval_issues"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return resp.get("issues") or []

    async def trade_approval_brief(self, number: int) -> dict:
        """The 🟣 view payload for issue #number: ``{number, title, brief}``. Raises
        RuntimeError if the manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv(
            {"type": "trade_approval_brief", "number": int(number)})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "trade_approval_brief failed")
        return resp.get("brief") or {}

    async def ack_completed(self) -> dict:
        """🟢 ACK COMPLETED: mark all currently-succeeded runs SEEN so the green pill
        clears on viewing. Returns ``{acked: <int>}``. Raises RuntimeError if the
        manager is unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "ack_completed"})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        return {"acked": resp.get("acked", 0)}

    async def ack_problem(self, run_id: str) -> dict:
        """🔴 ACK PROBLEM: mark one halted run acknowledged so it drops out of the
        red pill. Returns ``{acked: <int>}``. Raises RuntimeError if the manager is
        unreachable, ValueError on a bad request."""
        await self._ensure_once()
        resp = await _send_recv({"type": "ack_problem", "run_id": run_id})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "ack_problem failed")
        return {"acked": resp.get("acked", 0)}

    # ── run lifecycle (issue #30) ─────────────────────────────────────────────

    async def run_now(self, agent_id: str) -> dict:
        """Launch an agent now: create a Run, host it as a tagged terminal, and
        return the Run record (status queued/running, or failed on launch).

        Raises AgentDisabledError if the agent is OFF (a hard stop, #40),
        ValueError with the manager's reason for an unknown agent, or
        RuntimeError if the manager is unreachable.
        """
        await self._ensure_once()
        resp = await _send_recv({"type": "run_now", "id": agent_id})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            # A disabled agent is tagged code:"disabled" so the route can map it
            # to a 409 rather than the unknown-agent 404.
            if resp.get("code") == "disabled":
                raise AgentDisabledError(resp.get("error") or "agent is disabled")
            raise ValueError(resp.get("error") or "run_now failed")
        if resp.get("type") == "run":
            return resp.get("run")
        raise RuntimeError(f"unexpected response from agent-manager: {resp!r}")

    async def list_runs(self) -> list[dict]:
        """Return all run records (newest first)."""
        await self._ensure_once()
        resp = await _send_recv({"type": "list_runs"})
        if resp and resp.get("type") == "runs":
            return resp.get("runs", [])
        return []

    async def get_run(self, run_id: str) -> dict | None:
        """Return one run record, or None if missing / manager unreachable."""
        await self._ensure_once()
        resp = await _send_recv({"type": "get_run", "run_id": run_id})
        if resp and resp.get("type") == "run":
            return resp.get("run")
        return None

    async def cancel_run(self, run_id: str) -> dict:
        """Cancel a running run: kill its session and mark it cancelled. Returns
        the updated Run record.

        Raises ValueError with the manager's reason for an unknown run, or
        RuntimeError if the manager is unreachable.
        """
        await self._ensure_once()
        resp = await _send_recv({"type": "cancel_run", "run_id": run_id})
        if resp is None:
            raise RuntimeError("agent-manager unreachable")
        if resp.get("type") == "error":
            raise ValueError(resp.get("error") or "cancel_run failed")
        if resp.get("type") == "run":
            return resp.get("run")
        raise RuntimeError(f"unexpected response from agent-manager: {resp!r}")

    async def restart_manager(self) -> dict:
        """Stop the agent-manager process.

        Two paths, tried in order:
          1. ``rpc`` — the graceful ``{"type":"shutdown"}`` request: the
             manager acks and exits.
          2. ``pid-kill`` — fallback for an old manager binary that predates
             the RPC (it closes the connection without responding, so the RPC
             attempt fails fast on EOF) or a hung manager (RPC times out).
             Reads logs/agent_manager.pid, VERIFIES the process command line
             references trident_agent_manager.py (PIDs get recycled — never
             kill an arbitrary process), then taskkills ONLY that process —
             never its tree. The pty-manager + live agent sessions are
             descendants that MUST survive a brain restart ([[footguns#189]]);
             a tree kill here was the 2026-06-24 terminal-cascade bug.

        Deliberately does NOT respawn the manager: lazy respawn on next agent
        access is the established design. The spawn-once latch is reset up front
        (under the ensure lock) so the next API call re-detects and respawns
        cleanly.

        Returns {"ok": bool, "method": "rpc"|"pid-kill"|"not-running",
        "detail": str}.
        """
        workspace = self._resolve_workspace()

        def _result(ok: bool, method: str, detail: str) -> dict:
            _LOG.info("restart_manager: ok=%s method=%s detail=%s", ok, method, detail)
            return {"ok": ok, "method": method, "detail": detail}

        # Hold the ensure lock for the whole restart so no agent API call can
        # probe the dying manager's port mid-shutdown and re-latch _ensured
        # against a process that's about to exit. Concurrent calls block here,
        # then re-ensure (and lazily respawn) once we're done.
        async with self._ensure_lock:
            # Invalidate up front — even on a failed restart, leaving the latch
            # False is safe (the next call just re-checks the port).
            self._ensured = False

            if not await _manager_port_open():
                return _result(True, "not-running",
                               "agent-manager is not running — nothing to stop")

            # ── path 1: graceful shutdown RPC ─────────────────────────────
            resp = await _send_recv({"type": "shutdown"}, timeout=MANAGER_SHUTDOWN_TIMEOUT)
            if resp is not None and resp.get("type") == "shutdown" and resp.get("ok"):
                closed = await _wait_manager_port_closed(MANAGER_EXIT_TIMEOUT)
                detail = "manager shut down gracefully"
                if not closed:
                    detail += f" — warning: port {MANAGER_PORT} still open after ack"
                return _result(True, "rpc", detail)

            # ── path 2: verified PID-kill fallback ────────────────────────
            pid = await asyncio.to_thread(_read_manager_pid, workspace)
            if pid is None:
                return _result(False, "pid-kill",
                               "shutdown RPC failed and logs/agent_manager.pid is "
                               "missing or unreadable — kill the agent-manager "
                               "process manually")
            cmdline = await asyncio.to_thread(_pid_commandline, pid)
            if not cmdline or "agent_manager.py" not in cmdline:
                return _result(False, "pid-kill",
                               f"refusing to kill PID {pid}: its command line "
                               f"does not reference agent_manager.py "
                               f"(stale PID file / recycled PID)")
            killed, kill_detail = await asyncio.to_thread(_force_kill_pid, pid)
            if not killed:
                return _result(False, "pid-kill",
                               f"taskkill on PID {pid} failed: {kill_detail}")
            closed = await _wait_manager_port_closed(MANAGER_EXIT_TIMEOUT)
            detail = f"manager PID {pid} force-killed (single process — pty-manager preserved)"
            if not closed:
                detail += f" — warning: port {MANAGER_PORT} still open after kill"
            return _result(True, "pid-kill", detail)


# Module-level singleton — shared across all FastAPI requests.
manager = AgentSessionManager()
