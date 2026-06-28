"""trident_agent_tokens.py — full-chain token accounting (issue #73, parent #58).

What this is
------------
Tokens consumed against the Claude subscription, measured PER AGENT SESSION, with
a per-session guardrail that FLAGS the runaway-agent failure mode (e.g. a
resumed agent re-ingesting its own transcript) plus per-issue and per-PRD
rollups for visibility ("what did PRD #58 cost").

Scope of the guardrail — POST-HOC, not a budget cap (#121, L7)
--------------------------------------------------------------
:func:`classify_session` / :func:`is_over_ceiling` are PURE labels applied by the
manager at run-complete, AFTER the session is already terminal. They detect and
report a runaway session; they do NOT and CANNOT stop one mid-flight. A session
that spirals can burn far more than the 300k ceiling before it ends — the ceiling
labels the corpse, it doesn't pull the trigger. The only mid-flight bound is the
per-run wall-clock deadline in the manager (#119), which caps wall time, not
tokens. A real mid-run token-kill is out of scope here; this module is the
measurement + classification core, not an interrupt.

The unit is a single agent session — each loop agent (Gordon, Kleiner, Eli, a
fixer) is one session = one run record. The figure is summed by the MANAGER at
run-complete from that session's transcript JSONL (NOT the agent self-reporting:
a headless session can't reliably read its own running total, and Eli can't
measure its own still-running session). This is the same total the dashboard's
per-agent feed already reports (e.g. Gordon's #64 session ≈ 121k).

Two deliberately-separate halves, each independently testable:

1. **I/O seam** (:func:`sum_session_tokens`) — sum the per-turn ``usage`` across a
   session's transcript JSONL into one int. Reuses the EXACT field shapes the
   dashboard feed (``trident_agent_feed.assemble_feed``) already reads, and is
   equally tolerant of a live, mid-write transcript (a half-written final line, a
   blank line, or a stray non-JSON line is skipped, not fatal). This is the only
   function here that touches disk.

2. **PURE cores** (no I/O, table-tested) — the guardrail + rollups as data:
   :func:`classify_session` (OK/WARN/OVER against the 200k/300k thresholds),
   :func:`rollup_issue` (sum a chain's per-session totals), :func:`rollup_prd`
   (sum a PRD's child-issue totals), and the formatters
   :func:`format_issue_breakdown` / :func:`format_prd_rollup` that render the
   close-comment lines. Mirrors the pure-core discipline of
   ``trident_agent_scheduler`` / ``trident_eli_architect``.

Which ``usage`` field is summed, and why (revised 2026-06-23)
------------------------------------------------------------
A session's guardrail figure is the sum, ACROSS EVERY assistant turn, of just:

    output_tokens

i.e. only the tokens the agent actually GENERATED. The original design summed all
four fields (input + output + cache-creation + cache-read), reasoning that a
runaway re-ingesting its own transcript shows up as ballooning cache tokens. In
practice that metric is unusable as a per-session ceiling: prompt caching re-bills
the FULL context prefix as ``cache_read_input_tokens`` on EVERY turn, so a
perfectly healthy multi-turn agent N-counts the same cached bytes once per turn.
Measured on real loop sessions, the all-four sum was 0.5M–36M for *healthy* runs
(Gordon's own #81 build: 495,361 all-four but only 4,098 output) and 330M for a
long interactive session — making any fixed ceiling either trip constantly (the
#81 loop false-halted three times) or be so high it catches nothing.

``output_tokens`` is the only field immune to that N-counting: each output token is
generated exactly once, never cached, never re-read. It is a stable, interpretable
proxy for "how much work did this agent actually do" and the true signal of the
runaway-loop failure mode (an agent spinning forever emits unbounded output). Real
healthy loop agents generate well under 50k output; the 300k ceiling is ~6x that.
Cache reads/writes are still real subscription cost, but they belong in a
cost-display surface, not a halt threshold. The USD estimate stays out of here —
the guardrail is denominated in output tokens, not dollars.
"""

from __future__ import annotations

import os

from agents.feed import _int, _iter_records

# ── guardrail thresholds (per session) ──────────────────────────────────────────

# 🎯 Target: a healthy session stays at or under this. Below ⇒ "ok".
SESSION_TARGET = 200_000
# 🟡 Ceiling: accepted up to here. (200k, 300k] ⇒ "warn" (logged, not filed).
# Strictly OVER this ⇒ "over": raise the 🔴 halt + auto-file a needs-human report.
SESSION_CEILING = 300_000

# The three classification buckets (string constants so call-sites read as data).
OK = "ok"
WARN = "warn"
OVER = "over"


# ═══════════════════════════════════════════════════════════════════════════════
# I/O seam — sum a session's consumed tokens from its transcript JSONL
# ═══════════════════════════════════════════════════════════════════════════════

def sum_session_tokens(transcript_path: str | os.PathLike) -> int:
    """Sum a session's GENERATED tokens (``output_tokens``) across its transcript
    JSONL. The ONLY function here that touches disk.

    Walks every assistant turn's ``usage`` record and sums ``output_tokens`` only
    — deliberately NOT the cached input fields. ``cache_read_input_tokens`` re-bills
    the full context prefix every turn, so summing it (or the all-four total)
    N-counts the same bytes once per turn and balloons a healthy multi-turn session
    into the tens of millions (see the module docstring). Output is generated once
    and never re-read, so it is the stable runaway signal the guardrail needs.

    Tolerant of a live, mid-write transcript exactly like ``assemble_feed``: the
    shared ``_iter_records`` skips a half-written final line / blank line / stray
    non-JSON line, so a session that is still flushing never crashes the sum (it
    just omits the unparseable tail). A missing file yields 0.

    Returns the non-negative integer total.
    """
    total = 0
    for rec in _iter_records(_as_path(transcript_path)):
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        total += _int(usage.get("output_tokens"))
    return total


