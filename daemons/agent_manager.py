"""
daemons/agent_manager.py — Standalone agent manager for the dashboard's agent
runner (issue #26, parent #24).

Why this process exists
-----------------------
The agent runner needs to own long-lived agent processes (worktrees, PR-driving
Claude Code sessions) that must survive a dashboard restart — the same lifetime
problem the terminal tab solved by extracting PTY ownership into
pty_manager.py. This module is that extraction's sibling for agents:
a standalone daemon that the dashboard lazy-spawns and talks to over TCP, so
restarting the dashboard never kills a running agent.

This slice is the isolated, restart-surviving FOUNDATION only — not the agent
feature. Its RPC surface is intentionally tiny: ``ping`` and ``list_agents``
(empty for now), plus the graceful ``shutdown`` that every standalone manager
needs. Agent spawn/attach/kill land in later slices.

Architecture
------------
- JSON-lines TCP server on a dedicated port (default 58998 — DISTINCT from the
  pty-manager's 58999 so the two daemons coexist on one host).
- Short-lived connections: request → response → close.
- PID file at logs/agent_manager.pid so the dashboard can find / verify us.
- Rotating log file: logs/agent_manager.log (capped at ~1MB).
- The dashboard respawns us lazily on the next agent API access (see
  agent_client.py), so the graceful shutdown RPC just exits.

Protocol (JSON-lines, one JSON object per line)
-----------------------------------------------
  → {"type":"ping"}
  ← {"type":"pong"}

  → {"type":"list_agents"}
  ← {"type":"agents","agents":[...]}     (agent definitions from the store)

  → {"type":"get_agent","id":"alpha"}
  ← {"type":"agent","agent":{...}}       (or {"agent":null} if missing)

  → {"type":"upsert_agent","agent":{...}}
  ← {"type":"agent","agent":{...}}       (the normalized, persisted definition)
  ← {"type":"error","error":"<reason>"}  (on a validation failure)

  → {"type":"delete_agent","id":"alpha"}
  ← {"type":"deleted","deleted":true}    (false if it did not exist)

  → {"type":"set_enabled","id":"alpha","enabled":false}   (issue #39)
  ← {"type":"agent","agent":{...}}       (the updated, persisted definition)
  ← {"type":"error","error":"<reason>"}  (unknown agent / bad enabled)

  Run lifecycle (issue #30) — a Run is one execution of an agent, hosted as a
  TAGGED pty-manager session (agent:<run-id>) and walked through the status
  machine queued → running → succeeded/failed/cancelled. Records persist in the
  gitignored run store (agents.run_store, agent_runs/).

  → {"type":"run_now","id":"alpha"}
  ← {"type":"run","run":{...}}           (the Run record; status queued/running)
  ← {"type":"error","error":"<reason>"}  (unknown agent / launch failure)
  ← {"type":"error","error":"<reason>","code":"disabled"}  (agent is OFF, #40 —
                                          the manual override is ALSO blocked
                                          because OFF is a hard stop)

  → {"type":"list_runs"}
  ← {"type":"runs","runs":[...]}         (all run records, newest first)

  → {"type":"get_run","run_id":"alpha-7f3c1a"}
  ← {"type":"run","run":{...}}           (or {"run":null} if missing)

  → {"type":"cancel_run","run_id":"alpha-7f3c1a"}
  ← {"type":"run","run":{...}}           (status cancelled; kills the session)
  ← {"type":"error","error":"<reason>"}  (unknown run)

  → {"type":"shutdown"}
  ← {"type":"shutdown","ok":true}
    (graceful full shutdown: the ack is flushed, then the TCP server stops and
     the process exits. The dashboard respawns the manager lazily on the next
     agent API access.)

Usage: python daemons/agent_manager.py [--port 58998] [--workspace C:\\path\\to\\Trident]
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# This daemon lives in <repo>/daemons/ but imports the repo-root `agents` and
# `shared` packages. When launched directly (python daemons/agent_manager.py),
# sys.path[0] is the daemons/ dir, so put the repo root on the path first.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.pipeline import (
    IssuePrPipeline,
    build_claude_command,
    push_branch_to_origin,
    run_pytest_in_worktree,
    watch_enabled,
    WATCH_ENABLED_ENV,
)
from agents.eli_runtime import run_frontend_check
from agents.review_pipeline import (
    DEFAULT_MAX_ROUNDS,
    ReviewPipeline,
)
from agents.eli_pipeline import EliPipeline, eli_enforce_command
from agents.eli_runtime import (
    merge_branch_onto_main,
    push_main,
    rebuild_dashboard_bundle,
    run_full_suite,
)
from agents.feed import (
    default_transcript_base,
    project_dir_for_cwd,
    resolve_transcript_path,
)
from agents.halt import format_halt_report
from agents.run import (
    MANAGER_DEATH_GRACE_S,
    LaunchOutcome,
    PtyTerminalClient,
    build_run_command,
    classify_launch,
    enforce_run_command,
    install_pre_push_hook,
    run_session_with_retry,
)
from agents.tokens import (
    WARN,
    sum_session_tokens,
)
from agents.token_guardrail import (
    TokenGuardrail,
    _issue_from_watch_agent_id,
)
from agents.run_store import (
    QUEUED,
    RUNNING,
    SUCCEEDED,
    FAILED,
    HALTED,
    RunValidationError,
    default_run_store,
)
from agents.run_engine import AgentDisabledError, RunEngine
from agents.scheduler import (
    Candidate,
    DEFAULT_AGENT_PR_CAP,
    DEFAULT_CONCURRENCY_CAP,
    DEFAULT_LOOP_IN_FLIGHT_CAP,
    cron_is_due,
    decide_launches,
    loop_in_flight_count,
    running_watch_issue_numbers,
    should_withhold_loop,
    should_withhold_watch,
    stage_withheld,
    wall_clock_minute_key,
)
from agents.snapshot import AgentStateSnapshot
from agents.status import StatusAggregator
from agents.store import (
    AgentValidationError,
    DEFAULT_TIMEOUT_MINUTES,
    DEFAULT_WATCH_STAGE,
    default_store,
)
from agents.worktree import WorktreeCreator
from agents.json_persist import JsonFlag, JsonSet
from shared.github_gateway import (
    APPROVED_LABEL,
    GitHubGateway,
    NEEDS_HUMAN_LABEL,
    NEEDS_SIGNOFF_LABEL,
    NEEDS_TRADE_APPROVAL_LABEL,
    REJECTED_LABEL,
    TRADE_APPROVED_LABEL,
    format_pm_decision,
)
from agents.trade_surface import (
    classify_paths,
    format_trade_approval_brief,
)
from shared.loop_gate import LoopGateLike, default_loop_gate

# ── constants ─────────────────────────────────────────────────────────────────

# Dedicated port — DISTINCT from pty_manager.DEFAULT_PORT (58999) so the
# agent-manager and pty-manager daemons coexist on this one production host.
DEFAULT_PORT = 58998
# Lift the StreamReader limit so a large request line never trips the default
# 64KB readline wall (matches the pty-manager). Cheap insurance for later
# slices that may carry bigger payloads.
STREAM_LIMIT = 8 * 1024 * 1024  # 8 MB

# ── exceptions ────────────────────────────────────────────────────────────────

# AgentDisabledError (run-now on a DISABLED agent, #40) now lives with the run
# lifecycle in agents.run_engine and is re-exported above, so existing
# ``daemons.agent_manager.AgentDisabledError`` references (the dashboard route, the
# run-engine tests) still resolve here.


# How often the per-run watcher polls the pty session for liveness. The run is
# completed once its tagged session is no longer alive. Cheap (one list RPC).
RUN_POLL_INTERVAL = 2.0  # seconds

# How often the scheduler loop wakes to evaluate clock-triggered agents. 30s is
# fine-grained enough for cron's minute resolution (a due minute is caught
# within 30s) while staying cheap on this production host.
SCHEDULER_TICK_INTERVAL = 30.0  # seconds

# Stale-pre-launch grace period: a manager younger than this won't recycle just
# because a newer commit exists.  Prevents death loops when commits land rapidly
# (e.g. during a coding session or Eli landing a chain of PRs).  The code is
# still "fresh enough" — the manager loaded the repo HEAD at most this many
# seconds ago.  After the grace period, normal stale gating resumes.
STALE_PRE_LAUNCH_GRACE_PERIOD = 300.0  # seconds (5 min)

# Snapshot-warm loop (#151 perf). The consolidated snapshot's gh half
# (pill_counts + loop_health) is ~8 `gh` calls / ~3-5s cold — the agent panel's
# slow path. While the panel is OPEN the dashboard's /ws/agents requests a
# gh-bearing snapshot at least every backstop interval; each request ARMS a
# short warm window here. The warm loop then refreshes the StatusAggregator's gh
# caches every _SNAPSHOT_WARM_INTERVAL (kept under the cache's 10s TTL) while the
# window is live, so the gh snapshot a push fetches reads warm instead of cold.
# The window EXPIRES once requests stop (panel closed / WS dropped), so there is
# ZERO idle gh load when nobody is watching — the refresh is strictly
# demand-scoped.
_SNAPSHOT_WARM_INTERVAL = 7.0   # refresh cadence while armed (< gh cache TTL)
_SNAPSHOT_WARM_WINDOW = 45.0    # how long one snapshot request keeps it warming

# Machine-local persisted master-arm flag for the autonomous watch loop, flipped
# by the dashboard toggle (set_watch_gate). Gitignored — a clone never inherits
# "armed". Lives at the workspace root, same idiom as trident_config.json.
WATCH_GATE_FILENAME = "watch_gate.json"

# Machine-local persisted per-project SUPERVISED-MODE flag (#66), flipped by the
# dashboard toggle (set_supervised). When ON, a Kleiner-clean review parks at
# needs-signoff (the 🟡 PM gate) BEFORE Eli integrates, instead of advancing
# straight to `approved` (full-auto). Gitignored — a clone never inherits it.
# Lives at the workspace root, same idiom as watch_gate.json. Default OFF.
# NOTE: the live effect lands once Kleiner/ReviewPipeline is manager-wired (a
# later slice); today this persists the value and feeds the ReviewPipeline
# `supervised` constructor seam at the point that pipeline IS built.
SUPERVISED_FILENAME = "supervised.json"

# The per-session token guardrail (#73) — its classification + HaltReport decision,
# the per-issue/PRD chain rollups, and the ``watch-issue-<n>`` → issue parse — now
# live in agents.token_guardrail (#142). _issue_from_watch_agent_id is
# imported from there above (re-exported so _discover_prd_children + the tests that
# referenced it on the manager still resolve).

# ── logging ───────────────────────────────────────────────────────────────────

def _setup_logging(workspace: str) -> logging.Logger:
    log_dir = os.path.join(workspace, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "agent_manager.log")

    handler = RotatingFileHandler(
        log_path, maxBytes=1_048_576, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    log = logging.getLogger("agent_manager")
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
# AgentManagerServer — TCP server
# ═══════════════════════════════════════════════════════════════════════════════

class AgentManagerServer:
    def __init__(
        self,
        port: int,
        workspace: str,
        *,
        terminal_client: object | None = None,
        command_builder=None,
        run_cwd: str | None = None,
        concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
        agent_pr_cap: int = DEFAULT_AGENT_PR_CAP,
        loop_in_flight_cap: int = DEFAULT_LOOP_IN_FLIGHT_CAP,
        loop_gate: LoopGateLike | None = None,
    ) -> None:
        self._port = port
        self._workspace = workspace
        # Capture the git HEAD short-SHA at spawn so the dashboard can flag
        # staleness — the agent-manager is a detached daemon that survives the
        # dashboard .bat restart, so a code fix is dead until this process is
        # recycled. The loaded SHA versus the current repo HEAD is the staleness
        # signal (issue #182). Best-effort: if git is missing or the workspace
        # isn't a git repo, _loaded_sha is None and stale is always False.
        try:
            self._loaded_sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self._workspace, text=True,
            ).strip() or None
        except Exception:
            self._loaded_sha = None
        self._loaded_at = time.time()  # for stale-pre-launch grace period
        # Agent DEFINITIONS persist in the gitignored store (one JSON file per
        # agent under <workspace>/agents). The live processes those definitions
        # spawn land in later slices; this slice owns the recipes.
        self._store = default_store(workspace)
        # Run RECORDS persist in the gitignored run store (one JSON file per run
        # under <workspace>/agent_runs). One record per execution of an agent.
        self._run_store = default_run_store(workspace)
        # 🟢 completed-work ack marker (#65): the set of succeeded run_ids the
        # operator has already SEEN via the dashboard's completed view. Persisted
        # as a small gitignored JSON file beside the stores so "clears on viewing"
        # survives a dashboard restart. The green pill counts succeeded runs whose
        # id is NOT in this set; ack_completed() folds the current succeeded ids in.
        # A JsonSet (#140) owns the read/write (atomic, empty-on-missing/corrupt);
        # the manager owns the path and composes the primitive.
        self._completed_ack = JsonSet(Path(workspace) / "completed_ack.json")
        # 🔴 problems ack marker: the set of HALTED run_ids the operator has
        # acknowledged via the run-detail "Acknowledge" button. The red pill
        # counts halted runs whose id is NOT in this set, so acknowledging a halt
        # clears it from the badge while the run stays listed (marked ✓) in the
        # problems view. Separate from the green set so the two never collide.
        self._problems_ack = JsonSet(Path(workspace) / "problems_ack.json")
        self._lock = threading.Lock()
        self._log = logging.getLogger("agent_manager")
        # Set in start(); the shutdown RPC closes it to unblock serve_forever.
        self._server: asyncio.AbstractServer | None = None

        # ── run-engine injection seams (issue #30) ────────────────────────────
        # The terminal client (pty-manager wire) and the command builder are
        # injectable so the run-lifecycle tests can fake the terminal — no real
        # ConPTY spawns in the suite. The run cwd is a plain dir for this slice;
        # #35 (worktree) will wrap it.
        self._terminal = terminal_client or PtyTerminalClient()
        self._build_command = command_builder or build_run_command
        self._run_cwd = run_cwd or workspace

        # ── run lifecycle engine (issue #143) ─────────────────────────────────
        # The meatiest peel: the whole queued→running→succeeded/failed/cancelled
        # lifecycle (run_now, the watcher + its task registry, run-timeout, fail,
        # cancel, and the run-store CRUD wrappers) lives in RunEngine. The CORE
        # keeps the SHARED run store + lock (passed in) and the scheduler loop; the
        # engine reaches none of the scheduler's tasks and owns its OWN watcher
        # registry. The _cmd_* / CRUD methods below delegate to it.
        #
        # Seams a test reassigns on the MANAGER after construction are read through
        # a provider (a thunk) so the live watcher resolves the current value:
        #   • run_timeout_provider → the manager's _run_timeout_s (which delegates
        #     back to the engine by default; tests override the manager attr).
        #   • poll/grace providers → the RUN_POLL_INTERVAL / MANAGER_DEATH_GRACE_S
        #     MODULE globals (the suite monkeypatches them on this module).
        #   • stamp_run_tokens → the manager's _stamp_run_tokens (evaluate + apply
        #     the #73/#142 guardrail at run-finish; tests reassign it).
        self._run_engine = RunEngine(
            run_store=self._run_store,
            lock=self._lock,
            terminal=self._terminal,
            build_command=self._build_command,
            run_cwd=self._run_cwd,
            get_agent=self._get_agent,
            default_timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
            run_timeout_provider=lambda run_id: self._run_timeout_s(run_id),
            poll_interval_provider=lambda: RUN_POLL_INTERVAL,
            manager_death_grace_provider=lambda: MANAGER_DEATH_GRACE_S,
            stamp_run_tokens=lambda run_id: self._stamp_run_tokens(run_id),
            logger=self._log,
        )
        # agent_id → asyncio.Task running that watch agent's pipeline launch in the
        # BACKGROUND (#114). A watch run can run for the FULL length of a coding
        # session (30+ min); awaiting it inline in the scheduler tick suspended the
        # whole loop for that time — the dashboard pulse froze, the proactive
        # pty-watchdog never fired, and clock agents couldn't launch (the #95/#96
        # freeze). Backgrounding the launch lets the tick return promptly and the
        # loop keep ticking. Tracked here so shutdown can drain them (no orphaned
        # tasks) and so the failure done-callback can drop the fire-ledger mark for
        # a retry. The strict-serial invariant stays with the existing
        # candidate-gating (_due_watch_candidates), unchanged.
        self._watch_launch_tasks: dict[str, asyncio.Task] = {}

        # ── scheduler seam (issue #33) ────────────────────────────────────────
        # The GLOBAL concurrency cap (at most this many runs in flight) and the
        # background loop task that drives clock-triggered agents. The loop is a
        # thin driver over the PURE decide_launches core; both are testable
        # process-free because _scheduler_tick takes its launcher injected.
        #
        # NB (#64): this cap is NOT the watch/issue→PR loop's concurrency. That
        # loop is strictly SERIAL — one issue in flight across the whole
        # Gordon → Kleiner → Eli → merged cycle — enforced structurally by the
        # in-flight gate in _due_watch_candidates, not by a tunable. The cap stays
        # at 2 only because clock/report cron agents legitimately run a couple in
        # parallel; it does NOT govern (and must not be forced to 1 for) the
        # serial issue→PR loop.
        self._concurrency_cap = concurrency_cap
        self._scheduler_task: asyncio.Task | None = None
        # Snapshot-warm loop (#151 perf): the task handle + the monotonic
        # deadline until which the gh caches should be kept hot. 0.0 = idle (no
        # panel open). _arm_snapshot_warm() pushes the deadline forward on every
        # gh-bearing snapshot request; the loop refreshes while now < deadline.
        self._snapshot_warm_task: asyncio.Task | None = None
        self._snapshot_warm_until: float = 0.0
        # Monotonic count of scheduler ticks — bumped once per real loop check
        # (every SCHEDULER_TICK_INTERVAL). The dashboard reads it via pulse_status
        # and flashes the Claude-tab AGENTS button each time it advances, so the
        # glow pulses on an ACTUAL "agent checks for work" beat, not a UI timer.
        self._scheduler_tick_seq = 0
        # 🆕 change-epoch (#150): monotonic counter bumped at every agent-visible
        # state mutation — run created/updated/halted, scheduler tick, completed-ack,
        # problems-ack. Generalizes tick_seq to all state changes. Pure reads never
        # bump it. Exposed via pulse_status so a caller can ask "did anything change?"
        # extremely cheaply (no GitHub calls, no disk walk).
        self._change_epoch = 0
        # Auto-recycle staleness persistence (#198): the tick_seq at which stale
        # was first observed, or None if not stale. Reset when staleness clears.
        # _maybe_auto_recycle requires ≥2 ticks of staleness before recycling,
        # so a brief `git checkout` during manual work doesn't flap.
        self._stale_since_tick_seq: int | None = None
        # agent_id → the last clock minute we already fired it for, so a clock
        # agent fires AT MOST once per due minute even though the loop ticks
        # several times within that minute. None until first fire.
        self._last_clock_fire: dict[str, float] = {}

        # ── watch trigger seam (issue #35) ────────────────────────────────────
        # agent_id → the last wall-clock time we polled GitHub for that watch
        # agent, so each watch agent is polled at most once per its poll_interval.
        # Autonomous watch processing is GATED behind watch_gate_on() (the
        # persisted dashboard toggle, OFF by default): the tick skips ALL watch
        # agents and logs loudly until Nathan arms the toggle. The legacy env
        # var TRIDENT_AGENT_WATCH_ENABLED no longer gates anything (dropped
        # 2026-06-23, rogue-watcher incident).
        self._last_watch_poll: dict[str, float] = {}
        # Injectable factory so the loop tests can drive watch firing with a fake
        # pipeline — never a real worktree / claude / gh / push. Production builds
        # a real IssuePrPipeline (gateway + worktree + hook + claude + pytest).
        self._pipeline_factory = self._build_real_pipeline
        # Per-stage pipeline factories (#78) — the manager picks one in
        # _launch_watch_run by the agent's trigger.stage: claim→Gordon (above),
        # review→Kleiner, land→Eli. Each is injectable so the loop tests assert the
        # RIGHT factory is chosen without ever spawning a real pipeline.
        self._review_pipeline_factory = self._build_real_review_pipeline
        self._eli_pipeline_factory = self._build_real_eli_pipeline
        # One-shot loud log when watch is disabled, so the gate state is obvious
        # in the manager log without spamming every tick.
        self._watch_disabled_logged = False

        # ── cross-PR cap seam (issue #53) ─────────────────────────────────────
        # At most this many OPEN agent-authored PRs may be in flight before
        # candidate-gathering withholds NEW watch runs, so each run branches from
        # a main that already includes the prior merge. The counter is INJECTABLE
        # so the loop tests never shell out to `gh pr list`; production counts via
        # the gateway (_count_open_agent_prs).
        self._agent_pr_cap = agent_pr_cap
        self._loop_in_flight_cap = loop_in_flight_cap
        self._open_agent_pr_counter = self._count_open_agent_prs

        # ── strict-serial loop seam (issue #64) ───────────────────────────────
        # The whole issue→PR loop runs ONE issue at a time: a new claim is
        # withheld while ANY issue is in flight, where "in flight" = an issue
        # carrying a mid-pipeline label (agent-working / needs-review / approved)
        # OR a locally-running watch run. The counter is INJECTABLE (same idiom as
        # _open_agent_pr_counter) so the loop tests never shell out to `gh`;
        # production counts via the gateway listers + the run store.
        self._loop_in_flight_counter = self._count_loop_in_flight

        # ── architecture-freeze gate seam (issue #62 built it, #64 wires it) ───
        # The durable freeze sentinel (loop_freeze.json) Eli writes on scope
        # drift. INDEPENDENT of the watch toggle: the watch toggle is "Nathan
        # paused", the freeze is "Eli stopped on scope drift" — neither clears the
        # other. Injectable for tests; production roots it at the workspace.
        self._loop_gate: LoopGateLike = loop_gate or default_loop_gate(self._workspace)
        # One-shot loud log when the loop is frozen, mirroring _watch_disabled_logged.
        self._loop_frozen_logged = False

        # ── gateway seam (Slice C: PM approve/reject) ─────────────────────────
        # A callable returning a GitHub gateway. Production calls the real
        # constructor (shells out to `gh`); tests inject a fake factory so the
        # suite never touches GitHub. Same injectable-seam idiom as
        # _open_agent_pr_counter above.
        self._gateway_factory = GitHubGateway

        # ── token-accounting seams (issue #73) ────────────────────────────────
        # At run-complete the MANAGER measures the session's consumed-token total
        # from its transcript and stamps it on the run record (the agent can't
        # reliably read its own running total — #73). Two injectable seams keep
        # the suite off real transcripts:
        #   _session_token_counter — run_id → int (resolve the run's transcript
        #     from its cwd + session_id, then sum). Tests inject a fake that
        #     returns a canned total, so no real ~/.claude/projects read happens.
        #   _transcript_base — the ~/.claude/projects root the resolver reads
        #     under; injectable so a test could point it at a tmp fixture dir.
        # Same injectable-seam idiom as _open_agent_pr_counter / _gateway_factory.
        self._transcript_base = default_transcript_base()
        self._session_token_counter = self._count_session_tokens

        # ── token guardrail (issue #142, verdict-returning) ───────────────────
        # The per-session guardrail DECISION (classify ok/warn/over + build the
        # over-ceiling HaltReport) + the per-issue/PRD chain rollups live in
        # TokenGuardrail. The manager composes it and APPLIES its verdicts:
        # evaluate() performs no gh write and no store transition, so the manager's
        # _stamp_run_tokens stamps the total + (on over-ceiling) runs mark_halted +
        # the needs-human label — every run-state transition still funnels through
        # the run store's guard_transition (HALTED stays sticky, #117). The counter
        # is passed as a PROVIDER (not a snapshot) because tests reassign
        # _session_token_counter after construction — same indirection idiom as the
        # StatusAggregator gateway-factory provider.
        self._token_guardrail = TokenGuardrail(
            list_runs=self._list_runs,
            token_counter_provider=lambda: self._session_token_counter,
            logger=self._log,
        )

        # ── master watch-gate arm state (dashboard toggle) ────────────────────
        # The autonomous issue→PR loop is armed by a PERSISTED, machine-local flag
        # (watch_gate.json, gitignored) the dashboard flips via set_watch_gate —
        # no env var, no restart. Loaded once here; the scheduler reads the live
        # in-memory value each tick. The dashboard toggle is the SOLE gate: the
        # legacy TRIDENT_AGENT_WATCH_ENABLED env var no longer arms anything (the
        # OR was dropped 2026-06-23 — a stale env force-on could override an OFF
        # toggle and arm the loop nobody armed; the rogue-watcher incident,
        # footgun #176). Default DISARMED.
        # A JsonFlag (#140) owns the read/write (atomic, default-on-missing/corrupt
        # ⇒ DISARMED so the gate never fails OPEN); the manager owns the path.
        self._watch_gate_store = JsonFlag(
            os.path.join(self._workspace, WATCH_GATE_FILENAME), default=False)
        self._watch_gate_enabled = self._watch_gate_store.load()

        # ── supervised-mode arm state (dashboard toggle, #66) ──────────────────
        # Persisted, machine-local per-project flag (supervised.json, gitignored).
        # ON ⇒ a Kleiner-clean review parks at needs-signoff (🟡 PM gate) before
        # Eli lands; OFF ⇒ full-auto. Loaded once here; fed into the ReviewPipeline
        # `supervised` seam wherever that pipeline is constructed (Kleiner is not
        # manager-wired yet — see SUPERVISED_FILENAME). Default OFF.
        # A JsonFlag (#140) owns the read/write (atomic, default-on-missing/corrupt
        # ⇒ OFF so supervised never arms itself); the manager owns the path.
        self._supervised_store = JsonFlag(
            os.path.join(self._workspace, SUPERVISED_FILENAME), default=False)
        self._supervised_enabled = self._supervised_store.load()

        # ── status view-model assembler (issue #141) ──────────────────────────
        # The dashboard's pill-counts / loop-status / pulse view-models are
        # GATHERED (gh + store reads) then DERIVED (the pure scheduler functions).
        # That gather flow lives in StatusAggregator; the manager composes it once
        # and its _cmd_* handlers delegate. The gateway factory + loop gate are
        # passed as PROVIDERS (not snapshots) because tests reassign
        # _gateway_factory / _loop_gate after construction, and the watch-arm +
        # tick-seq readers are thunks because both change at runtime — the
        # aggregator must resolve the manager's CURRENT value on each call.
        self._status = StatusAggregator(
            list_runs=self._list_runs,
            gateway_factory_provider=lambda: self._gateway_factory,
            loop_gate_provider=lambda: self._loop_gate,
            completed_ack=self._completed_ack,
            problems_ack=self._problems_ack,
            run_issue_number=self._run_issue_number,
            watch_gate_on=self.watch_gate_on,
            count_running_stage_runs=self._count_running_stage_runs,
            scheduler_tick_seq=lambda: self._scheduler_tick_seq,
            change_epoch=lambda: self._change_epoch,
            agent_pr_cap=self._agent_pr_cap,
            loop_in_flight_cap=self._loop_in_flight_cap,
            logger=self._log,
        )
        self._snapshot = AgentStateSnapshot(
            status_aggregator=self._status,
            list_runs=self._list_runs,
            watch_gate_status=self.watch_gate_status,
            supervised_status=self.supervised_status,
            staleness_status=self.staleness,
            logger=self._log,
        )

    # ── agent registry (store-backed) ─────────────────────────────────────────
    # All store access is serialized under _lock: the store is one-file-per-agent
    # on disk, and concurrent connections could otherwise interleave a read with
    # an upsert/delete of the same file.

    def _list_agents(self) -> list[dict]:
        with self._lock:
            return self._store.list()

    def _get_agent(self, agent_id: str) -> dict | None:
        with self._lock:
            return self._store.get(agent_id)

    def _upsert_agent(self, defn: dict) -> dict:
        """Validate + persist. Raises AgentValidationError on a bad definition."""
        with self._lock:
            return self._store.upsert(defn)

    def _delete_agent(self, agent_id: str) -> bool:
        with self._lock:
            return self._store.delete(agent_id)

    def _set_enabled(self, agent_id: str, enabled: bool) -> dict | None:
        """Flip an agent's enabled flag (issue #39). None if it doesn't exist."""
        with self._lock:
            return self._store.set_enabled(agent_id, enabled)

    # ── run registry (store-backed) ───────────────────────────────────────────
    # Same _lock as the agent store: both are one-file-per-record on disk and a
    # run watcher's status write must not interleave with a concurrent get/list.

    # ── run store CRUD (delegated to the RunEngine, #143) ─────────────────────
    # The lock-held passthroughs over the SHARED run store live in the engine now;
    # these thin delegators keep the manager's (and the recorders') existing call
    # sites — self._mgr._get_run / _update_run / _mark_halted etc. — resolving
    # here unchanged. The lock is the SAME object the core owns and injected.

    def _list_runs(self) -> list[dict]:
        return self._run_engine.list_runs()

    @staticmethod
    def _run_issue_number(rec: dict) -> int | None:
        """Best-effort: the GitHub issue number a run is tied to, for deduping a
        halted run against its own needs-human label in the red pill. Parsed from
        the agent_id (watch agents are ``<stage>-issue-<N>``) first, then the halt
        outcome (``… on issue #<N>``) as a fallback. None if neither matches."""
        m = re.search(r"issue-(\d+)", rec.get("agent_id") or "")
        if m:
            return int(m.group(1))
        m = re.search(r"#(\d+)", rec.get("outcome") or "")
        if m:
            return int(m.group(1))
        return None

    def _get_run(self, run_id: str) -> dict | None:
        return self._run_engine.get_run(run_id)

    def _put_run(self, rec: dict) -> dict:
        result = self._run_engine.put_run(rec)
        self._bump_epoch()
        return result

    def _update_run(self, run_id: str, **changes) -> dict:
        result = self._run_engine.update_run(run_id, **changes)
        self._bump_epoch()
        return result

    def _mark_halted(self, run_id: str, note: str | None = None,
                     finished_at: float | None = None) -> dict:
        """Persist a run as HALTED (Slice F escape hatch) via the run store's
        ``mark_halted``. Delegates to the engine's lock-held wrapper."""
        result = self._run_engine.mark_halted(run_id, note=note, finished_at=finished_at)
        self._bump_epoch()
        return result

    # ── run lifecycle (issue #30, extracted to RunEngine in #143) ─────────────
    # The whole queued→running→succeeded/failed/cancelled walk — manual launch,
    # the watcher + its task registry, run-timeout resolution, fail, and cancel —
    # lives in self._run_engine now. These delegators keep the _cmd_* handlers'
    # and the scheduler's existing call sites resolving here.

    async def _run_now(self, agent_id: str) -> dict:
        """Resolve the agent, create a Run, launch it as a tagged terminal, and
        return the Run record. Delegates to the RunEngine; raises KeyError
        (unknown agent) / AgentDisabledError (OFF, #40) exactly as before, which
        the run-now command handler maps to 404 / 409."""
        return await self._run_engine.run_now(agent_id)

    def _run_timeout_s(self, run_id: str) -> float:
        """The run's per-session deadline in seconds (#119 M1). Delegates to the
        RunEngine. Kept as a manager method because the watcher resolves it
        through a provider that reads THIS attribute, so the suite can override
        the deadline by reassigning ``mgr._run_timeout_s``."""
        return self._run_engine.run_timeout_s(run_id)

    async def _cancel_run(self, run_id: str) -> dict:
        """Stop a running run: kill its tagged session and mark it cancelled.
        Delegates to the RunEngine; raises KeyError if the run does not exist."""
        return await self._run_engine.cancel_run(run_id)

    # ── scheduler loop (issue #33) ─────────────────────────────────────────────
    # The loop drives CLOCK-triggered agents: on each tick it finds the agents
    # whose schedule is due, hands them (plus the live in-flight set and the cap)
    # to the PURE decide_launches core, and launches whatever it returns —
    # oldest-first, cap-respecting, in-flight-excluded. Manual run-now is a
    # SEPARATE path (_run_now) that never consults the scheduler, so it always
    # works regardless of loop state. The `watch` trigger (poll GitHub) is NOT
    # here — slice #35 will add watch agents as candidates to the same core.

    def _in_flight_agent_ids(self) -> set[str]:
        """Agent ids with a run currently running OR queued — the live
        concurrency the cap is measured against and the in-flight exclusion set.
        Reads the run store (the durable source of truth), so it survives across
        ticks and a manager restart."""
        return {
            r["agent_id"]
            for r in self._list_runs()
            if r.get("status") in (RUNNING, QUEUED)
        }

    def _due_clock_candidates(self, now: float) -> list[Candidate]:
        """The clock-triggered agents whose schedule is due at ``now`` and that
        we have NOT already fired for this exact due-minute. Each becomes a
        ready Candidate (ready_since = the due minute, so older due-times launch
        first under the cap). Pure-ish: only reads the store + the in-memory
        fire ledger; no launching here."""
        minute_key = wall_clock_minute_key(now)  # local wall-clock minute (#116, M5)
        out: list[Candidate] = []
        for agent in self._list_agents():
            if agent.get("enabled", True) is False:
                continue  # OFF is a hard stop (#40) — a disabled clock agent never fires
            trigger = agent.get("trigger") or {}
            if trigger.get("kind") != "clock":
                continue  # watch agents are slice #35's job, not the clock loop
            schedule = trigger.get("schedule") or ""
            if not cron_is_due(schedule, now):
                continue
            agent_id = agent.get("id")
            if self._last_clock_fire.get(agent_id) == minute_key:
                continue  # already fired this agent for this due-minute
            out.append(Candidate(agent_id=agent_id, ready_since=minute_key))
        return out

    def _count_open_agent_prs(self) -> int:
        """Production counter for the #53 cross-PR cap: how many OPEN agent
        PRs exist, via the gateway (`gh pr list`, head in the agent/ namespace).
        Tests inject a fake ``self._open_agent_pr_counter`` so the suite never
        shells out to gh."""
        return GitHubGateway().count_open_agent_prs()

    def _count_loop_in_flight(self) -> int:
        """Production counter for the #64 strict-serial gate: how many DISTINCT
        issues are in flight in the issue→PR loop right now.

        "In flight" = an issue carrying ANY mid-pipeline label (agent-working,
        needs-review, approved) OR a locally-running watch run. The two sources
        are DEDUPED by issue number: an issue with both a label AND a running
        run (the common case — Gordon applies ``agent-working`` then records a
        running run) counts as ONE, not two.

        The mid-pipeline labels supply issue numbers via gateway listers; the
        local watch runs supply issue numbers parsed from their agent_id
        (``watch-issue-N`` / ``review-issue-N`` / ``land-issue-N``). The pure
        ``loop_in_flight_count`` takes the union. Tests inject a fake
        ``self._loop_in_flight_counter`` so the suite never shells out to gh."""
        gw = self._gateway_factory()
        pipeline_issue_nums: set[int] = set()
        for issue in (
            gw.list_working_issues()
            + gw.list_needs_review_issues()
            + gw.list_approved_issues()
        ):
            try:
                pipeline_issue_nums.add(int(issue.number))
            except (TypeError, ValueError):
                pass

        running_issue_nums = running_watch_issue_numbers(self._list_runs())
        return loop_in_flight_count(
            pipeline_issue_nums=pipeline_issue_nums,
            running_watch_issue_nums=running_issue_nums,
        )

    def _count_running_watch_runs(self) -> int:
        """How many issue→PR (watch) runs are RUNNING locally per the run store —
        a run that has claimed an issue but may not have applied a GitHub label
        yet. Watch runs are recorded with an ``agent_id`` of ``watch-issue-<n>``
        (see _RunStoreRecorder), so they are distinguishable from clock/manual
        runs. Reads the durable run store; no gh."""
        return self._count_running_stage_runs("watch-issue-")

    def _count_running_stage_runs(self, prefix: str) -> int:
        """How many runs whose ``agent_id`` starts with ``prefix`` are RUNNING
        locally per the run store (#78). Generalizes ``_count_running_watch_runs``
        to the per-stage overlap guard: ``watch-issue-`` (Gordon / claim),
        ``review-issue-`` (Kleiner), ``land-issue-`` (Eli). Reads the durable run
        store; no gh. Used by ``_due_watch_candidates`` to never launch a second
        same-stage run while one is already in flight (cheap overlap protection on
        top of each pipeline's own cheap no-op select)."""
        return sum(
            1
            for r in self._list_runs()
            if r.get("status") == RUNNING
            and str(r.get("agent_id", "")).startswith(prefix)
        )

    def _bump_epoch(self) -> None:
        """Advance the change-epoch — called at every mutation that changes
        agent-visible state (#150). Safe under the GIL (int += 1 is atomic
        in CPython); all mutation paths run on the asyncio event-loop thread."""
        self._change_epoch += 1

    def pulse_status(self) -> dict:
        """Cheap, gh-free AGENTS-tab glow signal — delegates to StatusAggregator
        (#141). See ``StatusAggregator.pulse_status`` for the contract."""
        s = self._status.pulse_status()
        s["epoch"] = self._change_epoch
        return s

    def snapshot(self, sections: str = "full") -> dict:
        """The consolidated agent-panel view-model — delegates to
        AgentStateSnapshot (#149). ``sections`` selects which half to gather:
        ``"full"`` (all six keys), ``"cheap"`` (the in-memory half — runs, pulse,
        watch_gate, supervised), or ``"gh"`` (the GitHub-derived half —
        pill_counts + loop_health). The live push fetches ``cheap`` then ``gh``
        so the agent cards render before the slow gh reads land (#151).
        Per-section fail-soft.

        The ``full``/``gh`` paths are offloaded to a thread by the caller for the
        same reason as pill_counts / loop_status: pill_counts() and loop_status()
        each make several gh calls (~3 + ~5), and running them inline would block
        the single-threaded async server for the combined ~8 gh round-trips."""
        return self._snapshot.snapshot(sections)

    def _arm_snapshot_warm(self) -> None:
        """Extend the snapshot-warm window — called on every gh-bearing snapshot
        request so the warm loop keeps the gh caches hot while the panel polls.
        Cheap (a monotonic stamp); see _run_snapshot_warm_loop / the
        _SNAPSHOT_WARM_* constants for the demand-scoped design."""
        self._snapshot_warm_until = time.monotonic() + _SNAPSHOT_WARM_WINDOW

    # ── token accounting (issue #73) ───────────────────────────────────────────
    # Measurement belongs to the MANAGER, fired at run-complete: a headless agent
    # can't reliably read its own running total, and Eli can't measure its own
    # still-running session. The manager resolves the run's transcript (cwd +
    # session_id, via the #32 feed resolver — the SAME path the dashboard run feed
    # uses) and sums it, then stamps `tokens` on the record and applies the
    # per-session guardrail (>300k ⇒ a 🔴 halt + a needs-human "what happened"
    # report).
    #
    # SCOPE — this guardrail is POST-HOC ONLY (#121, L7). It fires once, at
    # run-complete, AFTER the run is already terminal: it can LABEL a runaway
    # session (and file a report) but it cannot STOP one. There is no mid-flight
    # token kill — a session that re-ingests its own transcript can burn far more
    # than the 300k ceiling before it ends, and nothing here interrupts it. The
    # real mid-flight cap is the per-run 30-minute wall-clock deadline (_watch_run
    # / _run_timeout_s, #119), which bounds wall time, NOT tokens. Treat the token
    # guardrail as a post-mortem flag, not a budget enforcer. A true mid-run
    # token-kill is explicitly OUT of #121 — promote it to a feature if wanted.

    def _count_session_tokens(self, run_id: str) -> int | None:
        """Production token counter (the injectable seam's default). Resolve the
        run's transcript from its pinned ``cwd`` + ``session_id`` and sum its
        consumed tokens. Returns None when the transcript can't be located (a
        clearly-marked UNMEASURED sentinel — we never fabricate a number).

        Transcript locatability (the #73 load-bearing unknown): a watch run pins
        its worktree as ``cwd`` (``_RunStoreRecorder.worktree_ready``,
        [[footguns#171]]), and Claude Code writes exactly ONE transcript under
        that worktree's project-slug dir, so even with no ``session_id`` pinned
        the resolver's newest-``*.jsonl`` fallback lands on this run's transcript
        — the same resolution the dashboard feed already relies on. Tests NEVER
        call this (they inject a fake ``_session_token_counter``)."""
        rec = self._get_run(run_id)
        if rec is None:
            return None
        cwd = rec.get("cwd") or ""
        if not cwd:
            return None
        session_id = rec.get("session_id")
        # Isolation guard (2026-06-23): we can only trust the transcript we resolve
        # when it is unambiguously THIS run's. A run pinned to a dedicated worktree
        # (Gordon) has a unique project-slug dir with exactly one transcript, so the
        # resolver's newest-jsonl fallback is correct. But a run that shares the main
        # workspace cwd (Kleiner / Eli / a fixer) drops into the SAME project dir as
        # the live human session + every other agent, and with no session_id pinned
        # the newest-jsonl fallback grabs whatever transcript wrote last — which on
        # the #81 run was THIS chat (32.2M output-inflated tokens), not the agent's
        # own session. Refuse to measure that case (UNMEASURED ⇒ no guardrail) rather
        # than halt the loop on a stranger's transcript. Such agents are bounded by
        # their per-run timeout instead. (A pinned session_id makes even a shared cwd
        # unambiguous, so it is still measured.)
        if not session_id:
            try:
                same_dir = Path(cwd).resolve() == Path(self._run_cwd).resolve()
            except Exception:
                same_dir = False
            if same_dir:
                self._log.info(
                    "run %s shares the main workspace cwd with no session_id — "
                    "UNMEASURED (can't isolate its own transcript)", run_id)
                return None
        pdir = project_dir_for_cwd(self._transcript_base, cwd)
        path = resolve_transcript_path(pdir, session_id)
        if path is None:
            return None
        try:
            return sum_session_tokens(path)
        except Exception:
            self._log.exception("could not sum tokens for run %s", run_id)
            return None

    def _stamp_run_tokens(self, run_id: str, *, issue_number: int | None = None) -> int | None:
        """Measure + stamp a completed run's consumed-token total, then APPLY the
        per-session guardrail's verdict. Returns the measured total (or None if
        unmeasured).

        The DECISION (classify ok/warn/over + build the over-ceiling HaltReport) is
        the ``TokenGuardrail.evaluate`` verdict — pure, no gh write, no store
        transition (#142). This method is the APPLY half: it stamps the total and,
        on an over-ceiling verdict, runs ``mark_halted`` + the needs-human
        label/comment, so every run-state transition still funnels through the run
        store's ``guard_transition`` (HALTED stays sticky, #117).

        POST-HOC ONLY: this runs at run-complete, after the run is already
        terminal, so the guardrail can LABEL a runaway session but cannot STOP
        one mid-flight (#121, L7). A session can exceed the 300k ceiling many
        times over before it ends; the only mid-flight bound is the per-run
        wall-clock deadline (#119), not a token budget.

        Called by the run-completion paths (the clock/manual watcher and the watch
        recorder's finish/halt) AFTER the run reached a terminal status, so the
        transcript is fully flushed. Steps:

          1. Evaluate via the guardrail (counter None ⇒ unmeasured verdict ⇒ skip
             silently — an unmeasurable transcript must NEVER block a completion).
          2. Stamp ``tokens=N`` on the record (best-effort; a stamp failure logs
             but never raises into the completion path).
          3. ``warn`` (200k–300k) is LOGGED only; ``over`` (>300k) applies the halt
             via :meth:`_apply_token_halt`. ``issue_number`` (when known) ties the
             filed report to the issue so the PM sees which issue ran away."""
        rec = self._get_run(run_id)
        if rec is None:
            return None
        verdict = self._token_guardrail.evaluate(rec, issue_number=issue_number)
        if not verdict.measured:
            self._log.info("run %s tokens UNMEASURED (no transcript) — skipping guardrail", run_id)
            return None

        try:
            self._update_run(run_id, tokens=int(verdict.tokens))
        except RunValidationError:
            self._log.exception("could not stamp tokens on run %s", run_id)

        if verdict.classification == WARN:
            self._log.warning(
                "run %s used %d tokens (>%d target, within ceiling) — accepted",
                run_id, verdict.tokens, 200_000)
        elif verdict.over_ceiling:
            self._apply_token_halt(verdict)
        return verdict.tokens

    def _apply_token_halt(self, verdict) -> None:
        """Apply an over-ceiling :class:`~agents.token_guardrail.TokenVerdict`
        — raise a 🔴 halt + auto-file a needs-human "what happened" report. Reuses
        the EXISTING halt machinery (``mark_halted`` for the run record's terminal
        state + the gateway's needs-human label/comment), NOT a new system.

        This is the APPLY half of the verdict-returning split (#142): the guardrail
        DECIDED (classification + the HaltReport + the needs-human comment, all pure
        data on the verdict); this method does the side-effects the guardrail
        deliberately does not — the ``mark_halted`` store transition (through the
        run store's ``guard_transition``) and the gh label/comment.

        This is a POST-HOC label, NOT an interruption (#121, L7): the run already
        finished and burned the tokens by the time we get here — the "halt" only
        flags it for review, it never stopped the overspend.

        The run is ALREADY terminal here (this fires at run-complete), so the halt
        is recorded as the run's outcome and surfaces on the dashboard's red
        "needs your call" pill via the run store. When the run is tied to an issue
        (``verdict.issue_number`` is not None), the report is also filed on the
        issue (needs-human label + a 🤖 provenance comment) so the PM can read which
        agent on which issue ran away, with the session token count. Every gateway
        touch is best-effort: a token-accounting filing must never crash a
        completion."""
        report = verdict.halt_report
        run_id = verdict.run_id
        rec = self._get_run(run_id) or {}
        self._log.warning("TOKEN HALT — run %s (%s): %s",
                          run_id, report.agent_name, report.reason)

        # Mark the run halted (best-effort — the run is already terminal, this
        # overlays the token-halt outcome so the dashboard red pill picks it up).
        try:
            self._mark_halted(run_id, note=format_halt_report(report),
                              finished_at=rec.get("finished_at") or time.time())
        except RunValidationError:
            self._log.exception("could not mark token-halt on run %s", run_id)

        # File the needs-human report on the issue when we can tie one to it.
        issue_no = verdict.issue_number
        if issue_no is None:
            return
        try:
            gw = self._gateway_factory()
            try:
                gw.set_labels(issue_no, add=[NEEDS_HUMAN_LABEL])
            except Exception:
                self._log.warning("could not apply needs-human to #%d (token halt)", issue_no)
            try:
                gw.leave_bot_comment(issue_no, report.agent_name, verdict.needs_human_comment)
            except Exception:
                self._log.warning("could not leave token-halt comment on #%d", issue_no)
        except Exception:
            self._log.warning("token-halt filing on #%s failed (gateway error)", issue_no,
                              exc_info=True)

    def issue_chain_tokens(self, issue_number: int) -> list[tuple[str, int]]:
        """The per-session ``(agent_label, tokens)`` breakdown for one issue's chain
        (#73). Delegates to ``TokenGuardrail.issue_chain_tokens`` (#142); kept as a
        public manager method so existing callers + tests still resolve it here."""
        return self._token_guardrail.issue_chain_tokens(issue_number)

    def issue_chain_total(self, issue_number: int) -> int:
        """The summed consumed-token total for one issue's chain (#73). Delegates to
        ``TokenGuardrail.issue_chain_total`` (#142)."""
        return self._token_guardrail.issue_chain_total(issue_number)

    # ── master watch-gate (the dashboard ARM toggle) ───────────────────────────
    # The persisted arm flag (watch_gate.json) is read/written through the
    # _watch_gate_store JsonFlag (#140) — atomic write, DISARMED on a missing or
    # corrupt file so the gate never fails OPEN. The manager owns the in-memory
    # _watch_gate_enabled and the path; the primitive owns the disk I/O.

    def watch_gate_on(self) -> bool:
        """The EFFECTIVE arm state the scheduler gates on: the persisted dashboard
        toggle (watch_gate.json, set via set_watch_gate) is now the ONLY thing
        that arms the autonomous loop.

        The legacy env force-on (TRIDENT_AGENT_WATCH_ENABLED) was DROPPED
        2026-06-23: a stale env var could override an OFF toggle, arming the
        scheduler nobody armed — the rogue-watcher incident where the watch loop
        autonomously grabbed issues. The toggle is authoritative; the env var no
        longer gates anything (watch_enabled() survives as an informational
        reader only, surfaced by watch_gate_status)."""
        return self._watch_gate_enabled

    def watch_gate_status(self) -> dict:
        """The arm state for the dashboard. Shape is fixed (the frontend +
        test_agent_routes.py depend on these keys):

        - ``enabled``      — the persisted dashboard toggle (the sole control).
        - ``env_override`` — DEPRECATED: True means the legacy env var is set but
          IGNORED — informational only, it does NOT override or arm anything.
        - ``effective``    — what the scheduler gates on; now equals ``enabled``.
        """
        return {
            "enabled": self._watch_gate_enabled,
            "env_override": watch_enabled(),
            "effective": self.watch_gate_on(),
        }

    def set_watch_gate(self, enabled: bool) -> dict:
        """Arm/disarm the autonomous loop (persisted + live). Returns the new
        status. Takes effect on the NEXT scheduler tick — no restart."""
        self._watch_gate_enabled = bool(enabled)
        self._watch_gate_store.save(self._watch_gate_enabled)
        # Reset the one-shot disabled-log latch so a disarm logs loudly again.
        self._watch_disabled_logged = False
        if enabled:
            # Arm → clear all poll timers so every watch agent fires on the
            # very next scheduler tick instead of waiting up to poll_interval
            # (120s).  The per-stage gates still filter correctly — only the
            # appropriate agent (usually Gordon) passes through.
            self._last_watch_poll.clear()
        self._log.warning(
            "watch gate %s via toggle (effective=%s)",
            "ARMED" if self._watch_gate_enabled else "DISARMED",
            self.watch_gate_on())
        return self.watch_gate_status()

    # ── supervised-mode toggle (the dashboard 🟡 PM-gate switch, #66) ──────────
    # The persisted supervised flag (supervised.json) is read/written through the
    # _supervised_store JsonFlag (#140) — atomic write, OFF on a missing or corrupt
    # file so supervised mode never turns itself ON by accident (full-auto is the
    # default). The manager owns the in-memory _supervised_enabled and the path.

    def supervised_on(self) -> bool:
        """The EFFECTIVE supervised state fed into the ReviewPipeline `supervised`
        constructor seam: ON ⇒ a Kleiner-clean review parks at needs-signoff (🟡)
        for a human sign-off before Eli lands; OFF ⇒ full-auto. The live effect
        lands once Kleiner is manager-wired (see SUPERVISED_FILENAME)."""
        return self._supervised_enabled

    def supervised_status(self) -> dict:
        """The supervised-mode state for the dashboard. Shape is fixed (the
        frontend + test_agent_routes.py depend on these keys):

        - ``enabled``   — the persisted dashboard toggle (the sole control).
        - ``effective`` — what the review pipeline gates on; equals ``enabled``.
        """
        return {
            "enabled": self._supervised_enabled,
            "effective": self.supervised_on(),
        }

    # ── staleness (loaded SHA vs current repo HEAD — issue #182) ────────────────
    # The agent-manager is a detached daemon that survives the dashboard .bat
    # restart, so a code fix is dead until the manager is recycled — and nothing
    # before #182 made that stale state visible. The manager records the git
    # short-SHA at spawn (_loaded_sha) and compares it to the current repo HEAD
    # on each read; stale=True means the running code does not match what's on
    # disk — a recycle is needed for the fix to take effect.

    def staleness(self) -> dict:
        """The staleness snapshot: ``{loaded_sha, repo_head, stale}``.

        ``loaded_sha`` — the short SHA captured at manager spawn (or None).
        ``repo_head``  — the current ``git rev-parse --short HEAD`` (or None).
        ``stale``      — True when both SHAs are known and differ; False otherwise.

        Best-effort: a missing git or non-repo workspace returns
        ``{loaded_sha: None, repo_head: None, stale: False}`` — staleness must
        never false-positive when we can't tell.
        """
        # Capture loaded once at init; resolve repo_head fresh each call.
        loaded = self._loaded_sha
        try:
            repo_head = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self._workspace, text=True,
            ).strip() or None
        except Exception:
            repo_head = None
        stale = bool(loaded and repo_head and loaded != repo_head)
        return {"loaded_sha": loaded, "repo_head": repo_head, "stale": stale}

    def set_supervised(self, enabled: bool) -> dict:
        """Turn supervised mode ON/OFF (persisted + live). Returns the new status.
        Takes effect on the NEXT review cycle once Kleiner is manager-wired —
        no restart."""
        self._supervised_enabled = bool(enabled)
        self._supervised_store.save(self._supervised_enabled)
        self._log.warning(
            "supervised mode %s via toggle (effective=%s)",
            "ON" if self._supervised_enabled else "OFF",
            self.supervised_on())
        return self.supervised_status()

    # ── PM approve / reject (Slice C) ──────────────────────────────────────────

    def approve_issue(self, number: int) -> None:
        """PM sign-off APPROVE: flip needs-signoff → approved (Dr. Eli Vance picks it up
        later) and log the PM's decision as a comment. The label flip is
        load-bearing; the comment is best-effort (a gh comment failure must not
        leave the issue stuck in needs-signoff)."""
        gw = self._gateway_factory()
        gw.set_labels(number, remove=[NEEDS_SIGNOFF_LABEL], add=[APPROVED_LABEL])
        try:
            gw.leave_comment(number, format_pm_decision("approve"))
        except Exception:  # noqa: BLE001 — comment is best-effort, label flip won
            self._log.warning(
                "approve_issue #%d: label flipped but PM-decision comment failed",
                number, exc_info=True)

    def reject_issue(self, number: int) -> None:
        """PM sign-off REJECT: flip needs-signoff → rejected (terminal; it sits)
        and log the PM's decision as a comment. Same load-bearing-label /
        best-effort-comment contract as approve_issue."""
        gw = self._gateway_factory()
        gw.set_labels(number, remove=[NEEDS_SIGNOFF_LABEL], add=[REJECTED_LABEL])
        try:
            gw.leave_comment(number, format_pm_decision("reject"))
        except Exception:  # noqa: BLE001 — comment is best-effort, label flip won
            self._log.warning(
                "reject_issue #%d: label flipped but PM-decision comment failed",
                number, exc_info=True)

    # ── trade-surface approval (🟣 money wall — #65 / #63 two-pass handshake) ───

    def trade_approve_issue(self, number: int) -> None:
        """🟣 TRADING-SURFACE APPROVE (#65): clear the #63 money wall on issue
        #number. APPLIES the ``trade-approved`` clearance label and puts the issue
        BACK into Eli's inbox (``approved``), removing ``needs-trade-approval`` — the
        #63 two-pass handshake. On Eli's next poll, ``decide_integration`` sees the
        clearance label present and PROCEEDS (it does NOT invent a new path; the
        label IS the side-channel-free clearance Eli reads).

        Single-action by design: there is no reject — approving is the only way to
        clear the money wall, so this is the only verb the 🟣 view wires.

        Load-bearing label flip + best-effort comment, mirroring approve_issue."""
        gw = self._gateway_factory()
        gw.set_labels(
            number,
            remove=[NEEDS_TRADE_APPROVAL_LABEL],
            add=[TRADE_APPROVED_LABEL, APPROVED_LABEL],
        )
        try:
            gw.leave_comment(number, format_pm_decision("trade-approve"))
        except Exception:  # noqa: BLE001 — comment is best-effort, label flip won
            self._log.warning(
                "trade_approve_issue #%d: labels flipped but PM-decision comment failed",
                number, exc_info=True)

    def list_trade_approval_issues(self) -> list[dict]:
        """The 🟣 inbox: open issues parked at the #63 money wall, as
        ``[{number, title}]`` (oldest-first by number). Feeds the dashboard's 🟣
        view so the operator can pick which money-wall issue to approve. Best-
        effort: a gateway error returns ``[]`` (never crashes the view)."""
        try:
            gw = self._gateway_factory()
            issues = gw.list_needs_trade_approval_issues()
            return [
                {"number": i.number, "title": i.title}
                for i in sorted(issues, key=lambda x: x.number)
            ]
        except Exception:  # noqa: BLE001 — never let a gh blip crash the 🟣 view
            self._log.warning(
                "list_trade_approval_issues: gateway error — empty list",
                exc_info=True)
            return []

    def trade_approval_brief(self, number: int) -> dict:
        """The 🟣 view payload for issue #number: the plain-English #63 brief plus
        the issue title/number. The brief is RE-DERIVED here (not scraped from a
        comment) by classifying the issue's PR files via the SAME pure
        ``classify_paths`` / ``format_trade_approval_brief`` Eli used to post it —
        so the dashboard and the loop agree on the wording by construction.

        Best-effort: a gateway error yields a generic brief rather than crashing
        the view. Returns ``{number, title, brief}``."""
        try:
            gw = self._gateway_factory()
            # Title from the 🟣 inbox (the needs-trade-approval list carries it).
            title = next(
                (i.title for i in gw.list_needs_trade_approval_issues()
                 if i.number == number),
                "",
            )
            # Files from the issue's open PR → the SAME classify Eli ran.
            pr = gw.find_open_pr_for_issue(number)
            files = gw.get_pr_files(pr) if pr is not None else []
            verdict = classify_paths(files)
            return {
                "number": number,
                "title": title or "",
                "brief": format_trade_approval_brief(verdict),
            }
        except Exception:  # noqa: BLE001 — never let a gh blip crash the 🟣 view
            self._log.warning(
                "trade_approval_brief #%d: gateway error — generic brief", number,
                exc_info=True)
            return {
                "number": number,
                "title": "",
                "brief": format_trade_approval_brief(classify_paths([])),
            }

    # ── 🟢 completed-work ack (clears the green pill on viewing — #65) ──────────
    # The seen-run_id set (completed_ack.json) is read/written through the
    # _completed_ack JsonSet (#140) — atomic write, empty-on-missing/corrupt.

    def _succeeded_run_ids(self) -> set[str]:
        """The ids of every run that ended SUCCEEDED — the candidate green set."""
        return {
            r["run_id"]
            for r in self._list_runs()
            if r.get("status") == SUCCEEDED and r.get("run_id")
        }

    def ack_completed(self) -> dict:
        """Mark all currently-succeeded runs as SEEN — the 🟢 pill's "clears on
        viewing" (#65). Folds the current succeeded run_ids into the persisted ack
        set so the green count drops to 0 until a NEW run succeeds. Idempotent.
        Returns ``{"acked": <total seen count>}``."""
        acked = self._completed_ack.load() | self._succeeded_run_ids()
        self._completed_ack.save(acked)
        self._bump_epoch()
        return {"acked": len(acked)}

    # ── 🔴 problems ack (clears a halted run from the red pill on acknowledge) ──
    # The acknowledged-halt run_id set (problems_ack.json) is read/written through
    # the _problems_ack JsonSet (#140) — atomic write, empty-on-missing/corrupt.

    def ack_problem(self, run_id: str) -> dict:
        """Mark one halted run ACKNOWLEDGED so it drops out of the 🔴 red pill.
        Folds the run_id into the persisted problems-ack set; the red count then
        excludes it until (it never un-halts — halt is terminal). Idempotent.
        Returns ``{"acked": <total acknowledged count>}``."""
        acked = self._problems_ack.load()
        acked.add(run_id)
        self._problems_ack.save(acked)
        self._bump_epoch()
        return {"acked": len(acked)}

    # ── dashboard pill counts (four-pill HITL badge — #65) ─────────────────────

    def pill_counts(self) -> dict:
        """The four notification-pill counts for the dashboard badge (#65) —
        delegates to StatusAggregator (#141). See ``StatusAggregator.pill_counts``
        for the gather/derive flow, the best-effort-zeros fallback, and the
        needs-human/halted-run dedup contract."""
        return self._status.pill_counts()

    # ── 🩺 loop health (why the claim loop is / isn't advancing — the panel) ─────

    def loop_status(self) -> dict:
        """The full claim-loop health picture for the dashboard's loop-health
        panel — delegates to StatusAggregator (#141). See
        ``StatusAggregator.loop_status`` for the returned shape and the
        gh-degrades-to-baseline (``gh_ok=False``) fail-soft contract."""
        return self._status.loop_status()

    def resolve_blocker(self, action: dict) -> dict:
        """Perform ONE mechanical loop-health unblock requested from the panel.

        Only the no-judgment actions are wired here (the ``needs_human`` cards carry
        no action — those go to a Claude Code session):

          * ``{"type": "clear_freeze"}`` — remove the architecture-freeze sentinel.
          * ``{"type": "close_pr", "pr": <N>}`` — close an orphaned agent PR so the
            claim gate (``open_prs`` cap) reopens.

        Returns a small result dict. Raises ``ValueError`` on a malformed/unknown
        action (the RPC maps it to an error reply); a gh failure on ``close_pr``
        propagates as ``GitHubGatewayError`` (mapped to a 503-ish error)."""
        kind = action.get("type") if isinstance(action, dict) else None
        if kind == "clear_freeze":
            cleared = self._loop_gate.clear()
            self._log.info("loop-health: freeze cleared via panel (was_frozen=%s)", cleared)
            return {"resolved": "clear_freeze", "cleared": bool(cleared)}
        if kind == "close_pr":
            pr = action.get("pr")
            if not isinstance(pr, int) or isinstance(pr, bool):
                raise ValueError("close_pr requires an integer 'pr'")
            self._gateway_factory().close_pr(
                pr,
                comment="Closed from the dashboard loop-health panel — orphaned "
                        "agent PR was pinning the claim gate.",
            )
            self._log.info("loop-health: closed orphaned PR #%d via panel", pr)
            return {"resolved": "close_pr", "pr": pr}
        raise ValueError(f"unknown resolve action {kind!r}")

    def _due_watch_candidates(self, now: float) -> list[Candidate]:
        """The watch-triggered agents due for a GitHub poll at ``now``.

        Two FULL-STOP gates apply to ALL stages first, then a PER-STAGE gate (#78):

        1. **Arm switch** (``watch_gate_on()``) — the persisted dashboard toggle
           and ONLY the toggle; the legacy env force-on was dropped 2026-06-23
           (rogue-watcher incident, footgun #176). "Nathan paused the loop."
        2. **Architecture freeze** (``self._loop_gate.is_frozen()``) — Eli wrote
           the freeze sentinel on scope drift. INDEPENDENT of the toggle: a PM
           un-pausing the toggle must NOT silently clear a freeze (#62). "Eli
           stopped on scope drift."
        3. **Per-stage gate** (#78, ``stage_withheld``) — applied PER watch agent by
           its ``trigger.stage``, because the three stages play different roles in
           the strict-serial chain:
             * ``claim`` (Gordon) — a NEW claim, withheld while ANY issue is in
               flight (#64 ``should_withhold_loop``) OR open agent PRs are at/over
               the cap (#53 ``should_withhold_watch``). Never adds a 2nd issue.
             * ``review`` (Kleiner) / ``land`` (Eli) — ADVANCE the one in-flight
               issue, so the in-flight / open-PR gates do NOT apply (they exist to
               progress what is in flight). Withheld ONLY to prevent OVERLAP: a
               second same-stage run while one is already RUNNING.

        INVARIANT: at most one issue is ever in the whole chain — a second issue
        can never be CLAIMED while one is in flight — but Kleiner still runs on the
        in-flight ``needs-review`` issue and Eli on the in-flight ``approved`` one.

        When the gates pass for a stage, a watch agent is due when its
        ``poll_interval`` seconds have elapsed since its last poll. Each due agent
        becomes a ready Candidate fed to the SAME ``decide_launches`` core as clock
        agents (the documented #33 seam: "a watch agent just becomes another
        Candidate").
        """
        # Gate 1 — master arm switch (the dashboard toggle).
        if not self.watch_gate_on():
            if not self._watch_disabled_logged:
                self._log.warning(
                    "WATCH TRIGGER DISARMED — autonomous issue→PR runs are OFF. "
                    "Arm it from the dashboard (AgentManager → master toggle) — "
                    "that toggle is the only way to arm it. "
                    "Skipping ALL watch agents."
                )
                self._watch_disabled_logged = True
            return []

        # Gate 2 — architecture freeze (Eli stopped the loop on scope drift). A
        # SEPARATE state from the watch toggle: armed-but-frozen still withholds.
        if self._loop_gate.is_frozen():
            if not self._loop_frozen_logged:
                self._log.warning(
                    "LOOP FROZEN on architecture drift — new claims are OFF until "
                    "the freeze is cleared in a PM session. Reason: %s. "
                    "Skipping ALL watch agents.",
                    self._loop_gate.frozen_reason(),
                )
                self._loop_frozen_logged = True
            return []
        # Cleared since the last freeze — reset the one-shot so a future freeze
        # logs loudly again (mirrors set_watch_gate resetting _watch_disabled_logged).
        self._loop_frozen_logged = False

        # Gate 3 — the PER-STAGE gate (#78). The strict-serial in-flight count (#64)
        # and the cross-PR count (#53) only matter for CLAIM agents, so they are
        # computed LAZILY: a tick with only review/land agents never shells out to
        # gh for them. Both fail CLOSED (a counting error withholds a CLAIM rather
        # than risk a second concurrent claim). Cached so we count at most once.
        claim_counts: dict[str, int] | None = None

        def _claim_counts() -> dict[str, int] | None:
            """(in_flight, open_prs) for the claim gate, computed once per tick.
            Returns None if either count errors (the claim agent then withholds)."""
            nonlocal claim_counts
            if claim_counts is not None:
                return claim_counts
            try:
                in_flight = self._loop_in_flight_counter()
            except Exception:
                self._log.exception(
                    "could not count in-flight loop issues — withholding claims this tick")
                return None
            try:
                open_prs = self._open_agent_pr_counter()
            except Exception:
                self._log.exception(
                    "could not count open agent PRs — withholding claims this tick")
                return None
            claim_counts = {"in_flight": in_flight, "open_prs": open_prs}
            return claim_counts

        # Per-stage running-run counts (overlap guard). Cheap run-store reads; no gh.
        running_by_prefix = {
            "claim": self._count_running_stage_runs("watch-issue-"),
            "review": self._count_running_stage_runs("review-issue-"),
            "land": self._count_running_stage_runs("land-issue-"),
        }

        out: list[Candidate] = []
        for agent in self._list_agents():
            if agent.get("enabled", True) is False:
                continue  # OFF is a hard stop (#40) — a disabled watch agent is never polled
            trigger = agent.get("trigger") or {}
            if trigger.get("kind") != "watch":
                continue
            stage = trigger.get("stage", DEFAULT_WATCH_STAGE)
            same_stage_running = running_by_prefix.get(stage, running_by_prefix["claim"])

            # The claim gate needs the in-flight / open-PR counts; review/land don't.
            if stage == "claim":
                counts = _claim_counts()
                if counts is None:
                    continue  # fail CLOSED — a counting error withholds this claim
                in_flight, open_prs = counts["in_flight"], counts["open_prs"]
            else:
                in_flight = open_prs = 0  # not consulted for advancing stages

            if stage_withheld(
                stage,
                in_flight_count=in_flight,
                open_agent_pr_count=open_prs,
                same_stage_running=same_stage_running,
                agent_pr_cap=self._agent_pr_cap,
                loop_in_flight_cap=self._loop_in_flight_cap,
            ):
                self._log.info(
                    "withholding %s watch agent %r this tick (in_flight=%d open_prs=%d "
                    "same_stage_running=%d)",
                    stage, agent.get("id"), in_flight, open_prs, same_stage_running)
                continue

            interval = trigger.get("poll_interval") or 0
            agent_id = agent.get("id")
            last = self._last_watch_poll.get(agent_id)
            if last is not None and (now - last) < interval:
                continue  # not yet due for another poll
            # ready_since = when this poll became due, so an older-due watch
            # agent launches before a newer one under the cap.
            ready_since = (last + interval) if last is not None else now
            out.append(Candidate(agent_id=agent_id, ready_since=ready_since, stage=stage))
        return out

    async def _scheduler_tick(
        self,
        *,
        now: float,
        launch=None,
        watch_launch=None,
        in_flight: set[str] | None = None,
        cap: int | None = None,
        staleness: dict | None = None,
    ) -> list[str]:
        """One evaluation of the loop — the testable, injectable unit.

        Gathers the due clock AND watch candidates, asks the PURE decide_launches
        core which to fire (oldest-first, cap-respecting, in-flight-excluded),
        then invokes the right launcher for each: clock agents go to ``launch``
        (a Run + tagged terminal), watch agents go to ``watch_launch`` (the
        issue→PR pipeline). Both launchers are INJECTED so tests pass recording
        fakes — no real run / ConPTY / worktree / claude / gh is ever spawned in
        the suite. In production they are ``_launch_scheduled_run`` /
        ``_launch_watch_run``.

        ``staleness`` is an injectable ``{loaded_sha, repo_head, stale}`` dict so
        tests can drive the stale-pre-launch recycle path without touching git.
        Production defaults to ``self.staleness()``.

        Returns the agent_ids launched this tick (for tests / logging).
        """
        if launch is None:
            launch = self._launch_scheduled_run
        if watch_launch is None:
            watch_launch = self._launch_watch_run
        if in_flight is None:
            in_flight = self._in_flight_agent_ids()
        if cap is None:
            cap = self._concurrency_cap
        if staleness is None:
            staleness = self.staleness()

        clock_cands = self._due_clock_candidates(now)
        watch_cands = self._due_watch_candidates(now)
        # Tag which agent_ids are watch so we route the launch correctly.
        watch_ids = {c.agent_id for c in watch_cands}

        to_launch = decide_launches(
            [*clock_cands, *watch_cands], in_flight=in_flight, cap=cap
        )

        # ── stale gate ───────────────────────────────────────────────────
        # If the daemon is running old code, don't launch ANYTHING — even if
        # the fleet is busy.  Two cases:
        #
        #   Idle  → recycle NOW so the fresh manager handles the work.
        #   Busy  → withhold ALL launches so the fleet DRAINS.  Once the
        #           in-flight work completes, _maybe_auto_recycle fires and
        #           the fresh manager picks up the withheld candidates.
        #
        # Without this gate, a busy fleet stays busy forever — every tick
        # launches new stale work before the previous work completes, so the
        # idle window never opens and the recycle never fires.
        if staleness.get("stale"):
            idle, n_watchers, n_launches = self._fleet_idle()
            if idle:
                if to_launch:
                    age = now - self._loaded_at
                    if age < STALE_PRE_LAUNCH_GRACE_PERIOD:
                        self._log.info(
                            "stale gate: suppressing recycle — daemon only %.0fs "
                            "old (grace=%.0fs). Launching %d candidate(s) with "
                            "fresh-enough code.",
                            age, STALE_PRE_LAUNCH_GRACE_PERIOD, len(to_launch),
                        )
                        # fall through to normal launch below
                    else:
                        self._log.warning(
                            "stale gate: daemon loaded %s, repo at %s — "
                            "%d candidate(s) pending, fleet idle → recycling "
                            "before launch so fresh code handles the work",
                            staleness.get("loaded_sha"), staleness.get("repo_head"),
                            len(to_launch),
                        )
                        await self._self_shutdown("stale-pre-launch")
                        return []
                # stale + idle + no work → let the idle fallback handle it
            else:
                if to_launch:
                    self._log.info(
                        "stale gate: withholding %d candidate(s) — fleet busy "
                        "(%d watcher(s), %d launch task(s)). "
                        "Letting in-flight work drain before recycle.",
                        len(to_launch), n_watchers, n_launches,
                    )
                return []  # withhold ALL launches, even if fleet is busy

        minute_key = wall_clock_minute_key(now)  # local wall-clock minute (#116, M5)
        for agent_id in to_launch:
            is_watch = agent_id in watch_ids
            # Record the fire BEFORE the launch so the next tick can't double-fire:
            # clock agents per due-minute, watch agents per poll.
            if is_watch:
                self._last_watch_poll[agent_id] = now
                # A watch run can run for the full length of a coding session
                # (30+ min). Awaiting it inline would suspend this loop the whole
                # time (the #95/#96 freeze). Launch it as a TRACKED BACKGROUND task
                # and return promptly; the loop keeps ticking. The strict-serial
                # invariant is preserved unchanged — candidate-gating in
                # _due_watch_candidates still withholds a second concurrent watch
                # run. Failures retry via the task's done-callback, mirroring the
                # old inline except branch.
                self._spawn_watch_launch(agent_id, watch_launch, fired_at=now)
            else:
                self._last_clock_fire[agent_id] = minute_key
                # Clock launches are quick (a Run record + a tagged terminal), so
                # they stay inline — a failure drops the fire-ledger mark so a
                # transient error retries on a later tick rather than being stuck.
                try:
                    await launch(agent_id)
                except Exception:
                    self._log.exception("scheduled launch of %r failed", agent_id)
                    if self._last_clock_fire.get(agent_id) == minute_key:
                        self._last_clock_fire.pop(agent_id, None)
        return to_launch

    def _spawn_watch_launch(self, agent_id: str, watch_launch, *, fired_at: float) -> None:
        """Launch a watch pipeline as a tracked background task (#114).

        Backgrounding keeps the scheduler loop ticking while a long (30-min) watch
        run is in flight — the #95/#96 freeze fix. The task is registered in
        ``_watch_launch_tasks`` so shutdown can drain it (no orphaned tasks), and a
        done-callback drops the fire-ledger mark on failure so a transient error
        retries on a later tick (the same retry behaviour the old inline await had).

        The strict-serial invariant is left to the EXISTING candidate-gating
        (``_due_watch_candidates``), preserved unchanged: a watch agent's own
        poll-interval mark withholds it across ticks, and the run-store /
        in-flight / overlap counts withhold a second concurrent watch run while one
        is RUNNING. So this launcher never needs its own serial guard — adding one
        would only mis-fire on a fire-and-forget task still lingering in the map.
        """
        # Capture the completing agent's stage so the done callback can wake
        # only the NEXT stage in the pipeline (not ALL stages — clearing all
        # timers re-launches the completing agent itself in a tight loop when
        # the overlap guard's run record hasn't been written yet).
        agent_def = self._get_agent(agent_id) or {}
        completing_stage = (agent_def.get("trigger") or {}).get("stage", DEFAULT_WATCH_STAGE)

        task = asyncio.create_task(watch_launch(agent_id))
        self._watch_launch_tasks[agent_id] = task
        self._bump_epoch()  # dashboard sees the launch immediately, not on next tick

        def _on_done(t: asyncio.Task, agent_id=agent_id, fired_at=fired_at) -> None:
            # Detach from the registry first so a re-launch is never blocked by a
            # finished task lingering in the map.
            if self._watch_launch_tasks.get(agent_id) is t:
                self._watch_launch_tasks.pop(agent_id, None)
            if t.cancelled():
                return  # shutdown/drain cancelled it — nothing to retry
            exc = t.exception()
            if exc is not None:
                self._log.error(
                    "background watch launch of %r failed: %r", agent_id, exc)
                # Drop the fire mark so the next due tick retries, but only if it's
                # still OUR mark (a later poll may have overwritten it).
                if self._last_watch_poll.get(agent_id) == fired_at:
                    self._last_watch_poll.pop(agent_id, None)
            else:
                # Wake-on-completion: clear poll timers for the NEXT stage in the
                # pipeline so it fires on the next tick instead of waiting up to
                # 120s.  We clear ONLY the next stage, not all — clearing all
                # timers creates a re-launch loop because the overlap guard's run
                # record may not be written yet (pipeline runs in a thread).
                _NEXT_STAGE = {"claim": "review", "review": "land", "land": "claim"}
                next_stage = _NEXT_STAGE.get(completing_stage)
                if next_stage:
                    # Find agents of the next stage and clear only their timers.
                    for a in self._list_agents():
                        a_stage = (a.get("trigger") or {}).get("stage")
                        a_id = a.get("id")
                        if a_stage == next_stage and a_id not in self._watch_launch_tasks:
                            # Only wake the next stage if it's not already
                            # running — the overlap guard (run store) may not
                            # see it yet because the pipeline creates the run
                            # record inside its thread.
                            self._last_watch_poll.pop(a_id, None)

        task.add_done_callback(_on_done)

    async def _drain_background_tasks(self) -> None:
        """Cancel and await every tracked background task so shutdown leaves no
        orphaned/pending asyncio tasks (#114). Covers the backgrounded watch-launch
        tasks and the run-session watchers (#30). A watch pipeline's worker THREAD
        (asyncio.to_thread) can't be cancelled, but the awaiting task is cleaned up
        so there's no pending-task warning on exit."""
        tasks = [
            *self._watch_launch_tasks.values(),
            *self._run_engine.watcher_tasks(),
        ]
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _launch_scheduled_run(self, agent_id: str) -> None:
        """Production launcher for a clock-triggered agent: same path as a
        manual run-now (a Run record + a tagged terminal). Kept tiny so the
        scheduler tick can inject a fake in its place for tests."""
        await self._run_now(agent_id)

    # ── watch trigger → issue→PR pipeline (issue #35) ──────────────────────────

    async def _launch_watch_run(self, agent_id: str) -> None:
        """Production launcher for a WATCH-triggered agent: run ONE cycle of the
        agent's STAGE pipeline (#78). Only ever reached when watch_gate_on() is
        True (the master gate is checked in _due_watch_candidates).

        The agent's ``trigger.stage`` selects which pipeline is built:
          * ``claim``  → Gordon  (``_build_real_pipeline`` — issue→PR)
          * ``review`` → Kleiner (``_build_real_review_pipeline`` — needs-review)
          * ``land``   → Eli     (``_build_real_eli_pipeline`` — approved)

        Each pipeline is blocking (subprocess git/pytest/gh/claude), so it runs in
        a thread to avoid stalling the event loop. Kept tiny so the scheduler tick
        can inject a fake factory in its place for tests."""
        # Per-agent run timeout (minutes) — the backstop for the run's claude
        # session. Falls back to the default when the def omits the field.
        agent = self._get_agent(agent_id) or {}
        timeout_min = agent.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
        timeout_s = float(timeout_min) * 60.0
        stage = (agent.get("trigger") or {}).get("stage", DEFAULT_WATCH_STAGE)

        if stage == "review":
            pipeline = self._review_pipeline_factory(run_timeout_s=timeout_s)
        elif stage == "land":
            pipeline = self._eli_pipeline_factory(run_timeout_s=timeout_s)
        else:  # "claim" (and any unknown stage — fail safe to Gordon)
            pipeline = self._pipeline_factory(run_timeout_s=timeout_s)

        result = await asyncio.to_thread(pipeline.process)
        if result.acted:
            self._log.info(
                "%s watch agent %r processed issue #%s (failed=%s draft=%s url=%s)",
                stage, agent_id, result.issue_number, result.failed,
                result.draft, result.pr_url,
            )

    def _build_real_pipeline(self, *, run_timeout_s: float = DEFAULT_TIMEOUT_MINUTES * 60.0) -> IssuePrPipeline:
        """Assemble the production IssuePrPipeline: real gateway, real worktree
        creator, the #34 pre-push hook installer, a guarded claude launcher over
        the pty-manager, and the system-Python-3.12 pytest test runner.

        ``run_timeout_s`` is the per-agent backstop the launcher waits before
        killing a stuck claude session (threaded in from the agent def by
        _launch_watch_run).

        Tests NEVER call this — they set ``self._pipeline_factory`` to a fake — so
        no real worktree / claude / push / gh / recursive pytest runs in the
        suite."""
        gateway = GitHubGateway()
        worktree_maker = WorktreeCreator(repo_root=self._workspace)
        recorder = _RunStoreRecorder(self)

        def launch_claude(*, command: str, cwd: str, tag: str) -> LaunchOutcome:
            # #34 launch-command guard: block a forbidden command before it runs.
            enforce_run_command(command)
            # Launch claude AND WAIT for it to finish — the pipeline grades the
            # worktree with pytest right after this returns, so a fire-and-forget
            # launch would test untouched code ([[footguns#168]]).
            try:
                result = asyncio.run(run_session_with_retry(
                    self._terminal, command=command, cwd=cwd, tag=tag,
                    timeout_s=run_timeout_s,
                ))
            except Exception as e:
                self._log.exception("watch run claude launch/wait raised")
                return LaunchOutcome(False, f"claude launch raised: {e}")
            outcome = classify_launch(result, timeout_s=run_timeout_s)
            if outcome.ok:
                self._log.info("watch run claude session completed — grading worktree")
            else:
                self._log.error("watch run %s", outcome.reason)
            return outcome

        return IssuePrPipeline(
            gateway=gateway,
            worktree_maker=worktree_maker,
            hook_installer=install_pre_push_hook,
            run_launcher=launch_claude,
            test_runner=run_pytest_in_worktree,
            branch_pusher=push_branch_to_origin,
            run_recorder=recorder,
            frontend_checker=run_frontend_check,
        )

    def _claude_run_launcher(self, *, run_timeout_s: float, kind: str):
        """A ``(*, command, cwd, tag) -> bool`` claude launcher mirroring Gordon's
        ``launch_claude`` (#78): the SAME #34 launch-command guard, then launch
        claude AND WAIT for it to exit (the caller grades/reads results right
        after, so a fire-and-forget launch would race an unfinished session —
        [[footguns#168]]). ``kind`` is a label for the logs (review / fixer).
        Returns True iff the session launched, did not time out, and ran to
        completion. Used by the Kleiner factory for BOTH the reviewer and the
        FRESH-fixer seams (each gets its own call ⇒ structurally separate sessions,
        never a resumed builder — the #60 cost lesson)."""
        def launch_claude(*, command: str, cwd: str, tag: str) -> LaunchOutcome:
            enforce_run_command(command)  # #34 guard — block a forbidden command
            try:
                result = asyncio.run(run_session_with_retry(
                    self._terminal, command=command, cwd=cwd, tag=tag,
                    timeout_s=run_timeout_s,
                ))
            except Exception as e:
                self._log.exception("%s run claude launch/wait raised", kind)
                return LaunchOutcome(False, f"{kind} claude launch raised: {e}")
            outcome = classify_launch(result, timeout_s=run_timeout_s)
            if outcome.ok:
                self._log.info("%s run claude session completed", kind)
            else:
                self._log.error("%s run %s", kind, outcome.reason)
            return outcome

        return launch_claude

    def _build_real_review_pipeline(
        self, *, run_timeout_s: float = DEFAULT_TIMEOUT_MINUTES * 60.0
    ) -> ReviewPipeline:
        """Assemble the production ReviewPipeline (Kleiner, #78): real gateway, a
        guarded claude ``run_launcher`` (build_claude_command + run-to-completion,
        the SAME shape Gordon uses), a SEPARATE ``fixer_dispatch`` that launches a
        FRESH fixer claude session (never a resumed builder — the #60 cost lesson),
        the live ``supervised`` toggle, a review run recorder (records under
        ``review-issue-<n>``), and the default ``lens_loader`` (reads the real
        review_heuristics.md). Uses ``DEFAULT_MAX_ROUNDS``.

        Tests NEVER call this — they set ``self._review_pipeline_factory`` to a
        fake — so no real claude / gh / temp-dir review runs in the suite."""
        gateway = GitHubGateway()
        recorder = _StageRunRecorder(self, prefix="review-issue", label="review")
        run_launcher = self._claude_run_launcher(run_timeout_s=run_timeout_s, kind="review")
        fixer_launcher = self._claude_run_launcher(run_timeout_s=run_timeout_s, kind="fixer")

        def fixer_dispatch(*, pr_number: int, issue_number: int, finding: str, files) -> bool:
            """Launch a FRESH fixer claude scoped to the finding + the PR's files.
            STRUCTURALLY a brand-new session (its own launcher call), never the
            resumed builder. The fixer applies the minimal fix + a regression test
            on the PR's branch in a throwaway worktree. Returns True iff it ran to
            completion (Kleiner then re-fetches the diff and re-reviews)."""
            from agents.kleiner_rework import build_fixer_prompt
            prompt = build_fixer_prompt(
                issue_number=issue_number, finding=finding, files=tuple(files or ()))
            command = build_claude_command(model="claude", prompt=prompt)
            tag = f"fixer:issue-{issue_number}"
            # The fixer seam's contract is a plain bool ("ran to completion?");
            # coerce the launcher's LaunchOutcome down via its truthiness.
            return bool(fixer_launcher(command=command, cwd=self._workspace, tag=tag))

        return ReviewPipeline(
            gateway=gateway,
            run_launcher=run_launcher,
            recorder=recorder,
            supervised=self.supervised_on(),
            fixer_dispatch=fixer_dispatch,
            max_rounds=DEFAULT_MAX_ROUNDS,
        )

    def _build_real_eli_pipeline(
        self, *, run_timeout_s: float = DEFAULT_TIMEOUT_MINUTES * 60.0
    ) -> EliPipeline:
        """Assemble the production EliPipeline (#78): real gateway, the #73
        run-store token rollup, a land run recorder (records under
        ``land-issue-<n>``), and the four real seam helpers from
        ``agents.eli_runtime`` bound to this workspace:

          * ``merger``            — local ``git merge --ff-only <branch>`` onto main
                                    (never pushes; routed through eli_enforce_command).
          * ``bundle_builder``    — the single ``npm run deploy:dashboard`` in web/.
          * ``full_suite_runner`` — the WHOLE pytest suite (system 3.12) on the
                                    merged tree.
          * ``main_pusher``       — ``git push origin main`` (the one push-allowed
                                    seam; only on a green suite).

        ``run_timeout_s`` is accepted for signature symmetry with the other
        factories (Eli has no claude session of its own to backstop). Tests NEVER
        call this — they set ``self._eli_pipeline_factory`` to a fake — so no real
        git merge / npm build / recursive pytest / push runs in the suite."""
        gateway = GitHubGateway()
        recorder = _StageRunRecorder(self, prefix="land-issue", label="land")
        ws = self._workspace
        return EliPipeline(
            gateway=gateway,
            merger=lambda *, branch: merge_branch_onto_main(ws, branch=branch),
            bundle_builder=lambda: rebuild_dashboard_bundle(ws),
            full_suite_runner=lambda: run_full_suite(ws),
            main_pusher=lambda: push_main(ws),
            recorder=recorder,
            token_rollup=_RunStoreTokenRollup(self),
        )

    async def _run_scheduler_loop(self) -> None:
        """Background driver: tick the clock scheduler forever. Cancelled on
        shutdown. Each iteration reads the wall clock ONCE and hands it to the
        pure tick — the loop is the only place a clock is read, keeping the
        decision core pure."""
        self._log.info("scheduler loop started (cap=%d, tick=%.0fs)",
                       self._concurrency_cap, SCHEDULER_TICK_INTERVAL)
        try:
            while True:
                # Bump BEFORE the tick so the counter advances on every loop
                # check, even one whose evaluation raises — the dashboard pulse
                # marks "the manager looked for work", which it did regardless.
                self._scheduler_tick_seq += 1
                self._bump_epoch()
                # Proactive pty-manager watchdog (#96): heal a manager that died
                # BETWEEN runs on the tick — the proactive twin of create()'s
                # reactive self-heal ([[footguns#189]]/#192). Keeps the pty-manager
                # warm so the autonomous loop never eats a failed-launch escalation
                # (and keeps the operator's own terminals ready). Never fatal.
                try:
                    await self._terminal.ensure_pty_alive()
                except Exception:
                    self._log.exception("pty-manager health check raised")
                try:
                    await self._scheduler_tick(now=time.time())
                except Exception:
                    self._log.exception("scheduler tick raised")
                # Auto-recycle when stale + idle (#198): piggybacks on the
                # scheduler tick so it re-evaluates on the same 30s cadence.
                try:
                    await self._maybe_auto_recycle()
                except Exception:
                    self._log.exception("auto-recycle check raised")
                await asyncio.sleep(SCHEDULER_TICK_INTERVAL)
        except asyncio.CancelledError:
            self._log.info("scheduler loop stopped")
            raise

    async def _run_snapshot_warm_loop(self) -> None:
        """Keep the snapshot's expensive gh half warm WHILE the agent panel is
        open (#151 perf). Idle — a cheap 1s monotonic check — until a recent
        snapshot request armed the window (_arm_snapshot_warm), then refresh the
        StatusAggregator's gh caches every _SNAPSHOT_WARM_INTERVAL so a
        user-facing /ws/agents push reads warm (sub-second) instead of paying the
        ~8-call cold cost. Demand-scoped: the window expires once requests stop
        (panel closed / WS dropped), so there is NO gh load when nobody watches.
        The refresh is offloaded to a thread (it shells out to `gh`) so it never
        blocks the event loop; a refresh error is logged and the loop continues."""
        self._log.info(
            "snapshot-warm loop started (interval=%.0fs, window=%.0fs)",
            _SNAPSHOT_WARM_INTERVAL, _SNAPSHOT_WARM_WINDOW)
        try:
            while True:
                if time.monotonic() < self._snapshot_warm_until:
                    try:
                        await asyncio.to_thread(self._status.warm_gh)
                    except Exception:
                        self._log.exception("snapshot-warm refresh raised")
                    await asyncio.sleep(_SNAPSHOT_WARM_INTERVAL)
                else:
                    # Idle — poll cheaply for the next arm.
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            self._log.info("snapshot-warm loop stopped")
            raise

    # ── auto-recycle (#198) ──────────────────────────────────────────────────

    def _fleet_idle(self) -> tuple[bool, int, int]:
        """True when no runs are in flight that would be interrupted by a recycle.

        Returns ``(idle, watcher_count, launch_count)``.  "Idle" means zero active
        run watchers AND zero active watch-launch tasks — the two things that would
        lose data if interrupted.  Queued runs are persisted to disk and survive a
        recycle, so they do NOT block idleness.

        Shared between the proactive stale-pre-launch check (which recycles BEFORE
        launching stale work) and the idle auto-recycle fallback (which cleans up
        when the fleet is completely quiet).
        """
        active_watchers = [
            t for t in self._run_engine.watcher_tasks() if not t.done()
        ]
        active_launches = [
            t for t in self._watch_launch_tasks.values() if not t.done()
        ]
        return (
            len(active_watchers) == 0 and len(active_launches) == 0,
            len(active_watchers),
            len(active_launches),
        )

    async def _maybe_auto_recycle(self) -> None:
        """If the manager's code is stale AND the manager is idle, gracefully self-
        restart so a pushed code fix takes effect without a manual recycle.

        Called from the scheduler loop on every tick.  Requires staleness to
        persist for ≥2 ticks before acting — anti-flap for ``git checkout``
        during manual work.

        This is the *idle* fallback: it fires when the fleet is quiet and stale,
        even if no new work is pending.  The *proactive* path lives in
        ``_scheduler_tick`` — when work IS pending and the fleet is idle, that
        path recycles BEFORE launching, so the fresh manager handles the work.
        """
        st = self.staleness()
        if not st["stale"]:
            self._stale_since_tick_seq = None
            return

        # Stale — track persistence. The first tick that sees staleness stamps
        # the sequence; subsequent ticks must confirm it for ≥2 ticks before we
        # act (anti-flap: a brief checkout during manual work won't trigger).
        if self._stale_since_tick_seq is None:
            self._stale_since_tick_seq = self._scheduler_tick_seq
            return

        if self._scheduler_tick_seq - self._stale_since_tick_seq < 2:
            return

        # ── idle guards ───────────────────────────────────────────────────
        idle, n_watchers, n_launches = self._fleet_idle()
        if not idle:
            self._log.info(
                "auto-recycle deferred: %d watcher(s), %d launch task(s) "
                "(stale %d ticks)",
                n_watchers, n_launches,
                self._scheduler_tick_seq - self._stale_since_tick_seq,
            )
            return

        # All clear — recycle.
        self._log.warning(
            "auto-recycle: code is stale (%s → %s) and manager is idle — "
            "shutting down for lazy respawn with current code",
            st["loaded_sha"], st["repo_head"],
        )
        await self._self_shutdown("stale")

    # ── TCP server ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Write PID file so the dashboard can find us.
        pid_path = os.path.join(self._workspace, "logs", "agent_manager.pid")
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

        # Start the clock scheduler loop alongside the TCP server. It drives
        # clock-triggered agents; manual run-now flows through the TCP handlers
        # independently, so the loop never gates the manual path.
        self._scheduler_task = asyncio.create_task(self._run_scheduler_loop())

        # Snapshot-warm loop (#151 perf): keeps the gh half of the agent-panel
        # snapshot hot while the panel is open. Idle (and gh-free) otherwise.
        self._snapshot_warm_task = asyncio.create_task(self._run_snapshot_warm_loop())

        async with server:
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                # server.close() (the shutdown RPC) cancels the serve_forever
                # future — this is the graceful-exit path, not an error.
                pass
        self._log.info("TCP server stopped — agent-manager exiting")

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

        # The dispatch is wrapped so a handler that RAISES (e.g. a `gh` failure on
        # a PM approve/reject click) can't escape: an uncaught handler exception
        # left the client to time out with a generic "manager unreachable" that
        # MASKED the real error, and the socket leaked until GC. We send a truthful
        # error frame and close the writer — the same containment _cmd_resolve_blocker
        # does inline, now applied to every command (#121, L1). Individual handlers
        # still own their own happy-path writer.close(); this only catches the
        # raise-before-reply path.
        try:
            await self._dispatch(msg_type, writer, msg)
        except Exception as e:
            self._log.exception("handler for %r raised", msg_type)
            await self._send_line(
                writer, {"type": "error", "error": f"manager handler error: {e}"}
            )
            writer.close()

    async def _dispatch(
        self, msg_type, writer: asyncio.StreamWriter, msg: dict
    ) -> None:
        """Route one decoded request to its command handler. Raising is contained
        by the caller (_handle_client) into an error frame + socket close."""
        if msg_type == "ping":
            await self._cmd_ping(writer)
        elif msg_type == "list_agents":
            await self._cmd_list_agents(writer)
        elif msg_type == "get_agent":
            await self._cmd_get_agent(writer, msg)
        elif msg_type == "upsert_agent":
            await self._cmd_upsert_agent(writer, msg)
        elif msg_type == "delete_agent":
            await self._cmd_delete_agent(writer, msg)
        elif msg_type == "set_enabled":
            await self._cmd_set_enabled(writer, msg)
        elif msg_type == "get_watch_gate":
            await self._cmd_get_watch_gate(writer)
        elif msg_type == "get_pill_counts":
            await self._cmd_get_pill_counts(writer)
        elif msg_type == "pulse_status":
            await self._cmd_pulse_status(writer)
        elif msg_type == "set_watch_gate":
            await self._cmd_set_watch_gate(writer, msg)
        elif msg_type == "get_snapshot":
            await self._cmd_get_snapshot(writer, msg)
        elif msg_type == "get_supervised":
            await self._cmd_get_supervised(writer)
        elif msg_type == "set_supervised":
            await self._cmd_set_supervised(writer, msg)
        elif msg_type == "approve_issue":
            await self._cmd_approve_issue(writer, msg)
        elif msg_type == "reject_issue":
            await self._cmd_reject_issue(writer, msg)
        elif msg_type == "trade_approve_issue":
            await self._cmd_trade_approve_issue(writer, msg)
        elif msg_type == "list_trade_approval_issues":
            await self._cmd_list_trade_approval_issues(writer)
        elif msg_type == "trade_approval_brief":
            await self._cmd_trade_approval_brief(writer, msg)
        elif msg_type == "ack_completed":
            await self._cmd_ack_completed(writer)
        elif msg_type == "ack_problem":
            await self._cmd_ack_problem(writer, msg)
        elif msg_type == "loop_status":
            await self._cmd_loop_status(writer)
        elif msg_type == "resolve_blocker":
            await self._cmd_resolve_blocker(writer, msg)
        elif msg_type == "run_now":
            await self._cmd_run_now(writer, msg)
        elif msg_type == "list_runs":
            await self._cmd_list_runs(writer)
        elif msg_type == "get_run":
            await self._cmd_get_run(writer, msg)
        elif msg_type == "cancel_run":
            await self._cmd_cancel_run(writer, msg)
        elif msg_type == "get_staleness":
            await self._cmd_get_staleness(writer)
        elif msg_type == "shutdown":
            await self._cmd_shutdown(writer)
        else:
            writer.close()

    # ── command handlers ────────────────────────────────────────────────────

    async def _cmd_ping(self, writer: asyncio.StreamWriter) -> None:
        await self._send_line(writer, {"type": "pong"})
        writer.close()

    async def _cmd_list_agents(self, writer: asyncio.StreamWriter) -> None:
        agents = self._list_agents()
        await self._send_line(writer, {"type": "agents", "agents": agents})
        writer.close()

    async def _cmd_get_agent(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        agent_id = msg.get("id")
        if not isinstance(agent_id, str):
            await self._send_line(writer, {"type": "error", "error": "id is required"})
            writer.close()
            return
        agent = self._get_agent(agent_id)
        await self._send_line(writer, {"type": "agent", "agent": agent})
        writer.close()

    async def _cmd_upsert_agent(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        defn = msg.get("agent")
        try:
            agent = self._upsert_agent(defn)
        except AgentValidationError as e:
            await self._send_line(writer, {"type": "error", "error": str(e)})
            writer.close()
            return
        await self._send_line(writer, {"type": "agent", "agent": agent})
        writer.close()

    async def _cmd_delete_agent(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        agent_id = msg.get("id")
        if not isinstance(agent_id, str):
            await self._send_line(writer, {"type": "error", "error": "id is required"})
            writer.close()
            return
        deleted = self._delete_agent(agent_id)
        await self._send_line(writer, {"type": "deleted", "deleted": deleted})
        writer.close()

    async def _cmd_set_enabled(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """Flip an agent's enabled flag (issue #39). 'agent' result, or an error
        if the id is bad / the agent is unknown."""
        agent_id = msg.get("id")
        enabled = msg.get("enabled")
        if not isinstance(agent_id, str):
            await self._send_line(writer, {"type": "error", "error": "id is required"})
            writer.close()
            return
        if not isinstance(enabled, bool):
            await self._send_line(
                writer, {"type": "error", "error": "enabled must be a boolean"})
            writer.close()
            return
        try:
            agent = self._set_enabled(agent_id, enabled)
        except AgentValidationError as e:
            # set_enabled re-validates the existing def via upsert, so a legacy or
            # hand-edited on-disk file that is invalid in some OTHER field raises
            # here. Reply with the reason (like _cmd_upsert_agent) instead of
            # letting it propagate and leave the client hanging on an unanswered
            # connection (the dispatch loop has no catch-all).
            await self._send_line(writer, {"type": "error", "error": str(e)})
            writer.close()
            return
        if agent is None:
            await self._send_line(
                writer, {"type": "error", "error": f"unknown agent: {agent_id!r}"})
            writer.close()
            return
        await self._send_line(writer, {"type": "agent", "agent": agent})
        writer.close()

    async def _cmd_get_watch_gate(self, writer: asyncio.StreamWriter) -> None:
        """Report the master watch-gate arm state (toggle / env-override / effective)."""
        await self._send_line(
            writer, {"type": "watch_gate", "watch_gate": self.watch_gate_status()})
        writer.close()

    async def _cmd_get_pill_counts(self, writer: asyncio.StreamWriter) -> None:
        """Report the dashboard four notification-pill counts
        (green / yellow / purple / red — see derive_pill_counts, #65).
        Mirrors _cmd_get_watch_gate. pill_counts() is best-effort (zeros on a
        gateway error) so this never errors the connection.

        Offloaded to a thread for the same reason as _cmd_loop_status: pill_counts()
        makes several gh calls and would otherwise block the async server inline."""
        counts = await asyncio.to_thread(self.pill_counts)
        await self._send_line(writer, {"type": "pill_counts", "pill_counts": counts})
        writer.close()

    async def _cmd_pulse_status(self, writer: asyncio.StreamWriter) -> None:
        """Report the cheap AGENTS-tab glow/pulse signal (active-run flag +
        scheduler-tick counter — see pulse_status). No gh, so it runs inline; no
        thread offload like _cmd_loop_status / _cmd_get_pill_counts needs."""
        await self._send_line(writer, {"type": "pulse", "pulse": self.pulse_status()})
        writer.close()

    async def _cmd_get_snapshot(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """The consolidated agent-panel view-model (see AgentStateSnapshot, #149).
        Body may carry ``sections``: ``"full"`` (default — all six keys),
        ``"cheap"`` (in-memory half: runs, pulse, watch_gate, supervised), or
        ``"gh"`` (GitHub half: pill_counts + loop_health). The live push fetches
        ``cheap`` then ``gh`` so the cards render before the slow gh reads land
        (#151). An unknown value falls back to ``full``. Best-effort per-section
        so a degraded section never throws the whole snapshot out.

        A gh-bearing request (``full``/``gh``) arms the snapshot-warm window so
        the warm loop keeps the gh caches hot while the panel keeps polling.

        Offloaded to a thread: the gh half reaches pill_counts() + loop_status()
        (~8 gh calls total), which would block the single-threaded async server
        inline (~3-5s wall when cold). The cheap half has no gh calls but rides
        the same thread for uniformity."""
        sections = msg.get("sections") or "full"
        if sections not in ("full", "cheap", "gh"):
            sections = "full"
        if sections in ("full", "gh"):
            self._arm_snapshot_warm()
        snap = await asyncio.to_thread(self.snapshot, sections)
        await self._send_line(writer, {"type": "snapshot", "snapshot": snap})
        writer.close()

    async def _cmd_set_watch_gate(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """Arm/disarm the autonomous watch loop (master toggle). Body carries
        ``enabled: bool``; returns the new arm status."""
        enabled = msg.get("enabled")
        if not isinstance(enabled, bool):
            await self._send_line(
                writer, {"type": "error", "error": "enabled must be a boolean"})
            writer.close()
            return
        status = self.set_watch_gate(enabled)
        await self._send_line(writer, {"type": "watch_gate", "watch_gate": status})
        writer.close()

    async def _cmd_get_supervised(self, writer: asyncio.StreamWriter) -> None:
        """Report the per-project supervised-mode state (toggle / effective, #66)."""
        await self._send_line(
            writer, {"type": "supervised", "supervised": self.supervised_status()})
        writer.close()

    async def _cmd_set_supervised(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """Turn supervised mode ON/OFF (#66). Body carries ``enabled: bool``;
        returns the new status. Mirrors _cmd_set_watch_gate."""
        enabled = msg.get("enabled")
        if not isinstance(enabled, bool):
            await self._send_line(
                writer, {"type": "error", "error": "enabled must be a boolean"})
            writer.close()
            return
        status = self.set_supervised(enabled)
        await self._send_line(writer, {"type": "supervised", "supervised": status})
        writer.close()

    async def _cmd_approve_issue(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """PM sign-off APPROVE (Slice C). Body carries ``number: int``; flips the
        issue needs-signoff → approved and logs the PM decision. Replies
        ``{"ok": true}`` on success, or an error tagged code:"bad_request" for a
        missing/non-int number."""
        number = msg.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            await self._send_line(
                writer,
                {"type": "error", "error": "number must be an int",
                 "code": "bad_request"})
            writer.close()
            return
        self.approve_issue(number)
        await self._send_line(writer, {"type": "ok", "ok": True})
        writer.close()

    async def _cmd_reject_issue(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """PM sign-off REJECT (Slice C). Body carries ``number: int``; flips the
        issue needs-signoff → rejected and logs the PM decision. Same reply
        contract as _cmd_approve_issue."""
        number = msg.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            await self._send_line(
                writer,
                {"type": "error", "error": "number must be an int",
                 "code": "bad_request"})
            writer.close()
            return
        self.reject_issue(number)
        await self._send_line(writer, {"type": "ok", "ok": True})
        writer.close()

    async def _cmd_trade_approve_issue(
        self, writer: asyncio.StreamWriter, msg: dict
    ) -> None:
        """🟣 TRADING-SURFACE APPROVE (#65). Body carries ``number: int``; applies
        the trade-approved clearance + approved labels (the #63 two-pass handshake)
        so Eli re-polls and proceeds. Same reply / bad_request contract as
        _cmd_approve_issue. There is NO reject counterpart — approving is the only
        way to clear the money wall."""
        number = msg.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            await self._send_line(
                writer,
                {"type": "error", "error": "number must be an int",
                 "code": "bad_request"})
            writer.close()
            return
        self.trade_approve_issue(number)
        await self._send_line(writer, {"type": "ok", "ok": True})
        writer.close()

    async def _cmd_list_trade_approval_issues(
        self, writer: asyncio.StreamWriter
    ) -> None:
        """The 🟣 inbox: ``{type:"trade_approval_issues", issues:[{number,title}]}``.
        Best-effort (empty list on a gateway error)."""
        await self._send_line(
            writer,
            {"type": "trade_approval_issues",
             "issues": self.list_trade_approval_issues()})
        writer.close()

    async def _cmd_trade_approval_brief(
        self, writer: asyncio.StreamWriter, msg: dict
    ) -> None:
        """The 🟣 view payload for an issue: ``{number, title, brief}``. Body
        carries ``number: int``. Best-effort (a gateway error yields a generic
        brief, never an error frame)."""
        number = msg.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            await self._send_line(
                writer,
                {"type": "error", "error": "number must be an int",
                 "code": "bad_request"})
            writer.close()
            return
        await self._send_line(
            writer,
            {"type": "trade_approval_brief", "brief": self.trade_approval_brief(number)})
        writer.close()

    async def _cmd_ack_completed(self, writer: asyncio.StreamWriter) -> None:
        """🟢 ACK COMPLETED (#65): mark all currently-succeeded runs SEEN so the
        green pill clears on viewing. Replies ``{type:"acked", acked:<int>}``."""
        result = self.ack_completed()
        await self._send_line(writer, {"type": "acked", **result})
        writer.close()

    async def _cmd_ack_problem(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """🔴 ACK PROBLEM: mark one halted run acknowledged so it drops out of the
        red pill. Replies ``{type:"acked", acked:<int>}``."""
        run_id = msg.get("run_id")
        if not isinstance(run_id, str):
            await self._send_line(writer, {"type": "error", "error": "run_id is required"})
            writer.close()
            return
        result = self.ack_problem(run_id)
        await self._send_line(writer, {"type": "acked", **result})
        writer.close()

    async def _cmd_loop_status(self, writer: asyncio.StreamWriter) -> None:
        """Report the loop-health snapshot for the dashboard (arm/freeze state,
        in-flight/PR-cap, per-stage running flags, blockers — see loop_status).
        Best-effort like pill_counts (degrades on a gateway error).

        Offloaded to a worker thread: loop_status() shells out to gh ~5 times
        (~3-5s wall), and running it INLINE froze the single-threaded async server
        for the whole call — starving every other RPC (list_agents / list_runs /
        pill_counts) so the dashboard flickered and looked empty. ``asyncio.to_thread``
        keeps the event loop responsive while the gh calls run (mirrors the pipeline
        offload at _watch_run)."""
        status = await asyncio.to_thread(self.loop_status)
        await self._send_line(writer, {"type": "loop_status", "status": status})
        writer.close()

    async def _cmd_resolve_blocker(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """Perform one mechanical unblock (clear_freeze / close_pr — see
        resolve_blocker). Replies ``{type:"resolved", **result}``; a bad action
        is a ValueError → error, a gateway failure → logged error."""
        action = msg.get("action")
        try:
            # Offloaded — resolve_blocker shells out to gh (close_pr) and must not
            # block the event loop (same reason as _cmd_loop_status).
            result = await asyncio.to_thread(self.resolve_blocker, action)
            await self._send_line(writer, {"type": "resolved", **result})
        except ValueError as e:
            await self._send_line(writer, {"type": "error", "error": str(e)})
        except Exception as e:  # e.g. GitHubGatewayError from a close_pr failure
            self._log.warning("resolve_blocker failed", exc_info=True)
            await self._send_line(writer, {"type": "error", "error": str(e)})
        finally:
            writer.close()

    # ── run lifecycle handlers (issue #30) ────────────────────────────────────

    async def _cmd_run_now(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        agent_id = msg.get("id")
        if not isinstance(agent_id, str):
            await self._send_line(writer, {"type": "error", "error": "id is required"})
            writer.close()
            return
        try:
            run = await self._run_now(agent_id)
        except KeyError:
            await self._send_line(
                writer, {"type": "error", "error": f"unknown agent: {agent_id!r}"}
            )
            writer.close()
            return
        except AgentDisabledError as e:
            # OFF is a hard stop (#40): tag the error so the route returns a 409
            # (distinct from the unknown-agent 404).
            await self._send_line(
                writer, {"type": "error", "error": str(e), "code": "disabled"}
            )
            writer.close()
            return
        await self._send_line(writer, {"type": "run", "run": run})
        writer.close()

    async def _cmd_list_runs(self, writer: asyncio.StreamWriter) -> None:
        runs = self._list_runs()
        acked = self._problems_ack.load()
        for r in runs:
            r["acked"] = r.get("run_id") in acked
            # Strip the heavy per-stage command output (``activity[].log``) from the
            # LIST payload. The list / completed / problems views never render it —
            # only the run-DETAIL feed does, and that fetches the full record via
            # get_run. Each land-run carries up to MAX_ACTIVITY_LOG_CHARS (~20KB) of
            # log; left in, ~85 fat runs pushed the single-line list response past
            # the client's StreamReader limit, so readline() raised and the dashboard
            # read it as "manager unreachable" — every agent panel emptied at once.
            # The records are freshly json.loads'd per list() call, so rebuilding
            # activity here mutates nothing shared. See agent_client.
            acts = r.get("activity")
            if acts:
                r["activity"] = [
                    {k: v for k, v in ev.items() if k != "log"} for ev in acts
                ]
        await self._send_line(writer, {"type": "runs", "runs": runs})
        writer.close()

    async def _cmd_get_run(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        run_id = msg.get("run_id")
        if not isinstance(run_id, str):
            await self._send_line(writer, {"type": "error", "error": "run_id is required"})
            writer.close()
            return
        run = self._get_run(run_id)
        if run is not None:
            run["acked"] = run.get("run_id") in self._problems_ack.load()
        await self._send_line(writer, {"type": "run", "run": run})
        writer.close()

    async def _cmd_cancel_run(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        run_id = msg.get("run_id")
        if not isinstance(run_id, str):
            await self._send_line(writer, {"type": "error", "error": "run_id is required"})
            writer.close()
            return
        try:
            run = await self._cancel_run(run_id)
        except KeyError:
            await self._send_line(
                writer, {"type": "error", "error": f"unknown run: {run_id!r}"}
            )
            writer.close()
            return
        await self._send_line(writer, {"type": "run", "run": run})
        writer.close()

    async def _cmd_get_staleness(self, writer: asyncio.StreamWriter) -> None:
        """Report the manager's loaded SHA vs current repo HEAD (#182).
        ``{loaded_sha, repo_head, stale}`` — stale=True means the running daemon
        was loaded from a different commit than the current checkout. Cheap (one
        git rev-parse), so it runs inline."""
        await self._send_line(
            writer, {"type": "staleness", "staleness": self.staleness()})
        writer.close()

    async def _cmd_shutdown(self, writer: asyncio.StreamWriter) -> None:
        """Graceful full shutdown requested by the dashboard.

        Ack the request, flush the socket, then tear down via _self_shutdown —
        the same teardown the auto-recycle path (#198) shares. The dashboard
        respawns the manager lazily on the next agent API access.

        The PID file is intentionally left in place: no other exit path removes
        it (matching the pty-manager), and the next manager start overwrites it.
        The client-side fallback verifies the command line of whatever PID the
        file names before ever killing it, so a stale file is harmless.
        """
        self._log.info("shutdown RPC received")

        await self._send_line(writer, {"type": "shutdown", "ok": True})
        # Make sure the ack is fully written before tearing the server down —
        # wait_closed() drains the transport, so the exit is scheduled strictly
        # after the response bytes have left this process.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        await self._self_shutdown("rpc")

    async def _self_shutdown(self, reason: str) -> None:
        """Tear down the manager process from within.

        Shared between the RPC handler (_cmd_shutdown, which acks the caller
        first) and the auto-recycle path (_maybe_auto_recycle, which calls this
        directly). The caller owns whether any client was acknowledged — this
        method only does the teardown: cancel scheduler + snapshot-warm loops,
        drain background tasks, close the TCP server.
        """
        self._log.warning("self-shutdown: %s", reason)

        # Stop the scheduler loop so it doesn't fire a run during teardown.
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()

        # Stop the snapshot-warm loop (#151) — no gh refresh during teardown.
        if self._snapshot_warm_task is not None and not self._snapshot_warm_task.done():
            self._snapshot_warm_task.cancel()

        # Drain the tracked background tasks (#114): cancel the backgrounded watch
        # launches and the run-session watchers and await them, so the process
        # exits with no orphaned/pending asyncio tasks.
        await self._drain_background_tasks()

        if self._server is not None:
            # close() cancels the serve_forever future; start() then returns
            # and the process exits.
            self._server.close()

    async def _send_line(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        try:
            writer.write((json.dumps(obj) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# RunStoreRecorder — bridge the issue→PR pipeline to the #30 run store
# ═══════════════════════════════════════════════════════════════════════════════

class _RunStoreRecorder:
    """Records a watch agent's issue→PR cycle as a Run in the #30 run store, so a
    watch run is auditable in the dashboard exactly like a manual/clock run.

    One run per claimed issue, id ``watch-issue-<n>-<6hex>``. start() writes a
    running record; finish() walks it to succeeded (green PR) or failed (draft /
    error path). Failures persist a clear outcome so a human sees WHY it bounced.
    """

    def __init__(self, mgr: "AgentManagerServer") -> None:
        self._mgr = mgr
        self._run_ids: dict[int, str] = {}  # issue_number → run_id

    def start(self, issue) -> None:
        run_id = f"watch-issue-{issue.number}-{secrets.token_hex(3)}"
        self._run_ids[issue.number] = run_id
        try:
            self._mgr._put_run({
                "run_id": run_id,
                "agent_id": f"watch-issue-{issue.number}",
                "tag": f"agent:{run_id}",
                "command": None,
                "cwd": self._mgr._workspace,
                "session_id": None,
                "status": RUNNING,
                "outcome": f"issue→PR: {issue.title}",
                "created_at": time.time(),
                "started_at": time.time(),
                "finished_at": None,
            })
        except RunValidationError:
            self._mgr._log.exception("could not record watch run for #%d", issue.number)

    def worktree_ready(self, issue_number: int, *, cwd, branch: str) -> None:
        """Repoint the run's cwd at the worktree once it exists.

        start() had to write the record before the worktree was created, so it
        used the repo root. The dashboard's run feed derives the transcript dir
        from cwd (Claude Code slugifies the launch cwd), and this run's claude
        ran IN the worktree — so leaving cwd at the repo root made the feed
        resolve the newest unrelated transcript in the operator's own project
        dir. Pin the worktree here so the feed lands on this run's transcript
        ([[footguns#171]], 2026-06-21)."""
        run_id = self._run_ids.get(issue_number)
        if run_id is None:
            return
        try:
            self._mgr._update_run(run_id, cwd=str(cwd))
        except RunValidationError:
            self._mgr._log.exception("could not pin worktree cwd on run %s", run_id)

    def attach_session(self, issue_number: int, *, transcript_id: str,
                       cwd=None) -> None:
        """Pin the Claude transcript id (and optionally repoint cwd) on the run.

        The pipeline calls this right before launching claude, once it knows the
        ``--session-id`` uuid it pinned. The dashboard feed resolves the run's
        transcript by this id, so without it the feed falls back to the newest
        unrelated transcript in the project dir (the cross-wiring bug). ``cwd``
        repoints the run at the dir claude actually launches in when that differs
        from the dir start() recorded (the watch path already pins its worktree
        via ``worktree_ready``, so it passes no cwd here)."""
        run_id = self._run_ids.get(issue_number)
        if run_id is None:
            return
        changes = {"transcript_id": transcript_id}
        if cwd is not None:
            changes["cwd"] = str(cwd)
        try:
            self._mgr._update_run(run_id, **changes)
        except RunValidationError:
            self._mgr._log.exception("could not attach transcript id on run %s", run_id)

    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url, note: str = "") -> None:
        run_id = self._run_ids.pop(issue_number, None)
        if run_id is None:
            return
        outcome = (
            f"opened PR {pr_url}" if (succeeded and pr_url)
            else (note or f"draft/failed PR {pr_url or ''}".strip())
        )
        try:
            self._mgr._update_run(
                run_id,
                status=SUCCEEDED if succeeded else FAILED,
                outcome=outcome,
                finished_at=time.time(),
            )
        except RunValidationError:
            self._mgr._log.exception("could not finish watch run %s", run_id)
        # The watch run's claude session has ended (the pipeline runs it
        # synchronously and only calls finish() after), so its transcript is
        # flushed. Measure + stamp the consumed-token total and apply the #73
        # guardrail. The over-300k halt files back on THIS issue. Best-effort —
        # token accounting must never crash the pipeline's completion.
        try:
            self._mgr._stamp_run_tokens(run_id, issue_number=issue_number)
        except Exception:
            self._mgr._log.exception("token stamping failed for watch run %s", run_id)

    def halt(self, issue_number: int, *, report: str) -> None:
        """Persist the run as HALTED (Slice F) with the formatted halt report as
        its outcome. Mirrors finish() — pops the issue→run mapping and walks the
        record to the terminal ``halted`` status (distinct from failed)."""
        run_id = self._run_ids.pop(issue_number, None)
        if run_id is None:
            return
        try:
            self._mgr._mark_halted(run_id, note=report, finished_at=time.time())
        except RunValidationError:
            self._mgr._log.exception("could not halt watch run %s", run_id)
        # Even a halted run still consumed tokens — measure + stamp so the
        # per-issue rollup is complete, and surface a runaway halt (>300k) even on
        # an already-halted run. Best-effort.
        try:
            self._mgr._stamp_run_tokens(run_id, issue_number=issue_number)
        except Exception:
            self._mgr._log.exception("token stamping failed for halted watch run %s", run_id)


# ═══════════════════════════════════════════════════════════════════════════════
# StageRunRecorder — bridge Kleiner / Eli to the #30 run store (#78)
# ═══════════════════════════════════════════════════════════════════════════════

class _StageRunRecorder:
    """Records a review (Kleiner) or land (Eli) cycle as a Run in the #30 run
    store, so each stage is auditable in the dashboard exactly like a watch
    (Gordon) run.

    A sibling of ``_RunStoreRecorder`` parameterized by an agent_id PREFIX, so a
    review run is keyed ``review-issue-<n>`` and a land run ``land-issue-<n>`` —
    distinct from Gordon's ``watch-issue-<n>`` so the per-stage overlap guards
    (``_count_running_stage_runs``) can tell them apart. Same start/finish/halt
    shape the review + Eli pipelines call (no ``worktree_ready`` — those stages run
    in a throwaway temp dir / on the integration tree, not a per-run worktree).

    Token stamping rides finish()/halt() (the per-session #73 total) exactly like
    the watch recorder, so Eli's full-chain rollup sees every stage's cost."""

    def __init__(self, mgr: "AgentManagerServer", *, prefix: str, label: str) -> None:
        self._mgr = mgr
        self._prefix = prefix     # "review-issue" / "land-issue"
        self._label = label       # human stage word for the outcome line
        self._run_ids: dict[int, str] = {}  # issue_number → run_id

    def start(self, issue) -> None:
        run_id = f"{self._prefix}-{issue.number}-{secrets.token_hex(3)}"
        self._run_ids[issue.number] = run_id
        try:
            self._mgr._put_run({
                "run_id": run_id,
                "agent_id": f"{self._prefix}-{issue.number}",
                "tag": f"agent:{run_id}",
                "command": None,
                "cwd": self._mgr._workspace,
                "session_id": None,
                "status": RUNNING,
                "outcome": f"{self._label}: {issue.title}",
                "created_at": time.time(),
                "started_at": time.time(),
                "finished_at": None,
            })
        except RunValidationError:
            self._mgr._log.exception(
                "could not record %s run for #%d", self._label, issue.number)

    def attach_session(self, issue_number: int, *, transcript_id: str,
                       cwd=None) -> None:
        """Pin the Claude transcript id (and optionally repoint cwd) on the run.

        Mirrors ``_RunStoreRecorder.attach_session``. A stage run (review/land)
        records cwd=repo root in start() but the reviewer's claude launches in a
        throwaway temp workdir, so the feed needs BOTH the transcript id and the
        real workdir to resolve this run's own transcript instead of the newest
        unrelated one in the operator's project dir (the cross-wiring bug)."""
        run_id = self._run_ids.get(issue_number)
        if run_id is None:
            return
        changes = {"transcript_id": transcript_id}
        if cwd is not None:
            changes["cwd"] = str(cwd)
        try:
            self._mgr._update_run(run_id, **changes)
        except RunValidationError:
            self._mgr._log.exception(
                "could not attach transcript id on %s run %s", self._label, run_id)

    def stage_event(self, issue_number: int, *, stage: str, status: str,
                    detail: str = "", log: str = "") -> None:
        """Append one structured stage event to the run's ``activity`` log.

        A transcript-less stage run (Eli/land — no Claude session of its own)
        has no transcript for the dashboard feed to read, so it records its
        progress here instead: one event per pipeline stage (merge → bundle →
        test → push, or the stage it halted on). ``log`` is that stage's FULL
        terminal output (the real git/npm/pytest run) — the feed renders it as an
        expandable block so a finished land can be inspected, not just summarized;
        the run-store normalizer drops it when empty. The feed assembler renders
        this log as ``kind="stage"`` events. Read-modify-write so events accumulate
        in order; best-effort — a recording hiccup must never unwind the land."""
        run_id = self._run_ids.get(issue_number)
        if run_id is None:
            return
        try:
            current = self._mgr._get_run(run_id)
            activity = list((current or {}).get("activity") or [])
            entry = {
                "stage": stage, "status": status,
                "detail": detail, "ts": time.time(),
            }
            if log:
                entry["log"] = log
            activity.append(entry)
            self._mgr._update_run(run_id, activity=activity)
        except RunValidationError:
            self._mgr._log.exception(
                "could not record %s stage event on run %s", self._label, run_id)

    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url, note: str = "") -> None:
        run_id = self._run_ids.pop(issue_number, None)
        if run_id is None:
            return
        outcome = note or (f"{self._label} succeeded" if succeeded else f"{self._label} failed")
        if succeeded and pr_url:
            outcome = f"{outcome} ({pr_url})"
        try:
            self._mgr._update_run(
                run_id,
                status=SUCCEEDED if succeeded else FAILED,
                outcome=outcome,
                finished_at=time.time(),
            )
        except RunValidationError:
            self._mgr._log.exception("could not finish %s run %s", self._label, run_id)
        try:
            self._mgr._stamp_run_tokens(run_id, issue_number=issue_number)
        except Exception:
            self._mgr._log.exception(
                "token stamping failed for %s run %s", self._label, run_id)

    def halt(self, issue_number: int, *, report: str) -> None:
        run_id = self._run_ids.pop(issue_number, None)
        if run_id is None:
            return
        try:
            self._mgr._mark_halted(run_id, note=report, finished_at=time.time())
        except RunValidationError:
            self._mgr._log.exception("could not halt %s run %s", self._label, run_id)
        try:
            self._mgr._stamp_run_tokens(run_id, issue_number=issue_number)
        except Exception:
            self._mgr._log.exception(
                "token stamping failed for halted %s run %s", self._label, run_id)


# ═══════════════════════════════════════════════════════════════════════════════
# RunStoreTokenRollup — bridge the run store's per-session totals to Eli (#73)
# ═══════════════════════════════════════════════════════════════════════════════

class _RunStoreTokenRollup:
    """The run-store-backed :class:`~agents.eli_pipeline.TokenRollup` the manager
    injects into Eli's pipeline so Eli's close comments carry the full-chain cost.

    The MANAGER is the only actor that can do this: it stamps EVERY session's
    consumed-token total at run-complete (including Eli's own, once Eli's run
    finishes), so by the time Eli's finalize posts the close note the chain is
    fully measured. Eli never sums its own still-running session — it asks here for
    the already-measured breakdown.

    Both methods are best-effort and return None on any error / nothing-measured,
    so a token-accounting hiccup never blocks Eli's land or close."""

    def __init__(self, mgr: "AgentManagerServer") -> None:
        self._mgr = mgr

    def issue_breakdown(self, issue_number: int) -> str | None:
        """The per-issue chain breakdown string ("Gordon 121k + … = 163k"), or
        None when no session of this issue has a measured total yet."""
        from agents.tokens import format_issue_breakdown
        try:
            parts = self._mgr._token_guardrail.issue_chain_tokens(issue_number)
        except Exception:
            self._mgr._log.exception("issue_breakdown failed for #%d", issue_number)
            return None
        if not parts:
            return None
        return format_issue_breakdown(parts)

    def prd_rollup(self, prd_number: int, child_issue_numbers: list[int]) -> str | None:
        """The per-PRD rollup string ("PRD #58: ~211k across N slices"), or None
        when no child has a measured total.

        ``child_issue_numbers`` is the seed set Eli can name from its close context
        (the just-landed child). We UNION it with every other issue that has run
        records keyed to it (``watch-issue-<n>``) AND whose parent is this PRD, so a
        sibling that landed earlier (and is already closed) is still counted. Each
        child contributes its chain total via the manager's ``issue_chain_total``."""
        from agents.tokens import format_prd_rollup
        try:
            children = self._discover_prd_children(prd_number, child_issue_numbers)
        except Exception:
            self._mgr._log.exception("prd_rollup child discovery failed for #%d", prd_number)
            children = sorted(set(child_issue_numbers or []))
        totals = [self._mgr._token_guardrail.issue_chain_total(n) for n in children]
        totals = [t for t in totals if t > 0]
        if not totals:
            return None
        return format_prd_rollup(prd_number, totals)

    def _discover_prd_children(
        self, prd_number: int, seed: list[int]
    ) -> list[int]:
        """The set of child issue numbers of ``prd_number`` that have a run record.

        Starts from ``seed`` (what Eli could name) and adds every issue with a
        ``watch-issue-<n>`` run whose body names ``Parent #<prd_number>`` (resolved
        via the gateway). Gateway failures degrade to just the seed — the rollup is
        best-effort. De-duplicated + sorted for a deterministic comment."""
        found: set[int] = set(seed or [])
        # Issue numbers that have a run record in the loop.
        run_issue_numbers = {
            _issue_from_watch_agent_id(str(r.get("agent_id", "")))
            for r in self._mgr._list_runs()
        }
        run_issue_numbers.discard(None)
        candidates = run_issue_numbers - found
        if candidates:
            gw = self._mgr._gateway_factory()
            # One open-issue listing maps number → parent_refs cheaply; a child
            # already CLOSED won't appear, but a closed child is one already
            # rolled up at its own land time — the seed covers the just-landed one.
            try:
                parent_of = {
                    iss.number: iss.parent_refs for iss in gw.list_open_issues()
                }
            except Exception:
                parent_of = {}
            for n in candidates:
                if prd_number in parent_of.get(n, []):
                    found.add(n)
        return sorted(found)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Trident Agent Manager")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--workspace",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Trident repo root (default: directory containing this script)",
    )
    args = parser.parse_args()

    log = _setup_logging(args.workspace)
    log.info("agent-manager starting — port=%d workspace=%s", args.port, args.workspace)

    # Provider env vars (claude vs deepseek) are injected directly into each
    # agent's PowerShell command string at launch time — see
    # provider.get_provider_env_prefix(). No process-level
    # env-vars needed here; the next run picks up a toggle instantly.

    # Dump a stack trace on a fatal native signal before the process dies —
    # catches the crash class that bypasses Python's except-hook. Writes to a
    # DEDICATED file this process opens itself (not stderr), so it works on a
    # plain manager respawn without needing the spawner's stderr redirect (which
    # requires a dashboard restart). The handle lives for the whole process
    # (asyncio.run blocks below). Append mode preserves dumps across respawns.
    try:
        fault_path = os.path.join(args.workspace, "logs", "agent_manager.faulthandler.log")
        _fault_log = open(fault_path, "a")
        faulthandler.enable(file=_fault_log, all_threads=True)
    except Exception:
        log.warning("faulthandler.enable() failed — native crashes won't dump")

    manager = AgentManagerServer(port=args.port, workspace=args.workspace)

    try:
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        log.info("agent-manager shutting down (KeyboardInterrupt)")
    except Exception:
        log.exception("agent-manager crashed")
        raise


if __name__ == "__main__":
    main()
