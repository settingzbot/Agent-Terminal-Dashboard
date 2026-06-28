"""
trident_agent_store.py — gitignored agent-definition store (issue #27, parent #24).

What this is
------------
The agent runner needs persisted agent *definitions* — the recipe for each
autonomous Claude Code agent (its name, its prompt, which bootstrap command runs
it, and what triggers it). This module is that store: one JSON file per agent
under a gitignored runtime dir, in the same idiom as trident_config.json
(machine-local runtime state, never committed). The agent-manager owns the live
processes; this store owns the recipes they run from.

Layout
------
One file per agent at ``<dir>/<id>.json``. The ``id`` is the filename stem, so
it must be a safe slug (``[a-z0-9_-]``) — that is also the path-traversal guard:
an id can never escape the store dir.

Agent definition schema
------------------------
::

    {
      "id":     "morning-report",        # slug — filename stem
      "name":   "Morning report",        # human label
      "description": "Reviews PRs ...",   # optional one-line blurb (#78);
                                         #   defaults "", shown in the agent tab
      "prompt": "Run the morning ...",   # instructions handed to the agent
      "model":  "claude",                # bootstrap command vocabulary:
                                         #   claude | claude-ds | claude-go | claude-ds-go
      "timeout_minutes": 30,             # optional — per-run claude backstop
                                         #   (positive int; defaults to 30)
      "trigger": { ... },                # TAGGED UNION on "kind" — see below
      "options": { ... },                # free-form extras (worktree, etc.)
      "enabled": true                    # optional operator pause switch (#39);
                                         #   defaults True, absence = ON
    }

``trigger`` is a tagged union on ``kind``:

- ``{"kind": "watch", "poll_interval": <int sec>, "condition": <str>,
  "stage": <"claim"|"review"|"land">}`` — poll every ``poll_interval`` seconds;
  fire when ``condition`` holds. ``stage`` (#78) selects WHICH pipeline the manager
  drives for this watch agent: ``claim`` (Gordon, issue→PR — the default when
  absent, so existing defs read back as claim), ``review`` (Kleiner, needs-review),
  or ``land`` (Eli, approved). Clock triggers have no ``stage``.
- ``{"kind": "clock", "schedule": <cron-ish str>}`` — fire on a schedule.

Validation raises :class:`AgentValidationError` with a clear message; the router
maps that to a 400 so the API rejects bad definitions with a readable reason.
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
# (see .gitignore: `agents/`). The agent-manager / dashboard pass the resolved
# workspace; tests pass a tmp_path.
DEFAULT_DIRNAME = "agents"

# Bootstrap-command vocabulary — the exact launcher names from scripts/claude*.ps1
# (claude=Anthropic, -ds=DeepSeek, -go=skip-permissions). Reused here so an agent
# definition's `model` is one of the same verbs Nathan types in a terminal.
VALID_MODELS = ("claude", "claude-ds", "claude-go", "claude-ds-go")

VALID_TRIGGER_KINDS = ("watch", "clock")

# The stage of a WATCH agent (#78) — which pipeline the manager drives for it:
#   claim  → Gordon  (issue→PR, needs an unclaimed ready-for-agent issue)
#   review → Kleiner (review a needs-review PR)
#   land   → Eli     (merge-and-land an approved PR onto main)
# Absent ⇒ "claim", so the existing single-stage issue-pr-worker.json reads back
# unchanged. Clock triggers never carry a stage.
VALID_WATCH_STAGES = ("claim", "review", "land")
DEFAULT_WATCH_STAGE = "claim"

# Per-agent backstop (minutes) for how long a single run's claude session may
# run before it is killed and the run failed. A healthy headless `claude -p`
# exits on its own well before this — it's a safety net for a stuck/runaway
# session, not a target. A def that omits the field defaults to this.
DEFAULT_TIMEOUT_MINUTES = 30

# An id is the filename stem, so it doubles as the path-traversal guard.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class AgentValidationError(ValueError):
    """An agent definition failed validation. Message is user-facing."""


# ── validation ────────────────────────────────────────────────────────────────

def validate_agent(defn: object) -> dict:
    """Validate an agent definition. Returns a NORMALIZED copy (safe to persist).

    Raises AgentValidationError with a clear, user-facing message on any problem
    so the API can surface it as a 400.
    """
    if not isinstance(defn, dict):
        raise AgentValidationError("agent definition must be a JSON object")

    # ── required scalar fields ────────────────────────────────────────────
    for field in ("id", "name", "prompt", "model", "trigger"):
        if field not in defn:
            raise AgentValidationError(f"missing required field: {field!r}")

    agent_id = defn["id"]
    if not isinstance(agent_id, str) or not _ID_RE.match(agent_id.strip()):
        raise AgentValidationError(
            "id must be a slug matching [a-z0-9][a-z0-9_-]* "
            "(lowercase letters, digits, dash, underscore)"
        )
    agent_id = agent_id.strip()

    name = defn["name"]
    if not isinstance(name, str) or not name.strip():
        raise AgentValidationError("name must be a non-empty string")

    # Optional — a one-line human blurb shown in the agent tab (#78). Defaults to
    # "" so a def that predates the field reads back valid (absence = no blurb).
    description = defn.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise AgentValidationError("description must be a string")

    prompt = defn["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentValidationError("prompt must be a non-empty string")

    model = defn["model"]
    if model not in VALID_MODELS:
        raise AgentValidationError(
            f"model must be one of {VALID_MODELS}, got {model!r}"
        )

    # Optional — defaults to DEFAULT_TIMEOUT_MINUTES so existing defs stay valid.
    timeout_minutes = defn.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
    if (not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool)
            or timeout_minutes <= 0):
        raise AgentValidationError(
            "timeout_minutes must be a positive integer (minutes)"
        )

    trigger = _validate_trigger(defn["trigger"])

    options = defn.get("options", {})
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise AgentValidationError("options must be a JSON object")

    # Optional — an operator pause switch (issue #39). Defaults to True so an
    # agent file that predates the field reads back as enabled (absence = ON).
    # This slice persists + surfaces the flag; it does NOT yet gate firing.
    enabled = defn.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AgentValidationError("enabled must be a boolean")

    return {
        "id": agent_id,
        "name": name.strip(),
        "description": description.strip(),
        "prompt": prompt,
        "model": model,
        "timeout_minutes": timeout_minutes,
        "trigger": trigger,
        "options": options,
        "enabled": enabled,
    }


def _validate_trigger(trigger: object) -> dict:
    """Validate the tagged-union trigger. Returns a normalized copy."""
    if not isinstance(trigger, dict):
        raise AgentValidationError("trigger must be a JSON object")

    kind = trigger.get("kind")
    if kind not in VALID_TRIGGER_KINDS:
        raise AgentValidationError(
            f"trigger.kind must be one of {VALID_TRIGGER_KINDS}, got {kind!r}"
        )

    if kind == "watch":
        interval = trigger.get("poll_interval")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
            raise AgentValidationError(
                "watch trigger requires poll_interval as a positive integer (seconds)"
            )
        condition = trigger.get("condition", "")
        if not isinstance(condition, str):
            raise AgentValidationError("watch trigger condition must be a string")
        # Optional stage (#78) — which pipeline the manager drives for this watch
        # agent. Absent ⇒ "claim" (Gordon), so the existing issue-pr-worker.json
        # with no stage reads back unchanged.
        stage = trigger.get("stage", DEFAULT_WATCH_STAGE)
        if stage not in VALID_WATCH_STAGES:
            raise AgentValidationError(
                f"watch trigger stage must be one of {VALID_WATCH_STAGES}, got {stage!r}"
            )
        return {
            "kind": "watch", "poll_interval": interval,
            "condition": condition, "stage": stage,
        }

    # kind == "clock"
    schedule = trigger.get("schedule")
    if not isinstance(schedule, str) or not schedule.strip():
        raise AgentValidationError(
            "clock trigger requires schedule as a non-empty string (cron-ish)"
        )
    return {"kind": "clock", "schedule": schedule.strip()}


# ── store ─────────────────────────────────────────────────────────────────────

class AgentStore:
    """One JSON file per agent under a gitignored runtime dir.

    All mutating methods validate first, so the store can never hold an invalid
    definition. Reads tolerate a corrupt sibling file (skipped, logged) rather
    than failing the whole list — a hand-edited bad file shouldn't break the API.
    """

    def __init__(self, agents_dir: str | os.PathLike) -> None:
        self._dir = Path(agents_dir)

    # ── paths ─────────────────────────────────────────────────────────────

    def _path(self, agent_id: str) -> Path:
        # agent_id is validated to a safe slug before it ever reaches here on
        # the write path; on the read path callers pass an id we re-guard.
        return self._dir / f"{agent_id}.json"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── reads ─────────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        """All agent definitions. Corrupt files are skipped (logged).

        Non-agent JSON files (e.g. ``provider.json``) are silently skipped —
        only files that pass :func:`validate_agent` are returned. A sibling
        config file that happens to be valid JSON but isn't an agent definition
        must never reach the dashboard (it would have no ``trigger`` → crash
        on ``a.trigger.kind``).
        """
        if not self._dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                normalized = validate_agent(raw)
                out.append(normalized)
            except AgentValidationError:
                # Not an agent definition — skip silently. This covers
                # provider.json and any future non-agent config files that
                # share the agents/ directory.
                _LOG.debug("skipping non-agent JSON file %s", path)
            except (OSError, json.JSONDecodeError) as e:
                _LOG.warning("skipping unreadable agent file %s: %s", path, e)
        return out

    def get(self, agent_id: str) -> dict | None:
        """One agent definition, or None if missing / unreadable / bad id."""
        if not isinstance(agent_id, str) or not _ID_RE.match(agent_id.strip()):
            return None
        path = self._path(agent_id.strip())
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ── writes ────────────────────────────────────────────────────────────

    def upsert(self, defn: dict) -> dict:
        """Validate and persist an agent definition (create or update).

        Returns the normalized definition that was written. Raises
        AgentValidationError (nothing is written) on an invalid definition.
        """
        normalized = validate_agent(defn)
        self._ensure_dir()
        path = self._path(normalized["id"])
        # Atomic-ish write: temp + replace so a crash mid-write can't truncate
        # an existing definition.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(normalized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return normalized

    def set_enabled(self, agent_id: str, enabled: bool) -> dict | None:
        """Flip an agent's ``enabled`` flag and persist it (issue #39).

        A cheap one-field write that avoids a full PUT-upsert round-trip just to
        toggle a boolean. Reads the existing def, sets ``enabled``, and re-writes
        through :meth:`upsert` (which re-validates + normalizes — so a legacy
        file with no ``enabled`` key gets the field on the way back out).

        Returns the updated normalized definition, or ``None`` if the agent does
        not exist (so the API can map that to a 404).
        """
        defn = self.get(agent_id)
        if defn is None:
            return None
        defn = dict(defn)
        defn["enabled"] = bool(enabled)
        return self.upsert(defn)

    def delete(self, agent_id: str) -> bool:
        """Delete an agent's file. True if it existed, False otherwise."""
        if not isinstance(agent_id, str) or not _ID_RE.match(agent_id.strip()):
            return False
        path = self._path(agent_id.strip())
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def default_store(workspace: str | os.PathLike) -> AgentStore:
    """The store rooted at ``<workspace>/agents`` (gitignored runtime dir)."""
    return AgentStore(Path(workspace) / DEFAULT_DIRNAME)
