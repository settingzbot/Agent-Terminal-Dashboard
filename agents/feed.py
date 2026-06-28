"""
trident_agent_feed.py — structured activity feed from a Claude Code transcript
(issue #32, parent #24).

What this is
------------
An agent *run* is a Claude Code session driven in a tagged pty-manager terminal.
Claude Code writes a full transcript of that session to disk as JSONL, one record
per line, under ``~/.claude/projects/<project-slug>/<session-id>.jsonl``. This
module turns that on-disk transcript into a STRUCTURED FEED the dashboard can
render: an ordered list of events (the human prompt, assistant text, each
tool-use and its result) plus a per-run token/cost summary aggregated from the
transcript's usage records.

Two responsibilities, deliberately separate and each independently testable:

1. **Resolve** a run's transcript path (:func:`resolve_transcript_path`):
   when a transcript id is pinned (the run record's ``transcript_id``, set at
   launch via ``claude --session-id``), resolve STRICTLY to that file — a miss
   returns ``None``, never the newest unrelated transcript. Only when NO id is
   pinned (a legacy record) does it fall back to the *newest* ``*.jsonl`` in the
   project dir. The ``~/.claude/projects`` base is *injectable* so tests use a
   tmp fixture dir, never the real home dir.

2. **Parse** the transcript into a feed (:func:`assemble_feed`): tolerant of a
   live, mid-write transcript — a half-written final line, blank lines, or a
   stray non-JSON line never crash the parse; the bad line is skipped and the
   rest of the feed is returned.

The dashboard route (``GET /api/agents/runs/{run_id}/feed``) reads the run record
for its ``cwd`` + ``transcript_id``, then calls :func:`assemble_run_feed` to do
both steps. Reading the transcript directly off local disk is correct and intended —
it does NOT route through the agent-manager RPC.

Feed shape
----------
::

    {
      "transcript": "C:/.../<session>.jsonl" | None,   # resolved path (None if absent)
      "events": [                                       # ordered, oldest-first
        {"kind": "prompt",      "text": ...,  "ts": ...},
        {"kind": "text",        "text": ...,  "ts": ...},
        {"kind": "tool_use",    "name": ..., "tool_use_id": ..., "input": {...}, "ts": ...},
        {"kind": "tool_result", "tool_use_id": ..., "text": ..., "is_error": bool, "ts": ...},
      ],
      "usage": {
        "input_tokens": int, "output_tokens": int,
        "cache_creation_input_tokens": int, "cache_read_input_tokens": int,
        "message_count": int,          # assistant turns that carried usage
        "model": str | None,           # last assistant model seen
        "cost_usd": float,             # estimate from per-Mtoken rates below
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

_LOG = logging.getLogger(__name__)

# ── transcript base ─────────────────────────────────────────────────────────────

# Claude Code writes session transcripts under this dir, one subdir per project.
# Resolved lazily so a test can inject a tmp dir without importing os.environ at
# module load. Callers (the route) pass an explicit base; tests always do.
def default_transcript_base() -> Path:
    """The real ``~/.claude/projects`` base. Injectable everywhere else."""
    return Path.home() / ".claude" / "projects"


# ── project slug ────────────────────────────────────────────────────────────────

# Claude Code slugifies a project cwd into the subdir name by replacing EVERY
# non-alphanumeric character with a dash (path separators, the drive colon, AND
# underscores/dots). ``C:\Users\setti\Trident`` → ``C--Users-setti-Trident``
# (colon→dash + first backslash→dash gives the double dash); a worktree like
# ``...\agent_worktrees\agent-issue-38-...branch_slug`` →
# ``...-agent-worktrees-agent-issue-38-...branch-slug`` (the underscores become
# dashes too). An earlier ``[\\/:]`` mirror missed underscores, so a worktree
# cwd never resolved its transcript dir ([[footguns#171]], 2026-06-21). We mirror
# the full rule so a run's cwd maps to its transcript dir.
_SLUG_SEP_RE = re.compile(r"[^A-Za-z0-9]")


def project_slug(cwd: str | os.PathLike) -> str:
    """Claude Code's project-dir slug for a working directory."""
    return _SLUG_SEP_RE.sub("-", str(cwd))


