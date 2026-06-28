"""
trident_agent_scheduler.py — the scheduler/queue DECISION CORE (issue #33,
parent #24).

What this is
------------
The deep, table-driven-tested heart of the agent runner: a PURE function that,
given the set of agents that are *ready to launch* (clock-due or manually
queued), the live in-flight set, and the concurrency cap, decides WHICH agents
to launch on this tick and in WHAT order.

It does NO I/O and reads NO clock. The clock/now, the in-flight set, the
blocked state, and the readiness of each candidate are all passed IN by the
caller (the scheduler loop in trident_agent_manager.py). That is what makes it
exhaustively unit-testable as a data table — the four invariants below are each
one row in tests/test_agent_scheduler.py.

The four invariants it enforces
-------------------------------
1. **Concurrency cap.** At most ``cap`` runs are unblocked/in-flight at once
   (default 2). The in-flight set counts against the budget, so the number of
   NEW launches is ``cap - len(in_flight)`` (never negative).
2. **Oldest-first ordering.** Candidates launch in ascending ``ready_since``
   order (the moment each became ready). Ties break on ``agent_id`` so the
   decision is deterministic. Overflow beyond the cap is simply dropped this
   tick — it re-appears as a candidate next tick and drains in order.
3. **Skip-blocked.** A candidate flagged ``blocked`` (e.g. an issue→PR agent
   whose "Blocked by #N" is unsatisfied — wired in slice #35) is skipped and
   does NOT consume a slot.
4. **In-flight exclusion.** An agent already running or queued (its id is in
   ``in_flight``) is never launched again — no double-launch of one agent, and
   the duplicate is removed even if it appears twice in the candidate list.

Why a separate pure module
--------------------------
The decision is the part most worth testing and the part most likely to grow
(priorities, fairness, per-agent rate limits). Keeping it free of the manager's
TCP/asyncio/store machinery means the tests are plain dict/list tables with no
mocks, and the loop in trident_agent_manager.py stays a thin driver that feeds
this function its inputs and acts on its output.

The clock seam (``cron_is_due``) lives here too because it is likewise pure
(now/schedule in, bool out). The ``watch`` trigger — polling GitHub — is NOT
here; it arrives in slice #35 with the gateway. The seam for it is clean: a
watch agent simply becomes a ready ``Candidate`` once its condition holds, fed
to the same ``decide_launches`` core.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

# The global concurrency cap: at most this many agent runs unblocked/in-flight
# at once. Configurable per call; this is the default the loop uses.
DEFAULT_CONCURRENCY_CAP = 2

# The cross-PR cap (#53): at most this many OPEN, unmerged agent-authored PRs may
# exist before candidate-gathering withholds NEW watch runs. Default 2 — Gordon
# can pick up two unblocked issues at once, so two open agent PRs are allowed.
# Each run branches from a main that already includes the prior merge (PRs are
# sequential per-issue, not siblings off the same base).
DEFAULT_AGENT_PR_CAP = 2

# The claim-loop in-flight cap: at most this many issues may be in flight in the
# Gordon→Kleiner→Eli loop at once. Default 2 — Gordon can claim a second issue
# while one is already in the pipeline. The strict-serial invariant (#64) is
# relaxed from 1 to 2, but the loop is still bounded.
DEFAULT_LOOP_IN_FLIGHT_CAP = 2


def should_withhold_watch(open_agent_pr_count: int, *, cap: int = DEFAULT_AGENT_PR_CAP) -> bool:
    """PURE. True if candidate-gathering should withhold ALL new watch runs this
    tick because open agent-authored PRs are at/over the cap (#53).

    A ``cap`` <= 0 is treated as "no limit" (never withhold) — the explicit way to
    disable the gate. The normal cap is 1: hold the next run until the prior PR
    merges or closes, so each run branches from a main that includes it.

    Belt-and-suspenders under the #64 strict-serial loop: ``should_withhold_loop``
    is the primary gate (one issue ANYWHERE in the pipeline, PR or not). This
    open-PR cap is kept as a second, narrower check so #53's exact invariant — no
    new run while a prior agent PR is still OPEN — survives independently.
    """
    if cap <= 0:
        return False
    return open_agent_pr_count >= cap


def should_withhold_loop(
    in_flight_count: int, *, cap: int = DEFAULT_LOOP_IN_FLIGHT_CAP
) -> bool:
    """PURE. True if candidate-gathering should withhold ALL new watch runs this
    tick because the in-flight count in the issue→PR loop is at/over the cap.

    The in-flight cap (default 2) bounds how many issues may be in the
    Gordon → Kleiner → Eli → merged loop at once. "In flight" is any issue
    carrying a mid-pipeline label (agent-working / needs-review / approved) OR a
    locally-running watch run — the count is computed by ``loop_in_flight_count``
    and fed in by the caller. At or over the cap ⇒ withhold; under ⇒ the next
    claim may start.

    This generalizes #53's open-PR cap: an issue that is ``agent-working`` but has
    not yet opened a PR also counts against the flight budget (the gap #53 alone
    left open).

    ``cap`` <= 0 is treated as "no limit" (never withhold).
    """
    if cap <= 0:
        return False
    return in_flight_count >= cap


def running_watch_issue_numbers(
    runs: "Iterable[dict]", *, statuses: "tuple[str, ...]" = ("running",)
) -> "set[int]":
    """PURE. The set of issue numbers from watch runs (any stage: claim/review/land)
    that are in any of the given ``statuses`` (default: running only).

    Parses the agent_id (``watch-issue-N`` / ``review-issue-N`` / ``land-issue-N``)
    to extract the issue number. Runs whose agent_id does not match any stage prefix
    are silently skipped. Used by ``loop_in_flight_count`` to dedup running runs
    against the mid-pipeline label set."""
    out: set[int] = set()
    for r in runs:
        if r.get("status") not in statuses:
            continue
        agent_id = str(r.get("agent_id", ""))
        for prefix in ("watch-issue-", "review-issue-", "land-issue-"):
            if agent_id.startswith(prefix):
                try:
                    out.add(int(agent_id[len(prefix):].split("-")[0]))
                except (ValueError, IndexError):
                    pass
                break
    return out


def loop_in_flight_count(
    *, pipeline_issue_nums: "set[int]", running_watch_issue_nums: "set[int]"
) -> int:
    """PURE. The number of DISTINCT issues currently in flight in the issue→PR
    loop — the union of mid-pipeline labeled issues and locally-running watch
    runs, deduped by issue number.

    A single issue can briefly appear in BOTH sets (e.g. Gordon applies
    ``agent-working`` AND has a locally-running ``watch-issue-N`` run). Taking
    the union size means that issue counts as 1, not 2 — which matters now that
    the loop cap is 2 instead of 1 (at cap=1 the double-count was invisible
    because the gate tripped at any count ≥ 1).

    Kept a pure union so the in-flight definition is testable as set data
    (mirrors how ``decide_launches`` keeps the decision free of I/O). The caller
    fails CLOSED on a counting error — a gather failure withholds the tick
    rather than risk a second concurrent run.
    """
    return len(set(pipeline_issue_nums) | set(running_watch_issue_nums))


def stage_withheld(
    stage: str,
    *,
    in_flight_count: int,
    open_agent_pr_count: int,
    same_stage_running: int,
    agent_pr_cap: int = DEFAULT_AGENT_PR_CAP,
    loop_in_flight_cap: int = DEFAULT_LOOP_IN_FLIGHT_CAP,
) -> bool:
    """PURE. True if a watch agent of ``stage`` should be WITHHELD this tick (#78).

    The arm-toggle + architecture-freeze gates are checked upstream (full stop for
    ALL stages); this decides the per-stage gate AFTER those pass. The three stages
    gate differently because they play different roles in the strict-serial chain:

      * ``claim`` (Gordon) — a NEW claim. Withheld while the in-flight count is
        at/over ``loop_in_flight_cap`` (``should_withhold_loop``) OR open agent PRs
        are at/over ``agent_pr_cap`` (``should_withhold_watch``) — the #64 + #53
        gates. Also withheld while ``same_stage_running >= loop_in_flight_cap``
        (hard overlap guard — at most ``loop_in_flight_cap`` concurrent Gordon runs).
      * ``review`` (Kleiner) / ``land`` (Eli) — these ADVANCE the one in-flight
        issue, so the in-flight / open-PR gates do NOT apply to them (gating them on
        "something is in flight" would deadlock the chain — there is ALWAYS something
        in flight when they have work). They are withheld ONLY to prevent overlap:
        a second same-stage run while one is already RUNNING (``same_stage_running >= 1``).

    An unknown stage is treated as ``claim`` (fail safe — the most conservative
    gate). ``same_stage_running`` is the count of RUNNING runs of THIS stage's
    prefix (the caller reads it from the run store)."""
    if stage in ("review", "land"):
        # Advancing stages: overlap guard only (at most 1 concurrent run).
        if same_stage_running >= 1:
            return True
        return False
    # claim (and any unknown stage, fail-safe): the strict-serial + cross-PR gates
    # PLUS the overlap guard at loop_in_flight_cap.
    if same_stage_running >= loop_in_flight_cap:
        return True
    return should_withhold_loop(in_flight_count, cap=loop_in_flight_cap) or should_withhold_watch(
        open_agent_pr_count, cap=agent_pr_cap
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Pill counts — the four-pill HITL notification badge (#65, parent PRD #58)
# ═══════════════════════════════════════════════════════════════════════════════

def derive_pill_counts(
    *,
    needs_signoff: int,
    needs_trade_approval: int,
    needs_human: int,
    halted_runs: int,
    is_frozen: bool,
    unacked_completed: int,
) -> dict:
    """PURE. Map the agent-manager's raw view of the loop into the dashboard's four
    notification-pill counts (#65). Table-testable; the manager method is the thin
    gatherer that feeds these inputs in (mirrors how ``decide_launches`` keeps the
    decision free of I/O).

    The four pills:

      * 🟢 ``green``  — completed work the operator has not yet acknowledged
        (``unacked_completed``: succeeded runs whose run_id is not in the seen set).
        Viewing the completed view acks them, so this drops to 0.
      * 🟡 ``yellow`` — issues awaiting the PM's supervised sign-off
        (``needs_signoff``; the dormant Slice-C gate — zero unless supervised mode
        put something there).
      * 🟣 ``purple`` — issues parked at the #63 money wall awaiting trading-surface
        approval (``needs_trade_approval``).
      * 🔴 ``red``    — problems: issues an agent halted to ``needs-human`` PLUS
        locally-halted runs PLUS the architecture freeze (counted as ONE if frozen).
        The N badge is this sum.

    All inputs are coerced to non-negative ints; ``is_frozen`` contributes exactly
    one to ``red`` when true. No I/O, no clamping surprises — a counting error
    upstream is the caller's concern, not this function's.
    """
    def _n(v: int) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

    return {
        "green": _n(unacked_completed),
        "yellow": _n(needs_signoff),
        "purple": _n(needs_trade_approval),
        "red": _n(needs_human) + _n(halted_runs) + (1 if is_frozen else 0),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Loop health — why the claim loop is (or isn't) advancing, for the dashboard panel
# ═══════════════════════════════════════════════════════════════════════════════

def derive_withhold_reason(
    *,
    armed: bool,
    frozen: bool,
    in_flight_count: int,
    open_agent_pr_count: int,
    agent_pr_cap: int = DEFAULT_AGENT_PR_CAP,
    loop_in_flight_cap: int = DEFAULT_LOOP_IN_FLIGHT_CAP,
) -> tuple[bool, str | None, str | None]:
    """PURE. Is the CLAIM stage being withheld right now, and why (plain English)?

    Mirrors the EXACT gate order the scheduler applies to a claim agent in
    ``_due_watch_candidates`` so the panel never disagrees with reality: arm switch
    → architecture freeze → strict-serial in-flight (#64) → open-PR cap (#53). The
    first failing gate wins (it's the one the operator must clear first).

    Returns ``(withheld, reason, kind)`` — ``reason`` is None only when nothing is
    withholding. ``kind`` names the winning gate (``disarmed`` / ``frozen`` /
    ``in_flight`` / ``pr_cap``, or None) so the panel can style the BENIGN hold
    (``in_flight`` — the loop is healthy, just at its concurrency cap) differently
    from the ones that actually need an operator (the rest).
    """
    if not armed:
        return True, "Autonomous watch is DISARMED — arm it to start claiming issues.", "disarmed"
    if frozen:
        return True, "Loop is FROZEN on architecture drift — clear the freeze to resume.", "frozen"
    if should_withhold_loop(in_flight_count, cap=loop_in_flight_cap):
        # Benign: the loop is running, just at its in-flight cap. The panel renders
        # this as a quiet white note, NOT an amber alarm.
        return True, f"{in_flight_count} issue(s) in flight (cap {loop_in_flight_cap}) — claiming the next once one lands.", "in_flight"
    if should_withhold_watch(open_agent_pr_count, cap=agent_pr_cap):
        return True, (
            f"{open_agent_pr_count} open agent PR at the cap ({agent_pr_cap}) — the "
            "next issue can't be claimed until it merges or closes."
        ), "pr_cap"
    return False, None, None


def derive_loop_blockers(
    *,
    open_agent_prs: Iterable[dict],
    pipeline_issue_nums: set[int],
    needs_human_nums: set[int],
    frozen: bool,
    frozen_reason: str | None,
) -> list[dict]:
    """PURE. The concrete things pinning the claim loop, each a dashboard card.

    Inputs are gathered by the manager (one gh call each); this classifies them so
    the classification is table-testable. Each blocker dict carries:

      * ``kind``       — ``needs_human`` | ``frozen`` | ``orphan_pr``
      * ``severity``   — ``info`` (no button; resolve in a Claude Code session) or
                         ``action`` (a one-click mechanical unblock — no judgment)
      * ``resolvable`` — whether the panel shows a button (== severity action)
      * ``title`` / ``detail`` — plain-English copy
      * ``issue`` / ``pr`` — the numbers the operator needs (either may be None)
      * ``action``     — ``{"type": "close_pr"|"clear_freeze", ...}`` or None

    The split that keeps the panel from crying wolf:

      * An open agent PR whose issue is STILL in a pipeline stage (agent-working /
        needs-review / approved) is HEALTHY back-pressure — Kleiner/Eli are
        advancing it — so it is NOT a blocker.
      * An open agent PR whose issue is ``needs-human`` is a HALT — a judgment call.
        Surfaced as ``info`` (issue + PR numbers) to take into a Claude Code session;
        no button, because deciding what to do with the halt is the human's call.
      * An open agent PR whose issue is in NEITHER set (closed/deleted/label stripped)
        is a genuine ORPHAN — nothing will ever advance or merge it, and there is no
        pending decision — so it gets a mechanical ``close_pr`` button.
      * A ``needs-human`` issue with NO open PR is a pure triage halt — ``info`` too.
      * An architecture freeze is a mechanical ``clear_freeze`` button.
    """
    blockers: list[dict] = []

    if frozen:
        blockers.append({
            "kind": "frozen", "severity": "action", "resolvable": True,
            "title": "Architecture freeze is active",
            "detail": (frozen_reason or "The loop was frozen on scope drift.")
            + " New claims are off until it's cleared.",
            "issue": None, "pr": None,
            "action": {"type": "clear_freeze"},
        })

    human_with_pr: set[int] = set()
    for pr in open_agent_prs:
        num = pr.get("number")
        issue = pr.get("issue")
        if issue is not None and issue in needs_human_nums:
            human_with_pr.add(issue)
            blockers.append({
                "kind": "needs_human", "severity": "info", "resolvable": False,
                "title": f"Agent halted on issue #{issue} — needs your judgment",
                "detail": (
                    f"PR #{num} is open but the issue halted, so nothing in the loop "
                    "will advance or merge it — it's pinning the claim gate. Resolve "
                    "it in a Claude Code session (closing or merging the PR there "
                    "lets the loop move again)."
                ),
                "issue": issue, "pr": num,
                "action": None,
            })
        elif issue is None or issue not in pipeline_issue_nums:
            blockers.append({
                "kind": "orphan_pr", "severity": "action", "resolvable": True,
                "title": f"Orphaned agent PR #{num}",
                "detail": (
                    "This PR's issue isn't in any pipeline stage, so nothing will "
                    "advance or merge it — it just pins the claim gate. Closing it "
                    "lets the loop resume. No judgment needed."
                ),
                "issue": issue, "pr": num,
                "action": {"type": "close_pr", "pr": num},
            })
        # else: issue is live in the pipeline → healthy back-pressure, not a blocker.

    for issue in sorted(needs_human_nums - human_with_pr):
        blockers.append({
            "kind": "needs_human", "severity": "info", "resolvable": False,
            "title": f"Agent halted on issue #{issue} — needs your judgment",
            "detail": "Resolve it in a Claude Code session.",
            "issue": issue, "pr": None,
            "action": None,
        })

    return blockers


# ═══════════════════════════════════════════════════════════════════════════════
# Candidate — one ready-to-launch agent fed to the decision core
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stage priority for candidate ordering ──────────────────────────────────
# When two watch candidates are due at the same ready_since, claim wins over
# review, which wins over land.  Claim creates NEW work in the pipeline; land
# finishes EXISTING work — a delayed claim starves the whole chain, a delayed
# land just waits one more tick.  Clock/manual candidates default to 0 (claim
# priority) so they're never starved by a watch stage.
STAGE_PRIORITY: dict[str, int] = {"claim": 0, "review": 1, "land": 2}


@dataclass(frozen=True)
class Candidate:
    """One agent that is READY to launch this tick.

    The caller (the scheduler loop) builds these: a clock agent becomes a
    candidate when its schedule is due, a manual run-now is a candidate
    immediately, and (slice #35) a watch agent becomes a candidate when its
    GitHub condition holds.

    Fields
    ------
    agent_id    : the agent definition id (also the in-flight exclusion key).
    ready_since : the monotonic-ish moment this agent became ready — the
                  oldest-first ordering key (smaller = older = launched first).
                  For a clock agent this is the schedule's due time; for a manual
                  run it is the enqueue time.
    blocked     : True if a dependency is unsatisfied (skip-blocked). Defaults
                  False; the clock/manual paths never set it — #35's issue→PR
                  path will.
    stage       : the watch stage ("claim" / "review" / "land") for tie-breaking
                  when two candidates share the same ready_since. Clock/manual
                  candidates default to "claim" (highest priority). The sort key
                  is (ready_since, STAGE_PRIORITY[stage], agent_id) so claim
                  never waits behind a land poll that would be a no-op.
    """

    agent_id: str
    ready_since: float
    blocked: bool = False
    stage: str = "claim"


# ═══════════════════════════════════════════════════════════════════════════════
# The pure decision core
# ═══════════════════════════════════════════════════════════════════════════════

def decide_launches(
    candidates: Iterable[Candidate],
    *,
    in_flight: set[str],
    cap: int = DEFAULT_CONCURRENCY_CAP,
) -> list[str]:
    """Decide which agents to launch this tick — a PURE function.

    Parameters
    ----------
    candidates : the agents READY to launch (clock-due / manual / #35 watch).
    in_flight  : agent_ids currently running OR queued (the live concurrency).
                 Counts against the cap AND is the in-flight exclusion set.
    cap        : the global concurrency cap (default 2).

    Returns
    -------
    The ordered list of agent_ids to launch NOW — oldest-first, blocked skipped,
    in-flight excluded, length never exceeding the number of FREE slots
    (``cap - len(in_flight)``, floored at 0). Overflow is intentionally dropped
    (it re-queues as a candidate next tick).

    Invariants enforced (see module docstring): cap, oldest-first, skip-blocked,
    in-flight exclusion. The function never mutates its inputs.
    """
    free_slots = max(0, cap - len(in_flight))
    if free_slots <= 0:
        return []

    # Oldest-first; ties broken on stage priority (claim > review > land), then
    # agent_id for determinism.  Stage priority prevents land from starving claim
    # when both are due on the same tick with only one free slot.
    ordered = sorted(
        candidates,
        key=lambda c: (c.ready_since, STAGE_PRIORITY.get(c.stage, 0), c.agent_id),
    )

    launched: list[str] = []
    # Track ids we've already chosen this tick so a duplicate candidate (same
    # agent listed twice) can't take two slots — it becomes "in flight" the
    # moment we pick it.
    chosen: set[str] = set(in_flight)

    for cand in ordered:
        if len(launched) >= free_slots:
            break
        if cand.blocked:
            continue  # skip-blocked: does NOT consume a slot
        if cand.agent_id in chosen:
            continue  # in-flight exclusion / dedupe within the tick
        launched.append(cand.agent_id)
        chosen.add(cand.agent_id)

    return launched


# ═══════════════════════════════════════════════════════════════════════════════
# Clock seam — cron_is_due (pure: now + schedule in, bool out)
# ═══════════════════════════════════════════════════════════════════════════════

# Supported schedule grammar: standard 5-field cron
#   minute hour day-of-month month day-of-week
# Each field is one of:
#   *            any value
#   N            a single value
#   N,M,...      a list
#   A-B          an inclusive range
#   */K          every K (step over the whole range)
#   A-B/K        every K within a range
# This is the common cron subset — enough for "every minute", hourly, daily,
# weekday-at-time, etc. Anything it can't parse is treated as NEVER due (safe:
# a malformed schedule never fires rather than firing constantly).

_CRON_RANGES = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 7),    # day of week — standard cron: Sunday=0..Saturday=6, with 7 also
               # meaning Sunday (folded onto 0 at match time). (#116, M4)
)


def _parse_cron_field(field_str: str, lo: int, hi: int) -> set[int] | None:
    """Expand one cron field into the set of integers it matches, or None if it
    is malformed (caller treats None as 'never matches')."""
    values: set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        has_step = "/" in part
        if has_step:
            base, _, step_str = part.partition("/")
            try:
                step = int(step_str)
            except ValueError:
                return None
            if step <= 0:
                return None
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                return None
        else:
            try:
                start = int(base)
            except ValueError:
                return None
            # A bare numeric base paired with a step means "from here, step by
            # `step` up to the field max" — e.g. minute `5/20` -> 5,25,45. Without
            # a step it collapses to the single value. (#116, L5)
            end = hi if has_step else start

        if start < lo or end > hi or start > end:
            return None
        values.update(range(start, end + 1, step))

    return values or None


def cron_is_due(schedule: str, now: float) -> bool:
    """True if ``schedule`` (5-field cron) matches the local-time minute of
    ``now`` (a POSIX timestamp). PURE: no clock read — ``now`` is passed in.

    Day-of-month and day-of-week both default to a match when '*'; when BOTH are
    restricted, standard cron fires if EITHER matches (the classic OR rule).
    A malformed schedule is never due.
    """
    parts = schedule.split()
    if len(parts) != 5:
        return False

    fields = [
        _parse_cron_field(p, lo, hi)
        for p, (lo, hi) in zip(parts, _CRON_RANGES)
    ]
    if any(f is None for f in fields):
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    t = time.localtime(now)
    if t.tm_min not in minute_f:
        return False
    if t.tm_hour not in hour_f:
        return False
    if t.tm_mon not in month_f:
        return False

    # Standard 5-field cron day-of-week: Sunday=0..Saturday=6, with 7 also
    # meaning Sunday. Python's tm_wday is Monday=0..Sunday=6, so convert with
    # (tm_wday + 1) % 7 (Mon->1, ..., Sat->6, Sun->0) and fold a literal 7 in the
    # field onto 0 so `* * * * 7` matches Sunday. (#116, M4)
    dom_full = set(range(_CRON_RANGES[2][0], _CRON_RANGES[2][1] + 1))
    dow_full = set(range(_CRON_RANGES[4][0], _CRON_RANGES[4][1] + 1))
    dom_restricted = dom_f != dom_full
    dow_restricted = dow_f != dow_full
    cron_dow = (t.tm_wday + 1) % 7
    dom_match = t.tm_mday in dom_f
    dow_match = cron_dow in dow_f or (cron_dow == 0 and 7 in dow_f)

    # cron's classic day-of-month / day-of-week OR rule:
    if dom_restricted and dow_restricted:
        return dom_match or dow_match
    return dom_match and dow_match


def wall_clock_minute_key(now: float) -> float:
    """A stable identity for the LOCAL wall-clock minute of ``now`` — the
    anti-double-fire ledger key for clock agents. PURE: no clock read.

    Keyed on the local calendar minute (year/month/day/hour/minute via
    ``time.localtime``) rather than the epoch minute (``now // 60``) so the dedup
    basis MATCHES the match basis — ``cron_is_due`` also matches on local time.
    During a DST fall-back / backward clock step the local 01:00 minute recurs at
    two different epoch minutes; an epoch key sees two distinct minutes and a
    ``0 1 * * *`` agent fires twice, whereas this wall-clock key is identical for
    both occurrences, so the agent fires exactly once. (#116, M5)
    """
    t = time.localtime(now)
    return float(
        t.tm_year * 100_000_000
        + t.tm_mon * 1_000_000
        + t.tm_mday * 10_000
        + t.tm_hour * 100
        + t.tm_min
    )
