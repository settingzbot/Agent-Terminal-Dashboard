"""
trident_agent_run_engine.py — the run LIFECYCLE engine, lifted out of
``AgentManagerServer`` (issue #143, parent #138).

What this is
------------
The meatiest peel of the agent-manager god-hub: everything that walks ONE run
through its status machine ``queued → running → succeeded/failed/cancelled``
(plus the #117 sticky ``halted``). ``RunEngine`` owns:

* the run-store CRUD wrappers (``get_run`` / ``put_run`` / ``update_run`` /
  ``list_runs`` / ``mark_halted`` / ``new_run_id``) — lock-held passthroughs over
  the SHARED run store + lock the core still owns and injects here;
* manual-run launch (:meth:`run_now`) — resolve agent, create a Run, launch a
  tagged pty session, start the completion watcher;
* the session-watch loop (:meth:`watch_run`) + its watcher-task registry
  (``_run_watchers``, owned HERE, not in the core);
* run-timeout resolution (:meth:`run_timeout_s`, #119 M1);
* fail-on-timeout / manager-death (:meth:`fail_watched_run`, #119 M2);
* cancel (:meth:`cancel_run`).

The single-owner liveness poll (#143)
-------------------------------------
The tri-state liveness poll (alive / ended / unreachable-with-grace / deadline)
used to be DUPLICATED between this watcher and
``trident_agent_run.run_session_to_completion``. It now lives in exactly one
place — ``trident_agent_run.poll_session_until_terminal`` — which both call. So a
grace-window fix (#119) can never again drift between two copies. The loop is
side-effect-free; :meth:`watch_run` applies the run-store transitions (succeeded
or, via :meth:`fail_watched_run`, failed) and the token guardrail at run-finish.

What stays in the core
----------------------
The core (``AgentManagerServer``) keeps the shared stores, the lock, and the
scheduler loop. ``RunEngine`` reaches none of the scheduler's tasks and the
scheduler reaches none of the watcher tasks. The seams the engine needs are
INJECTED (terminal client, command builder, run cwd, get-agent, the per-run
timeout/poll/grace providers, and the token-stamp apply hook) so the lifecycle
can be unit-tested through fakes with no TCP daemon and no real ConPTY.

Run:  python -m pytest tests/test_agent_run_engine.py -q
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from typing import Callable

from agents.pipeline import new_transcript_id
from agents.run import (
    PollOutcome,
    poll_session_until_terminal,
)
from agents.run_store import (
    CANCELLED,
    FAILED,
    RUNNING,
    SUCCEEDED,
    is_terminal,
)


class AgentDisabledError(Exception):
    """Run-now was attempted on a DISABLED (OFF) agent (issue #40).

    OFF is a HARD STOP: a disabled agent is fully inert, so even the manual
    Run-now override is refused — not just autonomous scheduling. The run-now
    command handler maps this to an error response carrying ``code:"disabled"``
    so the dashboard route can surface a 409 (distinct from the unknown-agent
    404).

    Defined here (with the run lifecycle it guards) and re-exported from
    ``trident_agent_manager`` so existing ``trident_agent_manager.AgentDisabledError``
    references — the dashboard route and the run-engine tests — still resolve."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(
            f"agent {agent_id!r} is disabled (OFF) — re-enable it to run it"
        )