def project_dir_for_cwd(base: str | os.PathLike, cwd: str | os.PathLike) -> Path:
    """The ``<base>/<slug>`` transcript dir for a run's working directory."""
    return Path(base) / project_slug(cwd)


# ── resolver ────────────────────────────────────────────────────────────────────

def resolve_transcript_path(
    project_dir: str | os.PathLike,
    session_id: str | None = None,
) -> Path | None:
    """Resolve a run's transcript JSONL path inside ``project_dir``.

    Two regimes, deliberately different:

    * **A transcript id IS pinned** (``session_id`` truthy — in practice the run
      record's ``transcript_id``, the uuid pinned at launch via
      ``claude --session-id``): resolve STRICTLY to ``<id>.jsonl``. If that file
      does not exist yet (a just-launched run whose transcript isn't flushed) or
      ever, return ``None`` — DO NOT fall back to the newest file. The newest
      file in a shared project dir is whatever session wrote last, which for the
      operator's own interactive Claude tab is *that* tab's transcript. Falling
      back there is exactly the cross-wiring bug this strictness fixes: a run's
      feed must show its own transcript or nothing, never a stranger's.

    * **No id pinned** (``session_id`` falsy — a legacy/pre-fix run whose record
      carries no ``transcript_id``): fall back to the newest ``*.jsonl`` by
      mtime. This is only safe in a dir that holds a single run's session (e.g. a
      per-run worktree), which is where the legacy id-less runs lived.

    Returns ``None`` when the dir is missing or holds no usable transcript.
    """
    pdir = Path(project_dir)
    if session_id:
        # A pinned id; guard against any path trickery by only honoring it when
        # the resolved file stays inside project_dir. A miss returns None (NO
        # newest-file fallback) — see the strictness rationale above.
        candidate = pdir / f"{session_id}.jsonl"
        try:
            if candidate.is_file() and candidate.parent == pdir:
                return candidate
        except OSError:
            pass
        return None
    # No id pinned (legacy): newest *.jsonl by modification time.
    if not pdir.is_dir():
        return None
    newest: Path | None = None
    newest_mtime = -1.0
    try:
        for p in pdir.glob("*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest_mtime:
                newest_mtime, newest = m, p
    except OSError:
        return None
    return newest


# ── parser ──────────────────────────────────────────────────────────────────────

# Per-million-token USD rates, used only for a rough per-run cost ESTIMATE in the
# feed summary. These are display-only convenience numbers (Opus-class default);
# the authoritative spend lives in the provider's billing, not here. Kept as a
# single dict so the rate is obvious and easy to revise.
_RATE_PER_MTOK = {
    "input": 5.0,
    "output": 25.0,
    "cache_creation": 6.25,   # 5m-write surcharge ballpark
    "cache_read": 0.5,
}


def _new_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "message_count": 0,
        "model": None,
        "cost_usd": 0.0,
    }


def _estimate_cost(u: dict) -> float:
    return round(
        u["input_tokens"] / 1e6 * _RATE_PER_MTOK["input"]
        + u["output_tokens"] / 1e6 * _RATE_PER_MTOK["output"]
        + u["cache_creation_input_tokens"] / 1e6 * _RATE_PER_MTOK["cache_creation"]
        + u["cache_read_input_tokens"] / 1e6 * _RATE_PER_MTOK["cache_read"],
        6,
    )


def _iter_records(path: Path):
    """Yield parsed JSON objects from a JSONL file, skipping any unparseable line.

    Tolerant by design: a live transcript is mid-write, so the final line may be
    half-flushed; blank lines and the odd corrupt line must not abort the parse.
    A missing file yields nothing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated/garbage line — skip, keep going
        if isinstance(obj, dict):
            yield obj


def _result_text(content: object) -> str:
    """Flatten a tool_result's content (str or list of blocks) to display text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if isinstance(blk.get("text"), str):
                    parts.append(blk["text"])
                elif blk.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return ""


