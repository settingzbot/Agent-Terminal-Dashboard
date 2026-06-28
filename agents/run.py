"""
agents/run.py — run-engine seams for the agent manager (issue #30).

This module holds the two injection seams the run lifecycle in
trident_agent_manager.py is built around, kept OUT of the manager so tests can
fake them without a TCP daemon or a real ConPTY:

1. ``PtyTerminalClient`` — the thin async client the agent-manager uses to host
   a run's terminal in the standalone pty-manager (trident_pty_manager.py). It
   reuses daemons.terminal_client's TCP transport (``_send_recv`` + host/port), sending
   the #25 ``create`` payload with ``command``/``tag`` and the ``kill``/``list``
   RPCs. We do NOT reimplement the pty TCP protocol and we do NOT modify
   daemons.terminal_client — this is a separate caller over the same wire.

   Why a separate client and not daemons.terminal_client.manager.create()? That
   manager's ``create(cwd, label)`` signature predates the #25 command/tag
   extension and is the dashboard's USER-shell client (it lazy-spawns the
   pty-manager from the dashboard process). The agent-manager is a different
   process with a different lifetime; it talks to the already-running
   pty-manager directly and needs the command/tag fields. Keeping this as its
   own small client avoids widening daemons.terminal_client's public surface. It does
   reuse daemons.terminal_client's ``_ensure_manager`` to RESPAWN the pty-manager if it
   has died (the agent loop must self-heal a dead manager rather than halt on
   it — the 2026-06-24 #95 freeze); see ``PtyTerminalClient.create``.

2. ``build_run_command`` — resolves an agent definition into the command string
   the terminal runs. This is the seam #35 (worktree + issue→PR prompt) will
   wrap; for this slice it is a plain launcher invocation.

Both are injected into AgentManagerServer so the run-lifecycle tests can pass a
fake terminal client (no ConPTY) and assert on the recorded status transitions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from enum import Enum

from agents import guards
from agents import provider
from daemons import terminal_client

_LOG = logging.getLogger(__name__)


class SessionLiveness(str, Enum):
    """Tri-state liveness of a hosted pty session, as the manager sees it.

    ``is_alive`` collapses two genuinely different states into one boolean:
    ``ALIVE`` (the session is running) and ``UNREACHABLE`` (the pty-manager
    didn't answer the list RPC). That collapse is deliberate for a *single*
    poll — a brief TCP blip must not prematurely complete a run — but it is also
    the root cause of two failure modes (#119): a dead manager reads as "alive"
    forever, so a clock/manual run never leaves RUNNING, and the pipeline wait
    loop ages a dead-manager run into a bogus "30-minute timeout" halt note that
    blames the agent for an infrastructure fault. This tri-state lets a wait loop
    tell a transient blip from a genuinely dead manager and react honestly."""

    ALIVE = "alive"
    ENDED = "ended"
    UNREACHABLE = "unreachable"


async def probe_session_liveness(terminal, session_id: str) -> SessionLiveness:
    """Tri-state liveness probe over a terminal seam, with back-compat fallback.

    Prefers ``terminal.probe(session_id)`` (the real client + any fake that opts
    in). A terminal that only exposes the legacy ``is_alive`` can't distinguish
    UNREACHABLE from ALIVE (is_alive folds the two together by design), so its
    ``True`` maps to ALIVE and ``False`` to ENDED — such a terminal simply never
    reports a dead manager, which is correct for the fakes that don't exercise
    that path."""
    probe = getattr(terminal, "probe", None)
    if probe is not None:
        return await probe(session_id)
    return SessionLiveness.ALIVE if await terminal.is_alive(session_id) else SessionLiveness.ENDED


# ═══════════════════════════════════════════════════════════════════════════════
# Guard enforcement seam (issue #34)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Runs use LOOSENED tool-permissions so unattended 3am runs don't stall on
# approval prompts. Containment of the two failure modes worktree isolation (#35)
# does NOT cover — pushing/merging to `main`, and reading the OS keyring/secrets —
# comes from these two enforcement points, both of which delegate to the pure
# predicate in agents.guards so the policy lives in exactly one place:
#
#   1. ``enforce_run_command`` — applied to the LAUNCH command before a run is
#      handed to the pty-manager. Blocks a run whose own command is forbidden.
#   2. ``install_pre_push_hook`` — writes a git ``pre-push`` hook into the run's
#      worktree so a forbidden ``git push`` the agent issues *inside* the run is
#      blocked by git itself at push time (when the real resolved refspec is
#      known — the backstop the typed-string check can't see). #35's worktree
#      step calls this after creating the worktree; for this slice it's a clean
#      standalone function tested against a throwaway .git dir.
#
# ASYMMETRY (#121, L8): only the push axis is defended in-session — the pre-push
# hook sees every push the agent issues inside the run. The SECRET-READ axis has
# NO in-session backstop: ``enforce_run_command`` checks only the LAUNCH string
# (just ``claude ...`` for a coding run, which never reads a secret), so a
# ``keyring get`` the agent types mid-session is not blocked by anything here. A
# clean launch verdict is NOT evidence a run never read a secret.


class ForbiddenCommand(RuntimeError):
    """A run command was blocked by the guard predicate. Carries the verdict's
    reason so the run record / transcript can explain the block."""

    def __init__(self, verdict) -> None:
        self.verdict = verdict
        super().__init__(verdict.reason or "command blocked by run guard")


def enforce_run_command(command: str) -> str:
    """Raise :class:`ForbiddenCommand` if ``command`` is forbidden, else return
    it unchanged. The launch-command guard seam — call this on the command a run
    is about to execute (the #35 worktree builder will route its launch command
    through here).

    SCOPE (#121, L8): this guards ONLY the launch string, never the commands the
    agent issues inside the loosened-permission session. The push axis has the
    in-session pre-push hook as a backstop; the secret-read axis has none, so a
    pass here does not mean the run won't read a secret mid-session."""
    verdict = guards.check_command(command)
    if verdict.blocked:
        _LOG.warning("run command blocked (%s): %s", verdict.category, verdict.reason)
        raise ForbiddenCommand(verdict)
    return command


# The pre-push hook is a *python* script (not a bash shebang script) so it runs
# on the Windows production host without a POSIX shell — git invokes the hook via
# the path in core.hooksPath, and on Windows git uses the file's shebang through
# its bundled sh, but a python hook with a `#!/usr/bin/env python` shebang plus a
# `.py`-free name works under Git-for-Windows' MSYS sh which honours shebangs.
# To be OS-robust we make the hook self-contained: it re-derives the predicate by
# importing agents.guards from the repo root it was installed against, and
# reads git's stdin refspec lines to get the REAL push targets.
_PRE_PUSH_HOOK_TEMPLATE = '''#!/usr/bin/env python
"""Auto-generated by agents.run.install_pre_push_hook (issues #34, #51).

git pre-push hook. STDIN carries one line per ref being pushed:
    <local ref> <local sha> <remote ref> <remote sha>
Two axes are enforced, both delegating to agents.guards so the policy lives
in exactly one place:

  * Protected branch (#34): reconstruct an equivalent
    `git push <remote> <local>:<remote-ref>` string and run it through
    guards.check_command. A push to main/master aborts the push.
  * Built artifact (#51): resolve the REAL diff this push would carry
    (remote_sha..local_sha, or origin/main..local_sha for a new branch) and run
    the changed paths through guards.check_pushed_paths. A diff touching
    dashboard_v2/ aborts the push — agent PRs are source-only.
"""
import subprocess
import sys

sys.path.insert(0, {repo_root!r})
try:
    from agents import guards as guards
except Exception as e:  # pragma: no cover - install-time path issue
    sys.stderr.write("trident pre-push guard could not load predicate: %s\\n" % e)
    # Fail CLOSED for the protected-branch axis: if we can't load the guard, refuse.
    sys.exit(1)


def _changed_paths(local_sha, remote_sha):
    """Repo-relative paths this push would add/modify. Fails OPEN (returns []) if
    the diff can't be computed — a wrongly-blocked source-only push would stall an
    unattended run, and the catastrophic axis (push to main) is enforced
    independently from the refspec. A branch deletion carries no new paths."""
    if not local_sha or set(local_sha) == set("0"):
        return []
    if remote_sha and set(remote_sha) != set("0"):
        rng = remote_sha + ".." + local_sha
    else:
        # New branch on the remote: diff against the merge-base with origin/main.
        rng = "origin/main..." + local_sha
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", rng],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return []
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


remote_name = sys.argv[1] if len(sys.argv) > 1 else "origin"
blocked = False
for line in sys.stdin:
    parts = line.split()
    if len(parts) < 4:
        continue
    local_ref, local_sha, remote_ref, remote_sha = parts[:4]
    # Axis 1 — protected branch (refspec string → predicate).
    pseudo = "git push %s %s:%s" % (remote_name, local_ref, remote_ref)
    verdict = guards.check_command(pseudo)
    if verdict.blocked:
        sys.stderr.write(
            "BLOCKED by trident run guard: %s (%s)\\n" % (verdict.reason, remote_ref)
        )
        blocked = True
    # Axis 2 — built artifact (real diff paths → predicate).
    pverdict = guards.check_pushed_paths(_changed_paths(local_sha, remote_sha))
    if pverdict.blocked:
        sys.stderr.write("BLOCKED by trident run guard: %s\\n" % pverdict.reason)
        blocked = True
sys.exit(1 if blocked else 0)
'''


def _run_git(argv) -> None:
    """Run ``git <argv>`` and raise on a non-zero exit.

    The agent host (the laptop) has ``git`` on PATH — the worktree creator
    (``trident_agent_worktree._default_runner``) invokes it the same way. Kept
    tiny + injectable so tests never shell out.
    """
    proc = subprocess.run(["git", *argv], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(map(str, argv))} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )


def install_pre_push_hook(worktree: str | os.PathLike, *, git_runner=None) -> Path:
    """Install the protected-branch pre-push guard for ``worktree``.

    Handles BOTH git layouts:

    * **Normal repo** — ``<worktree>/.git`` is a *directory*; hooks live in
      ``<worktree>/.git/hooks``. (Used by the tests.)
    * **Linked git worktree** (``git worktree add``, the production path) —
      ``<worktree>/.git`` is a *file* pointing at ``<repo>/.git/worktrees/<name>``.
      A linked worktree resolves its hooks from the **shared** hooks dir, so a
      hook written into the worktree's own gitdir is ignored unless we point
      ``core.hooksPath`` at it. We do exactly that — **scoped to THIS worktree
      only** (per-worktree config) — which is why the worker's "block push to
      main" guard never lands in the main repo's shared hooks and so can never
      block the human's own pushes.

    Returns the hook path. Raises ``FileNotFoundError`` if there is no resolvable
    git dir (the caller creates the worktree first). For a linked worktree the
    ``core.hooksPath`` scoping is **fail-closed**: if it can't be set the call
    raises, because an unpointed hook would silently leave the run unguarded.
    ``git_runner`` is injectable for tests.
    """
    wt = Path(worktree)
    dotgit = wt / ".git"
    linked = False
    if dotgit.is_dir():
        git_dir = dotgit
    elif dotgit.is_file():
        # A linked worktree's .git is a one-line pointer: "gitdir: <abs path>".
        pointer = dotgit.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            raise FileNotFoundError(f"{dotgit} is not a valid git worktree pointer")
        git_dir = Path(pointer.split(":", 1)[1].strip())
        linked = True
    else:
        raise FileNotFoundError(f"no .git at {wt} — create the worktree first")

    hooks_dir = git_dir / "hooks"
    # A linked worktree's gitdir has no hooks/ by default — create it.
    hooks_dir.mkdir(parents=True, exist_ok=True)

    repo_root = str(Path(__file__).resolve().parent)
    hook_path = hooks_dir / "pre-push"
    hook_path.write_text(
        _PRE_PUSH_HOOK_TEMPLATE.format(repo_root=repo_root),
        encoding="utf-8",
    )
    # Make it executable (no-op effect on Windows file ACLs, but Git-for-Windows
    # MSYS honours the unix exec bit when present).
    mode = hook_path.stat().st_mode
    hook_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if linked:
        # extensions.worktreeConfig makes `config --worktree` write the
        # per-worktree config.worktree (without it, --worktree falls back to the
        # SHARED config and would leak core.hooksPath to the main repo). Then
        # point this worktree's hooks at the hook we just wrote. The main repo's
        # shared hooks are never written.
        run_git = git_runner or _run_git
        run_git(["-C", str(wt), "config", "extensions.worktreeConfig", "true"])
        run_git(["-C", str(wt), "config", "--worktree", "core.hooksPath", str(hooks_dir)])

    _LOG.info("installed pre-push guard hook at %s (linked worktree=%s)", hook_path, linked)
    return hook_path


# ═══════════════════════════════════════════════════════════════════════════════
# Command builder (injection seam — #35 will wrap this)
# ═══════════════════════════════════════════════════════════════════════════════

# The agent definition's `model` is one of the launcher verbs from
# scripts/claude*.ps1 (see trident_agent_store.VALID_MODELS). The bare "claude"
# verb is the profile function; the others are the .ps1 launchers. The
# pty-manager runs `command` through `powershell.exe -Command`, so a launcher
# name resolves through the user's PowerShell profile exactly as if Nathan typed
# it in a terminal tab.

def build_run_command(agent: dict, *, session_id: str | None = None) -> str:
    """Build the command string that runs an agent in a tagged terminal.

    Plain launcher invocation for this slice: ``<model> -p '<prompt>'`` where
    ``<model>`` is the launcher verb (claude / claude-ds / claude-go /
    claude-ds-go) and ``<prompt>`` is the agent's prompt passed as the initial
    instruction. Single quotes in the prompt are escaped for PowerShell ('' is
    a literal single quote inside a single-quoted string).

    ``session_id`` (a uuid): when given, ``--session-id <uuid>`` is added so
    Claude Code writes this run's transcript under a KNOWN filename. The dashboard
    feed resolves the transcript by that id; without it Claude picks its own
    random uuid and the feed can't find the file (it then fell back to the newest
    transcript in the shared project dir — the operator's own Claude tab — which
    was the feed cross-wiring bug). The caller stores the same id on the run
    record's ``transcript_id``.

    #35 will replace this with a worktree-aware, issue→PR-prompt-aware builder;
    keeping it a single small function is the clean injection point.
    """
    model = agent.get("model") or "claude"
    prompt = agent.get("prompt") or ""
    # PowerShell single-quote escaping: double every single quote.
    safe_prompt = prompt.replace("'", "''")
    sid = f" --session-id {session_id}" if session_id else ""
    provider_prefix = provider.get_provider_env_prefix()
    return f"{provider_prefix}{model}{sid} -p '{safe_prompt}'"


# ═══════════════════════════════════════════════════════════════════════════════
# PtyTerminalClient (injection seam — the pty-manager wire)
# ═══════════════════════════════════════════════════════════════════════════════

class PtyTerminalClient:
    """Async client the agent-manager uses to host runs in the pty-manager.

    Reuses daemons.terminal_client's TCP transport (same host/port/_send_recv), so we
    don't reimplement the JSON-lines protocol. Three verbs are all the run
    engine needs: create a tagged session, check whether a session is still
    alive, and kill a session (cancel).
    """

    # Bounded self-heal on launch: if the pty-manager has died on its own (a
    # silent native ConPTY/pywinpty crash — the cause of the 2026-06-24 loop
    # freeze on #95), the FIRST create comes back unreachable. Unlike the
    # dashboard's user-shell client, this agent client has no lazy-spawn latch,
    # so without this it fails forever until a dashboard restart and the loop
    # halts on a transient. We respawn the dead manager and retry — but ONLY
    # pre-launch (a failed create never started a claude session, so a retry can
    # never double-run an agent or open two PRs; the single-launched-run
    # invariant is untouched).
    DEFAULT_LAUNCH_ATTEMPTS = 3
    DEFAULT_LAUNCH_BACKOFF_S = 1.0

    def __init__(
        self,
        *,
        send_recv=None,
        ensure_manager=None,
        sleep=None,
        max_launch_attempts: int = DEFAULT_LAUNCH_ATTEMPTS,
        launch_backoff_s: float = DEFAULT_LAUNCH_BACKOFF_S,
        workspace: str | None = None,
    ) -> None:
        # All seams default to the real wire so production constructs it with no
        # args (the manager does ``terminal_client or PtyTerminalClient()``);
        # tests inject fakes so no TCP/ConPTY/process-spawn happens in the suite.
        self._send_recv = send_recv or terminal_client._send_recv
        self._ensure_manager = ensure_manager or terminal_client._ensure_manager
        self._sleep = sleep or asyncio.sleep
        self._max_launch_attempts = max(1, max_launch_attempts)
        self._launch_backoff_s = launch_backoff_s
        self._workspace = workspace

    def _resolve_workspace(self) -> str:
        # Same resolution the dashboard's client uses: the repo root is the dir
        # holding daemons.terminal_client (where _spawn_manager finds the manager
        # script + writes logs/).
        if self._workspace is None:
            self._workspace = os.path.dirname(os.path.abspath(terminal_client.__file__))
        return self._workspace

    async def create(self, *, command: str, tag: str, cwd: str) -> str | None:
        """Create a tagged pty session running ``command`` in ``cwd``.

        Returns the new session id, or None if the session could not be started.

        Self-healing: a create that comes back UNREACHABLE (``_send_recv`` ->
        None, i.e. the pty-manager process is gone) respawns the manager and
        retries, up to ``max_launch_attempts``. A create the manager actively
        REFUSES (a response that isn't ``created`` — e.g. pywinpty missing) is a
        persistent fault, so it returns None immediately without a pointless
        respawn loop. The agent-manager maps None to a failed run.
        """
        attempts = self._max_launch_attempts
        for attempt in range(1, attempts + 1):
            resp = await self._send_recv({
                "type": "create",
                "cwd": cwd,
                "command": command,
                "tag": tag,
            })
            if resp and resp.get("type") == "created":
                return resp["session"]["id"]
            if resp is not None:
                # Manager answered but didn't create — a real, persistent refusal
                # (respawning can't fix pywinpty being absent). Don't retry.
                _LOG.warning("pty create refused for tag %s: %r", tag, resp)
                return None
            # resp is None -> pty-manager unreachable. Respawn it and retry.
            if attempt < attempts:
                _LOG.warning(
                    "pty-manager unreachable on create (tag %s) — attempt %d/%d, "
                    "respawning manager and retrying", tag, attempt, attempts)
                try:
                    await self._ensure_manager(self._resolve_workspace())
                except Exception:
                    _LOG.exception("pty-manager respawn attempt %d failed", attempt)
                await self._sleep(self._launch_backoff_s)
        _LOG.error(
            "pty create failed after %d attempts — pty-manager unreachable (tag %s)",
            attempts, tag)
        return None

    async def probe(self, session_id: str) -> SessionLiveness:
        """Tri-state liveness: ALIVE, ENDED, or UNREACHABLE (#119).

        UNREACHABLE (the list RPC didn't come back) is reported distinctly so a
        wait loop can tell a transient TCP blip from a genuinely dead manager,
        rather than the old binary that folded "unreachable" into "alive" and so
        let a dead manager look alive forever."""
        resp = await self._send_recv({"type": "list"})
        if not (resp and resp.get("type") == "sessions"):
            return SessionLiveness.UNREACHABLE
        for s in resp.get("sessions", []):
            if s.get("id") == session_id:
                return SessionLiveness.ALIVE if s.get("alive") else SessionLiveness.ENDED
        # Session gone from the manager's table entirely ⇒ it ended.
        return SessionLiveness.ENDED

    async def is_alive(self, session_id: str) -> bool:
        """True if the pty session is still running. Unreachable ⇒ treated as
        alive (don't prematurely complete a run on a transient TCP blip).

        Thin shim over ``probe`` so the legacy binary contract is preserved for
        callers that don't need the unreachable/alive distinction."""
        return await self.probe(session_id) is not SessionLiveness.ENDED

    async def kill(self, session_id: str) -> bool:
        """Kill a session by id. True if the manager confirmed the kill."""
        resp = await self._send_recv(
            {"type": "kill", "session_id": session_id}
        )
        return resp is not None and resp.get("type") == "killed"

    async def ensure_pty_alive(self) -> bool:
        """PROACTIVE watchdog: ping the pty-manager and respawn it if unreachable.

        create()'s self-heal ([[footguns#189]]) is REACTIVE — it only respawns a
        dead manager when a launch actually fails. This is the proactive twin: the
        scheduler calls it every tick so a manager that died between runs is healed
        BEFORE the next run needs it (and before the autonomous loop, at 3am with no
        human clicking, eats a failed-launch escalation). Returns True if the
        manager is reachable (already, or after a successful respawn); False if a
        respawn was attempted and still failed. Never raises."""
        try:
            if await self._send_recv({"type": "list"}) is not None:
                return True
            _LOG.warning("pty-manager unreachable on health check — respawning proactively")
            try:
                await self._ensure_manager(self._resolve_workspace())
            except Exception:
                _LOG.exception("proactive pty-manager respawn failed")
                return False
            return await self._send_recv({"type": "list"}) is not None
        except Exception:
            _LOG.exception("pty-manager health check raised")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# Launch-and-wait (the issue→PR pipeline must WAIT for claude before grading)
# ═══════════════════════════════════════════════════════════════════════════════

# Default per-run backstop. A healthy headless `claude -p` run exits on its own
# when the task is done — the timeout only fires on a stuck/runaway session. The
# watch path overrides this per-agent (trident_agent_store.DEFAULT_TIMEOUT_MINUTES).
DEFAULT_RUN_TIMEOUT_S = 30 * 60.0
RUN_POLL_INTERVAL_S = 2.0

# Grace window before sustained pty-manager unreachability is ruled a genuinely
# DEAD manager rather than a transient TCP blip (#119). is_alive/probe survive a
# single blip by design; only an unbroken UNREACHABLE streak longer than this is
# treated as manager death — short enough to free a wedged slot well before the
# full run timeout, long enough to ride out a respawn-and-reconnect.
MANAGER_DEATH_GRACE_S = 90.0

# Retry-don't-halt (issue #96 hardening): a session that fails to LAUNCH (pty
# create returned None even after create()'s own self-heal) is a TRANSIENT infra
# fault — ride it out a few times before letting the pipeline escalate to a human.
# A launch that SUCCEEDS is never retried here (a timeout could loop forever; a
# clean completion is success). Small budget — create() already retries the pty
# respawn internally, so this is the outer "the whole launch flapped" backstop.
DEFAULT_LAUNCH_ATTEMPTS = 3
LAUNCH_RETRY_BACKOFF_S = 3.0


@dataclass
class SessionRunResult:
    """Outcome of launching a command in a pty session and waiting for it."""
    session_id: str | None
    launched: bool      # the pty session was created
    completed: bool     # the session ended on its own within the timeout
    timed_out: bool     # the session was killed for exceeding the timeout
    manager_dead: bool = False  # the pty-manager went unreachable past the grace
    #                             window — an INFRA fault, not an agent timeout (#119)


@dataclass(frozen=True)
class LaunchOutcome:
    """A guarded launch-and-wait result that carries WHY it failed.

    Truthy (``__bool__``) iff the session launched, ran to completion, and did NOT
    time out — so existing ``if not ok:`` / ``if ok:`` call sites keep working
    unchanged. When falsy, ``reason`` is a TRUTHFUL, plain-English cause (timeout
    vs failed-to-launch vs raised) the pipeline can put straight into a halt note,
    instead of the generic "claude run did not start / errored" that masked a
    30-min TIMEOUT on issue #99 ([[footguns#195]] extended to the build path).
    Callers that get a plain ``bool`` from a fake launcher still work — they read
    ``getattr(ok, "reason", "")`` and fall back to a generic note."""
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


class PollOutcome(str, Enum):
    """How a tri-state liveness poll loop terminated, WITHOUT any side effect.

    The single poll loop (:func:`poll_session_until_terminal`) returns one of
    these; the CALLER applies the side-effects each one implies — the manager's
    run-watcher writes succeeded/failed to the run store and kills on a timeout,
    while ``run_session_to_completion`` maps it to a :class:`SessionRunResult`.
    Keeping the loop side-effect-free is what lets the duplicate that used to live
    in BOTH the manager's ``_watch_run`` and ``run_session_to_completion`` collapse
    to this one place (#143), so the #119 grace-window logic can never drift
    between two copies again."""

    ENDED = "ended"            # the session ended on its own
    TIMED_OUT = "timed_out"    # the deadline passed with the session still live
    MANAGER_DEAD = "manager_dead"  # an UNREACHABLE streak outlasted the grace (#119)


async def poll_session_until_terminal(
    terminal,
    session_id: str,
    *,
    timeout_s: float,
    poll_interval_s: float = RUN_POLL_INTERVAL_S,
    manager_death_grace_s: float = MANAGER_DEATH_GRACE_S,
    sleep=None,
    monotonic=None,
    logger=None,
) -> PollOutcome:
    """THE single tri-state liveness poll loop (#119 logic, de-duplicated #143).

    Polls ``terminal`` for ``session_id``'s liveness every ``poll_interval_s``
    until one of three terminal conditions, returned as a :class:`PollOutcome`
    with NO side effect (no kill, no store write — the caller owns those):

    * ``ENDED`` — a clean session end.
    * ``MANAGER_DEAD`` — the pty-manager went UNREACHABLE and stayed that way past
      ``manager_death_grace_s``. A single blip never trips this: any ALIVE poll
      resets the streak, so only a *sustained* outage is ruled a dead manager (an
      infra fault, never an agent timeout — #119 M2).
    * ``TIMED_OUT`` — ``timeout_s`` elapsed with the session still live/unreachable.

    A poll that RAISES is a transient transport blip, never a completion: it is
    logged and skipped (the loop sleeps and retries), so a TCP error can't
    complete or fail a run. The deadline still bounds a manager that never answers.

    ``sleep`` / ``monotonic`` are injectable so tests run instantly on a fake
    clock; ``logger`` defaults to this module's logger."""
    _sleep = sleep or asyncio.sleep
    _monotonic = monotonic or time.monotonic
    _log = logger or _LOG

    deadline = _monotonic() + timeout_s
    unreachable_since: float | None = None
    while True:
        try:
            liveness = await probe_session_liveness(terminal, session_id)
        except Exception:
            # A poll that RAISES is a transient blip, never a completion: sleep and
            # retry so a TCP error can't complete (or fail) a run. The deadline
            # still bounds a manager that never answers.
            _log.exception("liveness poll raised for session %s", session_id)
            await _sleep(poll_interval_s)
            continue
        if liveness is SessionLiveness.ENDED:
            return PollOutcome.ENDED
        now = _monotonic()
        if liveness is SessionLiveness.UNREACHABLE:
            # Start (or continue) the unreachable streak. A genuinely dead manager
            # never recovers, so once the streak outlasts the grace window we stop
            # waiting and report an infra fault rather than aging into a bogus
            # agent-timeout note.
            if unreachable_since is None:
                unreachable_since = now
            elif now - unreachable_since >= manager_death_grace_s:
                _log.error(
                    "pty-manager unreachable for %.0fs hosting session %s — "
                    "ruling it dead (infra fault, not an agent timeout)",
                    now - unreachable_since, session_id)
                return PollOutcome.MANAGER_DEAD
        else:  # ALIVE — a reachable, running session clears any blip streak
            unreachable_since = None
        if now >= deadline:
            return PollOutcome.TIMED_OUT
        await _sleep(poll_interval_s)


def classify_launch(result: SessionRunResult, *, timeout_s: float) -> LaunchOutcome:
    """Map a SessionRunResult to a LaunchOutcome with a truthful reason. PURE.

    The exception case (launch/wait raised) is built by the launcher itself, which
    has the exception in hand; this covers the SessionRunResult shapes."""
    if not result.launched:
        return LaunchOutcome(
            False,
            "claude session failed to launch after retries — pty-manager "
            "unreachable",
        )
    if result.manager_dead:
        # An INFRA fault, never the agent's fault — say so plainly instead of the
        # "task too large / agent stalled" timeout note that this exact case used
        # to age into (#119). Retryable-transient: the next tick respawns the
        # manager and the issue stays ready-for-agent for a fresh attempt.
        return LaunchOutcome(
            False,
            "the pty-manager hosting this run became unreachable and did not "
            "recover — an infrastructure fault, not the agent. The run was not "
            "completed; it can be retried once the manager is back.",
        )
    if result.timed_out:
        return LaunchOutcome(
            False,
            f"claude exceeded its {timeout_s / 60.0:.0f}-minute timeout and was "
            "killed — the task is likely too large for one run, or the agent stalled",
        )
    return LaunchOutcome(True)


async def run_session_to_completion(
    terminal,
    *,
    command: str,
    cwd: str,
    tag: str,
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
    poll_interval_s: float = RUN_POLL_INTERVAL_S,
    manager_death_grace_s: float = MANAGER_DEATH_GRACE_S,
    sleep=None,
    monotonic=None,
) -> SessionRunResult:
    """Create a tagged pty session running ``command`` and WAIT for it to finish.

    The issue→PR pipeline grades the worktree with pytest AFTER the run, so the
    launcher MUST block until claude actually exits. The original production
    launcher was fire-and-forget — it returned the instant the session spawned,
    so the pipeline graded an untouched worktree ([[footguns#168]], 2026-06-21).

    Polls liveness every ``poll_interval_s`` until the session ends or
    ``timeout_s`` elapses; on timeout it kills the session. A healthy
    ``claude -p`` exits on its own, so the timeout is only a backstop for a
    stuck/runaway run. The poll is TRI-STATE (#119): a sustained UNREACHABLE
    streak longer than ``manager_death_grace_s`` is reported as a DEAD MANAGER
    (``manager_dead``) instead of being ridden all the way to the timeout wall
    and mislabelled "the task is too large / the agent stalled" — that note
    blames the agent for an infrastructure fault. ``sleep`` / ``monotonic`` are
    injectable so tests run instantly against a fake clock.

    The poll loop ITSELF lives in :func:`poll_session_until_terminal` — the one
    place that owns the #119 tri-state logic, shared with the manager's run
    watcher (#143). This function is the LAUNCH-and-map wrapper: it creates the
    session, drives the shared loop, and maps its :class:`PollOutcome` to a
    :class:`SessionRunResult` (killing the session on a genuine timeout — a dead
    manager can't be reached to kill, which is why only ``TIMED_OUT`` kills).
    """
    session_id = await terminal.create(command=command, tag=tag, cwd=cwd)
    if not session_id:
        return SessionRunResult(
            session_id=None, launched=False, completed=False, timed_out=False
        )

    outcome = await poll_session_until_terminal(
        terminal, session_id,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        manager_death_grace_s=manager_death_grace_s,
        sleep=sleep, monotonic=monotonic,
    )
    if outcome is PollOutcome.ENDED:
        return SessionRunResult(
            session_id=session_id, launched=True, completed=True, timed_out=False
        )
    if outcome is PollOutcome.MANAGER_DEAD:
        return SessionRunResult(
            session_id=session_id, launched=True, completed=False,
            timed_out=False, manager_dead=True,
        )
    # TIMED_OUT — a reachable-but-never-finishing session is the genuine timeout;
    # kill it (best-effort) and report the wall.
    try:
        await terminal.kill(session_id)
    except Exception:
        _LOG.exception("failed to kill timed-out session %s", session_id)
    return SessionRunResult(
        session_id=session_id, launched=True, completed=False, timed_out=True
    )


async def run_session_with_retry(
    terminal,
    *,
    command: str,
    cwd: str,
    tag: str,
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
    max_attempts: int = DEFAULT_LAUNCH_ATTEMPTS,
    backoff_s: float = LAUNCH_RETRY_BACKOFF_S,
    sleep=None,
    **kwargs,
) -> SessionRunResult:
    """``run_session_to_completion`` with retry-don't-halt on a failed LAUNCH.

    Retries ONLY when the session never launched (``launched is False`` — the pty
    create returned None even after create()'s own respawn-and-retry, i.e. the
    infra flapped). A result that LAUNCHED is returned immediately regardless of
    ``completed`` / ``timed_out``: a timeout must NOT be retried (it could loop a
    runaway forever) and a completion is success. This is the outer backstop that
    turns a transient pty fault into a few-second delay instead of a needs-human
    halt ([[footguns#189]]/#96). Returns the LAST result (``launched is False``)
    once the budget is spent — the caller then escalates with a truthful note.

    ``**kwargs`` (e.g. ``poll_interval_s``, ``monotonic``) pass through to
    ``run_session_to_completion``; ``sleep`` is injectable for instant tests."""
    _sleep = sleep or asyncio.sleep
    attempts = max(1, max_attempts)
    last: SessionRunResult | None = None
    for attempt in range(1, attempts + 1):
        last = await run_session_to_completion(
            terminal, command=command, cwd=cwd, tag=tag,
            timeout_s=timeout_s, sleep=sleep, **kwargs,
        )
        if last.launched:
            return last
        if attempt < attempts:
            _LOG.warning(
                "session failed to launch (tag %s) — attempt %d/%d, retrying in %.0fs",
                tag, attempt, attempts, backoff_s)
            await _sleep(backoff_s)
    _LOG.error(
        "session failed to launch after %d attempts (tag %s) — giving up", attempts, tag)
    return last  # launched is False
