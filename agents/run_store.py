"""
trident_agent_run_store.py — gitignored agent-RUN store (issue #30, parent #24).

What this is
------------
Sibling of trident_agent_store.py. Where that store owns the agent *recipes*
(definitions), this store owns the *runs* — one record per execution of an
agent. The agent-manager creates a Run when an agent is launched (run-now), then
walks its status through the lifecycle and persists each transition here so a
completed run survives a dashboard restart and is readable back through the API.

Layout
------
One file per run at ``<dir>/<run_id>.json`` under a gitignored runtime dir
(``agent_runs/`` — already in .gitignore from Wave 1), in the same atomic-write
idiom as trident_agent_store.py (temp + os.replace). The ``run_id`` is the
filename stem, so it must be a safe slug (``[a-z0-9_-]``) — that doubles as the
path-traversal guard: a run_id can never escape the store dir.

Run record schema
-----------------
::

    {
      "run_id":      "alpha-7f3c1a",       # slug — filename stem (agent + suffix)
      "agent_id":    "alpha",              # the agent definition this run is of
      "tag":         "agent:alpha-7f3c1a", # the pty-manager session tag
      "command":     "& scripts/...",      # the command launched in the terminal
      "cwd":         "C:/.../Trident",     # working directory the run ran in
      "session_id":  "ab12...",            # pty-manager session hosting the run
      "status":      "running",            # queued|running|succeeded|failed|cancelled
      "outcome":     null,                 # free-form final detail (str) or null
      "tokens":      null,                  # consumed-token total for the session
                                            #   (#73; OPTIONAL — absent on old
                                            #    records, stamped by the manager
                                            #    at run-complete from the run's
                                            #    transcript). null = unmeasured.
      "activity":    [],                    # structured stage log for a run that
                                            #   has NO Claude transcript of its own
                                            #   (Eli/land: merge→bundle→test→push).
                                            #   OPTIONAL — absent on old records and
                                            #   on Claude-session runs (whose feed
                                            #   comes from the transcript), so it
                                            #   normalizes to []. Each entry:
                                            #   {stage, status(ok|fail|skip|info),
                                            #    detail, ts, log?}. ``log`` is the
                                            #   stage's full command output (the
                                            #   feed's expandable terminal log) —
                                            #   OPTIONAL, present only when non-empty.
      "created_at":  1718900000.0,         # Run record created (queued)
      "started_at":  1718900001.0,         # terminal launched (running) or null
      "finished_at": null                  # terminal status (succeeded/...) or null
    }

Status is a small state machine:

    queued ──► running ──► succeeded
                       └──► failed
                       └──► cancelled
                       └──► halted      (needs a human decision — Slice F)
    queued ──► failed            (launch never succeeded)

``halted`` is a TERMINAL state distinct from ``failed``: a halt means "this run
hit something that needs your judgment" (the Slice F escape hatch), whereas a
failure means "it broke". Both end the run, but they read very differently to
the PM and are counted by different dashboard pills (red for halted).

Validation raises :class:`RunValidationError` with a clear message. Reads
tolerate a corrupt sibling file (skipped, logged) rather than failing the whole
list — a hand-edited bad file shouldn't break the API.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

_LOG = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

# Default store dir, sibling to trident_config.json at the repo root. Gitignored
# (see .gitignore: `agent_runs/`). The agent-manager / dashboard pass the
# resolved workspace; tests pass a tmp_path.
DEFAULT_DIRNAME = "agent_runs"

# Lifecycle states. The first is the initial state; the last three are terminal.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
# Slice F escape hatch: a run that needs a human decision halts here. Terminal
# and DISTINCT from FAILED — a halt is "needs your judgment", not "it broke".
HALTED = "halted"

VALID_STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, HALTED)
TERMINAL_STATUSES = (SUCCEEDED, FAILED, CANCELLED, HALTED)

# Stage-activity statuses (the structured land-run feed). A run with no Claude
# transcript of its own (Eli/land — pure git/npm/pytest, never a claude session)
# records its progress here instead, one event per pipeline stage:
#   ok   — the stage completed cleanly
#   fail — the stage halted the land (carries the plain-English reason)
#   skip — the stage was not reached / not applicable
#   info — a non-failure note (e.g. a trading-surface wall awaiting clearance)
VALID_ACTIVITY_STATUSES = ("ok", "fail", "skip", "info")

# Cap on a stage's stored ``log`` (the raw command output for the land feed). The
# tail is kept (summaries + failures sit at the END of npm/pytest output), so a
# verbose build can't bloat a run record without bound. Mirrors the pipeline-side
# cap in EliPipeline._STAGE_LOG_CAP.
MAX_ACTIVITY_LOG_CHARS = 20000


def is_terminal(status: str) -> bool:
    """True if ``status`` ends a run (succeeded/failed/cancelled/halted)."""
    return status in TERMINAL_STATUSES


def is_active(status: str) -> bool:
    """True if a run with ``status`` is still in flight (queued/running)."""
    return status in VALID_STATUSES and status not in TERMINAL_STATUSES


def guard_transition(old: str | None, new: str, run_id: str = "") -> None:
    """Enforce the run-store state machine on a status change. PURE (raises only).

    The store accepted ANY valid status over ANY current status, so a UI race
    could overwrite a HALTED run's report with "cancelled by operator", or a
    stale watcher could regress a finished run back to ``running`` (issue #117).
    The rules, from the run's lifecycle (``queued → running → terminal``):

    * No current record (``old is None``) or an unchanged status — always allowed
      (a fresh create, or an idempotent rewrite that only touches other fields
      like ``outcome``/``tokens``).
    * ``HALTED`` is FULLY STICKY — once a run halts (needs a human), nothing may
      move it off HALTED. This is the bug fix: it protects the halt report.
    * Leaving any OTHER terminal state (succeeded/failed/cancelled) is allowed
      ONLY to escalate to ``HALTED`` (the token-guardrail overlay marks an
      already-finished run halted). Any other move out of a terminal state — back
      to active, or laterally to a different terminal — is a rejected regression.

    Raises :class:`RunValidationError` on an illegal transition; returns None when
    the transition is allowed.
    """
    if old is None or old == new:
        return
    if old not in TERMINAL_STATUSES:
        return  # active → anything valid is fine
    # `old` is terminal and `new` differs.
    if old != HALTED and new == HALTED:
        return  # escalation overlay onto a finished run is allowed
    who = f" for run {run_id!r}" if run_id else ""
    raise RunValidationError(
        f"illegal status transition{who}: {old!r} is terminal and cannot move to "
        f"{new!r} (terminal states are final; only an idempotent same-status "
        f"rewrite — or an escalation to 'halted' — is allowed)"
    )

# A run_id is the filename stem, so it doubles as the path-traversal guard.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class RunValidationError(ValueError):
    """A run record failed validation. Message is user-facing."""


# ── validation ────────────────────────────────────────────────────────────────

def validate_run(rec: object) -> dict:
    """Validate a run record. Returns a NORMALIZED copy (safe to persist).

    Raises RunValidationError with a clear, user-facing message on any problem
    so the API can surface it. The optional fields are normalized to their null
    defaults when absent so the on-disk shape is always complete.
    """
    if not isinstance(rec, dict):
        raise RunValidationError("run record must be a JSON object")

    for field in ("run_id", "agent_id", "status"):
        if field not in rec:
            raise RunValidationError(f"missing required field: {field!r}")

    run_id = rec["run_id"]
    if not isinstance(run_id, str) or not _ID_RE.match(run_id.strip()):
        raise RunValidationError(
            "run_id must be a slug matching [a-z0-9][a-z0-9_-]* "
            "(lowercase letters, digits, dash, underscore)"
        )
    run_id = run_id.strip()

    agent_id = rec["agent_id"]
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise RunValidationError("agent_id must be a non-empty string")

    status = rec["status"]
    if status not in VALID_STATUSES:
        raise RunValidationError(
            f"status must be one of {VALID_STATUSES}, got {status!r}"
        )

    return {
        "run_id": run_id,
        "agent_id": agent_id.strip(),
        "tag": _opt_str(rec.get("tag")),
        "command": _opt_str(rec.get("command")),
        "cwd": _opt_str(rec.get("cwd")),
        "session_id": _opt_str(rec.get("session_id")),
        # transcript_id: the Claude Code session UUID pinned at launch via
        # `claude --session-id <uuid>`. DISTINCT from session_id (the pty-manager
        # terminal handle used for liveness/kill) — Claude Code names its
        # transcript file after THIS id, so it is what the dashboard feed resolves
        # against. OPTIONAL + back-compat: absent on every record written before
        # the feed-cross-wiring fix, normalizing to None (the feed then has no
        # pinned id and shows an empty feed rather than an unrelated session's
        # transcript). See trident_agent_feed.resolve_transcript_path.
        "transcript_id": _opt_str(rec.get("transcript_id")),
        "status": status,
        "outcome": _opt_str(rec.get("outcome")),
        # tokens (#73): the session's consumed-token total, stamped by the
        # manager at run-complete. OPTIONAL + back-compat — absent on every
        # record written before #73, so it normalizes to None (unmeasured). A
        # non-negative int when present; a bad/negative/bool value is dropped to
        # None rather than raising, mirroring the lenient _opt_* normalizers.
        "tokens": _opt_count(rec.get("tokens")),
        # activity (#B land-feed): the structured stage log for a transcript-less
        # run (Eli/land). OPTIONAL + back-compat — absent on every record written
        # before this field, and on Claude-session runs (Gordon/Kleiner, whose
        # feed is the transcript), so it normalizes to []. Malformed entries are
        # dropped, never raised, so a hand-edited/partial record can't break the
        # run list (mirrors the lenient _opt_* normalizers).
        "activity": _opt_activity(rec.get("activity")),
        "created_at": _opt_num(rec.get("created_at")),
        "started_at": _opt_num(rec.get("started_at")),
        "finished_at": _opt_num(rec.get("finished_at")),
    }


def _opt_str(v: object) -> str | None:
    """Normalize an optional string field — None unless it is a real string."""
    return v if isinstance(v, str) else None


def _opt_num(v: object) -> float | None:
    """Normalize an optional numeric (timestamp) field. Bools are not numbers."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _opt_count(v: object) -> int | None:
    """Normalize an optional NON-NEGATIVE INT count (the #73 token total).

    Distinct from _opt_num: a token count is an int, not a float timestamp, and a
    negative/bool/garbage value is meaningless, so it drops to None (unmeasured)
    rather than persisting nonsense. A float that is a whole number (e.g. a JSON
    ``121000.0``) is accepted and floored to int; anything else → None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float):
        return int(v) if v >= 0 else None
    return None


def _opt_activity(v: object) -> list[dict]:
    """Normalize the optional structured stage-activity log (the land-run feed).

    A list of stage events; each event is kept ONLY when well-formed — a dict
    with a string ``stage`` and a ``status`` in :data:`VALID_ACTIVITY_STATUSES`.
    ``detail`` is coerced to a string ("" when missing/non-string) and ``ts`` to
    an optional float (via :func:`_opt_num`). ``log`` (a stage's FULL terminal
    output — the land feed's expandable command log) is OPTIONAL: kept only when a
    non-empty string and tail-capped at :data:`MAX_ACTIVITY_LOG_CHARS`; absent
    otherwise, so an entry without a log keeps its original 4-key shape. A malformed
    entry is DROPPED rather than raising, and a non-list value normalizes to ``[]``
    — so a hand-edited or partially-written record can never break the run list
    (mirrors the lenient ``_opt_*`` normalizers). The result is always a complete
    list of complete events, safe to persist and to hand straight to the feed
    assembler."""
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for ev in v:
        if not isinstance(ev, dict):
            continue
        stage = ev.get("stage")
        status = ev.get("status")
        if not isinstance(stage, str) or status not in VALID_ACTIVITY_STATUSES:
            continue
        detail = ev.get("detail")
        entry = {
            "stage": stage,
            "status": status,
            "detail": detail if isinstance(detail, str) else "",
            "ts": _opt_num(ev.get("ts")),
        }
        log = ev.get("log")
        if isinstance(log, str) and log:
            entry["log"] = log[-MAX_ACTIVITY_LOG_CHARS:]
        out.append(entry)
    return out


# ── store ─────────────────────────────────────────────────────────────────────

class AgentRunStore:
    """One JSON file per run under a gitignored runtime dir.

    All mutating methods validate first, so the store can never hold an invalid
    record. Reads tolerate a corrupt sibling file (skipped, logged) rather than
    failing the whole list — a hand-edited bad file shouldn't break the API.
    """

    def __init__(self, runs_dir: str | os.PathLike) -> None:
        self._dir = Path(runs_dir)

    # ── paths ─────────────────────────────────────────────────────────────

    def _path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.json"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── reads ─────────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        """All run records, newest first (by created_at). Corrupt files skipped."""
        if not self._dir.is_dir():
            return []
        out: list[dict] = []
        for path in self._dir.glob("*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as e:
                _LOG.warning("skipping unreadable run file %s: %s", path, e)
        # Newest first; runs without a created_at sort to the end (treated as 0).
        out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return out

    def get(self, run_id: str) -> dict | None:
        """One run record, or None if missing / unreadable / bad id."""
        if not isinstance(run_id, str) or not _ID_RE.match(run_id.strip()):
            return None
        path = self._path(run_id.strip())
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ── writes ────────────────────────────────────────────────────────────

    def put(self, rec: dict) -> dict:
        """Validate and persist a run record (create or overwrite).

        Returns the normalized record that was written. Raises
        RunValidationError (nothing is written) on an invalid record OR on an
        illegal status transition out of a terminal state (see
        :func:`guard_transition`) — a HALTED run's report can't be clobbered by a
        late cancel, and a finished run can't regress back to active.

        Durability: the record is written to a temp file, flushed AND fsync'd to
        disk, then atomically ``os.replace``d over the target, and the directory
        is fsync'd so the rename itself survives a crash. Without the fsync an OS
        crash / power loss between write and flush could leave a zero-length temp
        that replace then promotes to a truncated record (the L4 hardening).
        """
        normalized = validate_run(rec)

        # Transition guard (issue #117): a terminal record is final. Reject a
        # regression (e.g. HALTED → cancelled, succeeded → running); allow an
        # idempotent same-status rewrite and an escalation to HALTED (the
        # token-guardrail overlay). Reads the on-disk status; a fresh create
        # (no current file) is unguarded.
        current = self.get(normalized["run_id"])
        if current is not None:
            guard_transition(
                current.get("status"), normalized["status"], normalized["run_id"]
            )

        self._ensure_dir()
        path = self._path(normalized["run_id"])
        # Atomic + DURABLE write: temp → flush+fsync → replace, then fsync the dir.
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(normalized, indent=2, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self._fsync_dir()
        return normalized

    def _fsync_dir(self) -> None:
        """fsync the store directory so a completed ``os.replace`` rename is itself
        durable. Best-effort: directory fsync is unsupported on some platforms
        (notably Windows), where the file-level fsync above is the durability
        guarantee — so a failure here is swallowed, not raised."""
        try:
            dir_fd = os.open(str(self._dir), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass  # Windows / FS without directory-fsync support
        finally:
            os.close(dir_fd)

    def update(self, run_id: str, **changes) -> dict:
        """Read-modify-write a single run record. Returns the new record.

        Raises RunValidationError if the run does not exist (so a transition
        against a missing run is a hard error, not a silent create) or if the
        merged record is invalid.
        """
        current = self.get(run_id)
        if current is None:
            raise RunValidationError(f"run {run_id!r} does not exist")
        merged = {**current, **changes}
        return self.put(merged)

    def mark_halted(self, run_id: str, note: str | None = None,
                    finished_at: float | None = None) -> dict:
        """Mark a run HALTED (Slice F escape hatch). Returns the new record.

        A halt is a TERMINAL outcome distinct from ``failed``: the run hit
        something that needs a human decision rather than breaking. Mirrors the
        finish idiom — it is a thin wrapper over :meth:`update` that sets the
        terminal status plus the optional report/note (stored in ``outcome``,
        the same free-form final-detail field ``failed``/``cancelled`` use) and
        the finish timestamp. Raises :class:`RunValidationError` if the run does
        not exist (same as ``update``).

        ``note`` is the concise plain-English report string; it is left as
        ``None`` (not written) when absent, matching the optional-field idiom.
        """
        changes: dict = {"status": HALTED}
        if note is not None:
            changes["outcome"] = note
        if finished_at is not None:
            changes["finished_at"] = finished_at
        return self.update(run_id, **changes)

    def delete(self, run_id: str) -> bool:
        """Delete a run's file. True if it existed, False otherwise."""
        if not isinstance(run_id, str) or not _ID_RE.match(run_id.strip()):
            return False
        path = self._path(run_id.strip())
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def default_run_store(workspace: str | os.PathLike) -> AgentRunStore:
    """The run store rooted at ``<workspace>/agent_runs`` (gitignored runtime)."""
    return AgentRunStore(Path(workspace) / DEFAULT_DIRNAME)