def assemble_feed(transcript_path: str | os.PathLike) -> dict:
    """Parse a transcript JSONL into ``{transcript, events, usage}``.

    Never raises on a partial/truncated/corrupt file; a missing file yields an
    empty-but-valid feed. ``transcript`` echoes the resolved path (or ``None`` if
    the file does not exist).
    """
    path = Path(transcript_path)
    events: list[dict] = []
    usage = _new_usage()
    # Usage is folded ONCE per assistant message id. Claude Code writes one
    # transcript record PER content block (thinking, text, each tool_use) and
    # repeats the SAME ``message.usage`` object + ``message.id`` on every one, so a
    # turn split into N blocks would be summed N times (verified ~2.7–3.1x
    # overcount against live transcripts). Track which message ids have already
    # contributed their usage and skip the repeats (values are identical per id).
    seen_usage_ids: set[str] = set()

    exists = path.is_file()

    for rec in _iter_records(path):
        rtype = rec.get("type")
        msg = rec.get("message")
        ts = rec.get("timestamp")

        if rtype == "user":
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                # A typed human prompt.
                events.append({"kind": "prompt", "text": content, "ts": ts})
            elif isinstance(content, list):
                # Tool results are delivered back as user-role blocks.
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        events.append({
                            "kind": "tool_result",
                            "tool_use_id": blk.get("tool_use_id"),
                            "text": _result_text(blk.get("content")),
                            "is_error": bool(blk.get("is_error", False)),
                            "ts": ts,
                        })

        elif rtype == "assistant" and isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text" and isinstance(blk.get("text"), str):
                        if blk["text"].strip():
                            events.append({"kind": "text", "text": blk["text"], "ts": ts})
                    elif btype == "tool_use":
                        events.append({
                            "kind": "tool_use",
                            "name": blk.get("name"),
                            "tool_use_id": blk.get("id"),
                            "input": blk.get("input") if isinstance(blk.get("input"), dict) else {},
                            "ts": ts,
                        })
                    # "thinking" blocks are intentionally not surfaced in the feed.
            # Aggregate token usage from this assistant turn — ONCE per message
            # id. A record whose message id has already contributed is a
            # per-content-block repeat (same usage object); fold it only the first
            # time the id is seen. A record with no id (legacy single-block shape)
            # can't be deduped, so it still counts — there's no repeat to fold.
            u = msg.get("usage")
            if isinstance(u, dict):
                msg_id = msg.get("id")
                already_counted = isinstance(msg_id, str) and msg_id in seen_usage_ids
                if not already_counted:
                    if isinstance(msg_id, str):
                        seen_usage_ids.add(msg_id)
                    usage["input_tokens"] += _int(u.get("input_tokens"))
                    usage["output_tokens"] += _int(u.get("output_tokens"))
                    usage["cache_creation_input_tokens"] += _int(u.get("cache_creation_input_tokens"))
                    usage["cache_read_input_tokens"] += _int(u.get("cache_read_input_tokens"))
                    usage["message_count"] += 1
                    model = msg.get("model")
                    if isinstance(model, str):
                        usage["model"] = model

    usage["cost_usd"] = _estimate_cost(usage)
    return {
        "transcript": str(path) if exists else None,
        "events": events,
        "usage": usage,
    }