class RunEngine:
    """Owns one agent run's lifecycle end-to-end. See module docstring.

    Constructed with the SHARED run store + lock (owned by the core) plus the
    injectable seams the lifecycle needs. Everything tests reassign on the
    manager AFTER construction — the per-run timeout, the poll interval / grace
    window module constants, the token-stamp apply hook — is read through a
    PROVIDER (a thunk) so the engine resolves the manager's CURRENT value on each
    use, the same indirection idiom the StatusAggregator / TokenGuardrail
    extractions use.
    """

    def __init__(
        self,
        *,
        run_store,
        lock: threading.Lock,
        terminal,
        build_command: Callable,
        run_cwd: str,
        get_agent: Callable[[str], dict | None],
        default_timeout_minutes: float,
        run_timeout_provider: Callable[[str], float] | None = None,
        poll_interval_provider: Callable[[], float],
        manager_death_grace_provider: Callable[[], float],
        stamp_run_tokens: Callable[[str], object] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_store = run_store
        self._lock = lock
        self._terminal = terminal
        self._build_command = build_command
        self._run_cwd = run_cwd
        self._get_agent = get_agent
        self._default_timeout_minutes = default_timeout_minutes
        # The per-run deadline is read through a provider so a test that reassigns
        # the manager's ``_run_timeout_s`` (which delegates back here by default)
        # takes effect on the live watcher. Defaults to this engine's own logic.
        self._run_timeout_provider = run_timeout_provider or self.run_timeout_s
        self._poll_interval_provider = poll_interval_provider
        self._manager_death_grace_provider = manager_death_grace_provider
        # The token guardrail apply hook (the manager's ``_stamp_run_tokens``):
        # evaluate + apply at run-finish. A no-op default keeps the engine usable
        # standalone. Resolved live so a test can reassign it post-construction.
        self._stamp_run_tokens = stamp_run_tokens or (lambda _run_id: None)
        self._log = logger or logging.getLogger("agent_manager")

        # run_id → asyncio.Task watching that run's session to completion. Owned
        # HERE (not the core), so cancel can stop the watcher and shutdown can
        # drain them. The scheduler's tasks live in the core and never overlap
        # this registry.
        self._run_watchers: dict[str, asyncio.Task] = {}

    # ── run store CRUD (lock-held passthroughs over the SHARED store) ──────────
    # All store access is serialized under the SHARED lock the core owns: the run
    # store is one-file-per-run on disk, and concurrent connections (or the
    # recorders that also write through the manager's delegators) could otherwise
    # interleave a read with an update of the same file.

    def list_runs(self) -> list[dict]:
        with self._lock:
            return self._run_store.list()

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            return self._run_store.get(run_id)

    def put_run(self, rec: dict) -> dict:
        with self._lock:
            return self._run_store.put(rec)

    def update_run(self, run_id: str, **changes) -> dict:
        with self._lock:
            return self._run_store.update(run_id, **changes)

    def mark_halted(self, run_id: str, note: str | None = None,
                    finished_at: float | None = None) -> dict:
        """Persist a run as HALTED (Slice F escape hatch) via the run store's
        ``mark_halted``. Thin lock-held wrapper, same idiom as update_run."""
        with self._lock:
            return self._run_store.mark_halted(run_id, note=note, finished_at=finished_at)

    # ── run lifecycle (issue #30) ─────────────────────────────────────────────

    def new_run_id(self, agent_id: str) -> str:
        """``<agent_id>-<6 hex>`` — a unique, slug-safe run id (the run store's
        path-traversal guard requires [a-z0-9_-])."""
        return f"{agent_id}-{secrets.token_hex(3)}"

    def watcher_tasks(self) -> list[asyncio.Task]:
        """Live watcher tasks, for the core's shutdown drain (#114). The core
        reaches them ONLY through this accessor — it never touches the registry."""
        return list(self._run_watchers.values())

    async def run_now(self, agent_id: str) -> dict:
        """Resolve the agent, create a Run, launch it as a tagged terminal, and
        return the Run record (status running, or failed if launch failed).

        Raises KeyError if the agent does not exist, or AgentDisabledError if the
        agent is OFF — OFF is a HARD STOP (#40), so the manual run-now override is
        refused too, not just autonomous scheduling. The caller maps both to
        error responses (unknown → 404, disabled → 409).
        """
        agent = self._get_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        # OFF is a hard stop: a disabled agent is fully inert, manual launch
        # included. The scheduler already filters disabled agents out of its
        # candidates; this is the manual-path enforcement of the same rule (and
        # the backstop for an agent disabled between candidate-gathering and a
        # scheduled launch).
        if agent.get("enabled", True) is False:
            raise AgentDisabledError(agent_id)

        run_id = self.new_run_id(agent_id)
        tag = f"agent:{run_id}"
        # Pin a Claude transcript id so the dashboard feed resolves THIS run's
        # transcript by name. Without it Claude picks its own random uuid and the
        # feed fell back to the newest transcript in the shared project dir — the
        # operator's own Claude tab — which was the feed cross-wiring bug. Stored
        # on the run record's transcript_id; passed to the builder as --session-id.
        transcript_id = new_transcript_id()
        command = self._build_command(agent, session_id=transcript_id)
        now = time.time()

        # 1) queued — the Run exists on disk before we touch the terminal, so a
        #    crash mid-launch still leaves an auditable record.
        self.put_run({
            "run_id": run_id,
            "agent_id": agent_id,
            "tag": tag,
            "command": command,
            "cwd": self._run_cwd,
            "session_id": None,
            "transcript_id": transcript_id,
            "status": "queued",
            "outcome": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        })

        # 2) launch the tagged terminal in the pty-manager.
        try:
            session_id = await self._terminal.create(
                command=command, tag=tag, cwd=self._run_cwd
            )
        except Exception as e:  # defensive — a wire error shouldn't crash the RPC
            self._log.exception("run %s launch raised", run_id)
            return self.update_run(
                run_id, status="failed",
                outcome=f"launch error: {e}", finished_at=time.time(),
            )

        if not session_id:
            return self.update_run(
                run_id, status="failed",
                outcome="pty-manager refused to create the run terminal",
                finished_at=time.time(),
            )

        # 3) running — record the hosting session and start the completion watcher.
        rec = self.update_run(
            run_id, status=RUNNING, session_id=session_id, started_at=time.time(),
        )
        self.start_run_watcher(run_id, session_id)
        return rec

    def start_run_watcher(self, run_id: str, session_id: str) -> None:
        """Spawn the background task that completes the run when its session
        ends. No-op if a watcher already exists for this run."""
        if run_id in self._run_watchers and not self._run_watchers[run_id].done():
            return
        task = asyncio.create_task(self.watch_run(run_id, session_id))
        self._run_watchers[run_id] = task

    async def watch_run(self, run_id: str, session_id: str) -> None:
        """Poll the hosting session until it ends, then mark the run succeeded —
        OR fail it if it blows past its deadline / its manager dies (#119).

        This slice has no exit-code channel from the pty (a ConPTY shell's exit
        status isn't surfaced over the protocol yet), so a clean session end is
        recorded as ``succeeded`` with an explanatory outcome. A run already moved
        to a terminal status (e.g. cancelled) is left alone.

        The tri-state poll itself lives in ``poll_session_until_terminal`` — the
        ONE place that owns the #119 deadline + dead-manager grace logic, shared
        with ``run_session_to_completion`` (#143). This method drives that loop and
        APPLIES the run-store transitions its outcome implies: a clean ENDED is a
        success (then the token guardrail is stamped), and a TIMED_OUT / dead
        manager is failed via :meth:`fail_watched_run` with a truthful reason.
        """
        timeout_s = self._run_timeout_provider(run_id)
        try:
            outcome = await poll_session_until_terminal(
                self._terminal, session_id,
                timeout_s=timeout_s,
                poll_interval_s=self._poll_interval_provider(),
                manager_death_grace_s=self._manager_death_grace_provider(),
                logger=self._log,
            )
            if outcome is PollOutcome.ENDED:
                # Session ended. Only complete it if it's still running — a
                # concurrent cancel may have already moved it to terminal.
                current = self.get_run(run_id)
                if current is None or current.get("status") != RUNNING:
                    return
                self.update_run(
                    run_id, status=SUCCEEDED,
                    outcome="run terminal exited", finished_at=time.time(),
                )
                # Session ended ⇒ its transcript is fully flushed. Measure +
                # stamp the consumed-token total and apply the #73 guardrail
                # (>300k ⇒ a 🔴 halt). Best-effort: a token-accounting failure
                # must never unwind a clean completion.
                try:
                    self._stamp_run_tokens(run_id)
                except Exception:
                    self._log.exception("token stamping failed for run %s", run_id)
                return

            # TIMED_OUT or MANAGER_DEAD — fail with the truthful reason (#119).
            await self.fail_watched_run(
                run_id, session_id,
                timeout_s=timeout_s,
                manager_dead=outcome is PollOutcome.MANAGER_DEAD,
            )
        except asyncio.CancelledError:
            # Cancelled by cancel_run (which writes the cancelled status) or by
            # shutdown — don't touch the record here.
            raise
        finally:
            self._run_watchers.pop(run_id, None)

    def run_timeout_s(self, run_id: str) -> float:
        """The run's per-session deadline in seconds, sourced from its agent's
        ``timeout_minutes`` (the SAME source the watch-pipeline backstop threads
        in), so a never-completing clock/manual run is bounded the same way
        (#119 M1). Falls back to the default when the run or its agent can't be
        resolved, or carries a non-numeric timeout."""
        rec = self.get_run(run_id)
        agent = self._get_agent(rec.get("agent_id")) if rec else None
        timeout_min = (agent or {}).get("timeout_minutes", self._default_timeout_minutes)
        try:
            return float(timeout_min) * 60.0
        except (TypeError, ValueError):
            return float(self._default_timeout_minutes) * 60.0

    async def fail_watched_run(
        self, run_id: str, session_id: str, *, timeout_s: float, manager_dead: bool
    ) -> None:
        """Mark a watched run FAILED past its deadline and release its slot (#119).

        ``manager_dead`` picks a TRUTHFUL reason: an unreachable-manager run is an
        infrastructure fault (NOT "the task is too large / the agent stalled" —
        the exact lying-halt-note class the LaunchOutcome design exists to kill),
        whereas an alive-but-never-finishing session is the genuine timeout. The
        session kill is best-effort (a dead manager can't be reached to kill it,
        which is fine — FAILED already frees the cap slot)."""
        # Only fail a still-running run — a concurrent cancel/halt may have already
        # moved it to a terminal status we must not clobber.
        current = self.get_run(run_id)
        if current is None or current.get("status") != RUNNING:
            return
        try:
            await self._terminal.kill(session_id)
        except Exception:
            self._log.exception("failed to kill timed-out session %s", session_id)
        if manager_dead:
            outcome = (
                "the pty-manager hosting this run became unreachable and did not "
                "recover — an infrastructure fault, not the agent. The run was "
                "not completed; it can be retried once the manager is back."
            )
        else:
            outcome = (
                f"run exceeded its {timeout_s / 60.0:.0f}-minute deadline without "
                "its session ending — marked failed and its concurrency slot released"
            )
        self._log.error("run %s failed: %s", run_id, outcome)
        self.update_run(
            run_id, status=FAILED, outcome=outcome, finished_at=time.time(),
        )

    async def cancel_run(self, run_id: str) -> dict:
        """Stop a running run: kill its tagged session and mark it cancelled.

        Raises KeyError if the run does not exist. A run that already reached a
        terminal status is returned unchanged (cancel is idempotent).
        """
        rec = self.get_run(run_id)
        if rec is None:
            raise KeyError(run_id)
        # Any terminal run is left untouched (cancel is idempotent). HALTED is in
        # this set (issue #117): cancelling an already-halted run must be a no-op,
        # NOT overwrite its "needs a human" report with "cancelled by operator"
        # via a narrow UI race. The run-store transition guard is the backstop if
        # this is ever bypassed.
        if is_terminal(rec.get("status")):
            return rec

        # Stop the watcher first so it can't race us to a succeeded write when
        # the kill makes the session disappear.
        watcher = self._run_watchers.pop(run_id, None)
        if watcher is not None and not watcher.done():
            watcher.cancel()

        session_id = rec.get("session_id")
        if session_id:
            try:
                await self._terminal.kill(session_id)
            except Exception:
                self._log.exception("run %s session kill raised", run_id)

        return self.update_run(
            run_id, status=CANCELLED,
            outcome="cancelled by operator", finished_at=time.time(),
        )