def _as_path(p: str | os.PathLike):
    """Coerce to a ``Path`` so ``_iter_records`` (which calls ``.read_text``) is
    happy with either a str or a PathLike. Kept tiny + local — the feed module's
    ``_iter_records`` already takes a Path."""
    from pathlib import Path
    return Path(p)


# ═══════════════════════════════════════════════════════════════════════════════
# PURE cores — guardrail classification + rollups (no I/O; table-tested as data)
# ═══════════════════════════════════════════════════════════════════════════════

def classify_session(tokens: int) -> str:
    """Bucket a session's consumed-token total against the guardrail. PURE.

    Returns one of :data:`OK` / :data:`WARN` / :data:`OVER`:

      * ``tokens <= SESSION_TARGET``   → ``"ok"``   (🎯 healthy)
      * ``SESSION_TARGET < tokens <= SESSION_CEILING`` → ``"warn"`` (🟡 logged)
      * ``tokens > SESSION_CEILING``   → ``"over"`` (🔴 halt + file a report)

    The boundaries are INCLUSIVE on the lower bucket: exactly ``SESSION_TARGET``
    (200k) is "ok" and exactly ``SESSION_CEILING`` (300k) is "warn"; only a
    strictly-greater total is "over". This is the actionable half of the issue —
    everything downstream (the halt) branches on an ``"over"`` here.

    POST-HOC: "over" labels an already-finished session (#121, L7). The downstream
    "halt" files a report; it does not interrupt the run, which has already spent
    the tokens by the time this is evaluated."""
    if tokens <= SESSION_TARGET:
        return OK
    if tokens <= SESSION_CEILING:
        return WARN
    return OVER


def is_over_ceiling(tokens: int) -> bool:
    """True iff ``tokens`` is strictly OVER the ceiling (i.e. classifies "over").
    PURE — the one predicate the manager's halt path branches on, kept named so
    the wiring reads as intent rather than a magic ``> SESSION_CEILING``."""
    return classify_session(tokens) == OVER


def rollup_issue(session_tokens: list[int]) -> int:
    """Sum an issue's per-session consumed-token totals into one chain total. PURE.

    A trivial sum kept as a named pure core so the per-issue rollup logic is
    testable as data (not buried in a comment-builder). Bad/None entries are
    coerced to 0 via ``_int`` so a run record missing its (optional, back-compat)
    ``tokens`` field never poisons the chain total — an unmeasured stage simply
    contributes 0. Empty list ⇒ 0."""
    return sum(_int(t) for t in (session_tokens or []))


def rollup_prd(issue_totals: list[int]) -> int:
    """Sum a PRD's child-issue chain totals into one PRD total. PURE.

    Same trivial-but-named-pure discipline as :func:`rollup_issue`, one level up:
    each entry is an issue's chain total (itself a ``rollup_issue``). Bad/None
    entries coerce to 0; empty list ⇒ 0."""
    return sum(_int(t) for t in (issue_totals or []))


def humanize_tokens(tokens: int) -> str:
    """Render a token count compactly for a close comment. PURE.

    ``121_000 → "121k"``, ``163_500 → "164k"`` (rounded to the nearest thousand),
    ``800 → "800"`` (sub-1k stays exact so a tiny run isn't shown as "0k"). The
    close comments read in thousands the way the issue text does ("Gordon 121k …
    = 163k")."""
    t = _int(tokens)
    if t < 1000:
        return str(t)
    return f"{round(t / 1000)}k"


def format_issue_breakdown(
    parts: list[tuple[str, int]], *, total: int | None = None
) -> str:
    """Render the per-issue session breakdown line for Eli's close comment. PURE.

    ``parts`` is an ordered list of ``(agent_label, tokens)`` per session in the
    chain (e.g. ``[("Gordon", 121_000), ("Kleiner", 18_000), ("Eli", 24_000)]``)
    →  ``"Gordon 121k + Kleiner 18k + Eli 24k = 163k"`` — the exact shape the
    issue spec asks for. ``total`` defaults to the rollup of ``parts``' token
    values, but is INJECTABLE so the manager can pass the authoritative
    run-store sum (which may include a stage not present in ``parts``). An empty
    chain renders ``"no measured sessions"`` so the comment never reads "= 0k"
    with nothing on the left."""
    if not parts:
        return "no measured sessions"
    chain_total = rollup_issue([t for _, t in parts]) if total is None else _int(total)
    body = " + ".join(f"{label} {humanize_tokens(t)}" for label, t in parts)
    return f"{body} = {humanize_tokens(chain_total)}"


def format_prd_rollup(prd_number: int, issue_totals: list[int]) -> str:
    """Render the per-PRD rollup line for the parent's close comment. PURE.

    ``[121_000, 90_000]`` for PRD #58 →
    ``"PRD #58: ~211k across 2 slices"`` — the shape the issue spec asks for.
    "slices" counts the entries (the child issues that contributed a total),
    singularised for one. An empty list reads ``"PRD #58: no measured slices"``."""
    n = len(issue_totals or [])
    if n == 0:
        return f"PRD #{prd_number}: no measured slices"
    total = rollup_prd(issue_totals)
    slices = "slice" if n == 1 else "slices"
    return f"PRD #{prd_number}: ~{humanize_tokens(total)} across {n} {slices}"