def _int(v: object) -> int:
    """Coerce a usage field to a non-negative int; bad/missing → 0."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return 0


def assemble_run_feed(
    project_dir: str | os.PathLike,
    session_id: str | None = None,
) -> dict:
    """Resolve a run's transcript inside ``project_dir`` then assemble its feed.

    Convenience that pairs :func:`resolve_transcript_path` with
    :func:`assemble_feed`. When no transcript exists yet (just-launched run),
    returns an empty-but-valid feed with ``transcript: None``.
    """
    path = resolve_transcript_path(project_dir, session_id)
    if path is None:
        return {"transcript": None, "events": [], "usage": _new_usage()}
    return assemble_feed(path)


def _stage_events_from_activity(activity: list) -> list[dict]:
    """Render a run's structured stage log (the land-run feed) as ordered feed
    events. Each well-formed entry becomes a ``kind="stage"`` event; a malformed
    entry is skipped (the run store already normalizes, but the feed stays
    tolerant of a hand-written record). Oldest-first, preserving list order."""
    events: list[dict] = []
    for ev in activity:
        if not isinstance(ev, dict):
            continue
        stage = ev.get("stage")
        status = ev.get("status")
        if not isinstance(stage, str) or not isinstance(status, str):
            continue
        detail = ev.get("detail")
        event = {
            "kind": "stage",
            "stage": stage,
            "status": status,
            "detail": detail if isinstance(detail, str) else "",
            "ts": ev.get("ts"),
        }
        # The stage's FULL command output (git/npm/pytest), when recorded — the
        # feed renders it as an expandable block. OPTIONAL: omitted when absent so
        # an old record (or a stage with no captured output) stays log-free.
        log = ev.get("log")
        if isinstance(log, str) and log:
            event["log"] = log
        events.append(event)
    return events


def assemble_feed_for_run(
    run: dict,
    transcript_base: str | os.PathLike,
    shared_cwd: str | os.PathLike | None = None,
) -> dict:
    """Assemble the feed for a run RECORD against a transcript base dir.

    Three regimes, checked in order:

    1. **A transcript-less run carries a structured ``activity`` log** (Eli/land —
       pure git/npm/pytest, never a Claude session). Render THAT log as the feed
       (``kind="stage"`` events) and NEVER touch a transcript dir: the run has no
       session of its own to find, so there is nothing to resolve and nothing to
       cross-wire onto. This is the primary land-feed fix.

    2. **A Claude-session run** — derive the project dir from the run's ``cwd``
       (Claude Code's slug) and pin the run's ``transcript_id`` (the Claude
       session UUID set at launch via ``claude --session-id``, which is what
       Claude names its transcript file after; falls back to the legacy
       ``session_id`` field for pre-fix records).

    3. **Isolation guard** — when NO transcript id is pinned AND ``shared_cwd`` is
       given AND the run's ``cwd`` resolves to it (the operator's main workspace,
       which a land/review/fixer run shares with the live human Claude tab), the
       newest-``*.jsonl`` fallback would grab whatever session wrote LAST — the
       operator's own open tab — not this run's. Refuse the fallback (empty feed)
       rather than show a stranger's transcript. This mirrors the same guard the
       token counter already applies (``trident_agent_manager._count_session_tokens``)
       and also covers the brief window before a watch/review run pins its id. A
       per-run worktree cwd is a UNIQUE dir, so its fallback stays correct and is
       left untouched.

    ``transcript_base`` is ``~/.claude/projects`` in production, a tmp dir in
    tests. ``shared_cwd`` is the operator's workspace root (the dashboard route
    passes it); ``None`` disables the guard (the legacy behavior).
    """
    # 1) A structured stage log ⇒ that IS the feed (no transcript to resolve).
    activity = run.get("activity")
    if isinstance(activity, list) and activity:
        return {
            "transcript": None,
            "events": _stage_events_from_activity(activity),
            "usage": _new_usage(),
        }

    cwd = run.get("cwd") or ""
    transcript_id = run.get("transcript_id") or run.get("session_id")

    # 3) Isolation guard — refuse the newest-file fallback in the shared workspace.
    if not transcript_id and shared_cwd is not None:
        try:
            same_dir = Path(cwd).resolve() == Path(shared_cwd).resolve()
        except (OSError, ValueError):
            same_dir = False
        if same_dir:
            return {"transcript": None, "events": [], "usage": _new_usage()}

    # 2) Resolve + parse this run's own transcript.
    pdir = project_dir_for_cwd(transcript_base, cwd)
    return assemble_run_feed(pdir, session_id=transcript_id)
