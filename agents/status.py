"""
trident_agent_status.py — the dashboard STATUS view-model assembler (issue #141,
parent #138).

What this is
------------
A read-only module that owns the GATHER-then-DERIVE flow behind the three
agent-manager status surfaces the dashboard polls:

* :meth:`StatusAggregator.pill_counts` — the four-pill HITL notification badge
  (green / yellow / purple / red — #65).
* :meth:`StatusAggregator.loop_status` — the claim-loop health panel (why the
  loop is / isn't advancing, plus the concrete blocker cards — #64/#53/#62).
* :meth:`StatusAggregator.pulse_status` — the cheap, gh-free AGENTS-tab glow
  signal (any run in flight + the scheduler-tick counter).

Why a separate module
---------------------
The *decisions* already live as PURE, table-tested functions in
``trident_agent_scheduler`` (``derive_pill_counts``, ``derive_withhold_reason``,
``derive_loop_blockers``, ``loop_in_flight_count``). What stayed welded into
``AgentManagerServer`` was the *gathering* around them: the gh reads (via the
gateway) and the run-store / ack-marker / freeze reads, plus the fail-soft
wrapping that turns a downed gateway into a safe baseline view instead of an
exception out of the RPC. This module is that gather layer, lifted out so the
manager composes it and its ``_cmd_*`` handlers delegate to it.

It performs NO writes and NO side-effects — every method is a pure read that
returns a dict the dashboard renders directly. The manager keeps owning the
stores, the gateway factory, the lock, and the run-store helpers; this class is
constructed with those injected (as the manager's own live-resolving seams) and
reaches back into none of the manager's mutating machinery.

The injected seams are deliberately INDIRECTIONS, not snapshots: the manager's
tests reassign ``_gateway_factory`` / ``_loop_gate`` AFTER construction and flip
the watch toggle / bump the tick counter at runtime, so the gateway factory, the
loop gate, the watch-arm reader, and the tick-seq reader are passed as thunks
that resolve the manager's CURRENT value on each call. The ack markers, the
run-store readers, and the pure derivers are stable, so they're passed directly.

Run:  python -m pytest tests/test_agent_status.py -q
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from agents.run_store import HALTED, QUEUED, RUNNING, SUCCEEDED
from agents.scheduler import (
    DEFAULT_AGENT_PR_CAP,
    DEFAULT_LOOP_IN_FLIGHT_CAP,
    derive_loop_blockers,
    derive_pill_counts,
    derive_withhold_reason,
    loop_in_flight_count,
    running_watch_issue_numbers,
)

_LOG = logging.getLogger("agent_manager")


@dataclass(frozen=True)
class _GhBundle:
    """The gateway gh-read results both the pill and loop view-models consume —
    fetched once per TTL window and shared. ``ok`` is False when the gh read
    failed; callers map that to their own baseline (pill → zeros, loop → degraded
    gh fields with ``gh_ok=False``)."""

    ok: bool
    needs_signoff: int = 0
    needs_trade_approval: int = 0
    # pill consumes the RAW needs-human issue list (its dedup counts number-less
    # issues, which a set of numbers would silently drop); loop consumes the set
    # of numbers. Each fetch fills only the field its consumer needs.
    needs_human_issues: list = field(default_factory=list)
    needs_human_numbers: "set[int]" = field(default_factory=set)
    open_agent_prs: list = field(default_factory=list)
    pipeline_issue_nums: "set[int]" = field(default_factory=set)

    @classmethod
    def empty(cls) -> "_GhBundle":
        """The not-ok baseline — empty everything, ``ok=False``."""
        return cls(ok=False)


class StatusAggregator:
    """Assembles the pill-counts / loop-status / pulse-status view-models from
    injected stores + gateway + the pure scheduler derivers.

    Read-only and fail-soft: a gh/store read error yields the same baseline view
    the manager produced inline before this extraction, never an exception out of
    the RPC.

    Parameters
    ----------
    list_runs
        ``() -> list[dict]`` — the manager's lock-held run lister (``_list_runs``).
    gateway_factory_provider
        ``() -> (() -> gateway)`` — returns the manager's CURRENT gateway factory.
        A provider (not the factory itself) so a test reassigning
        ``mgr._gateway_factory`` after construction is still honored.
    loop_gate_provider
        ``() -> LoopGateLike`` — returns the manager's CURRENT loop gate (likewise
        a provider so a post-construction ``mgr._loop_gate = ...`` is honored).
    completed_ack / problems_ack
        The two ``JsonSet`` ack markers (succeeded-seen / halted-acked).
    run_issue_number
        ``(dict) -> int | None`` — the manager's run→issue-number extractor, used
        to dedup a halted run against its own needs-human label in the red pill.
    watch_gate_on
        ``() -> bool`` — the manager's effective arm-state reader.
    count_running_stage_runs
        ``(prefix: str) -> int`` — the manager's per-stage running-run counter.
    scheduler_tick_seq
        ``() -> int`` — reads the manager's current monotonic tick counter.
    change_epoch
        ``() -> int`` — reads the manager's current change-epoch counter (#150).
        Generalizes tick_seq to all state changes (run create/update/halt, ack
        markers). Bumped on every mutation; pure reads never advance it.
    agent_pr_cap
        The #53 open-agent-PR cap (fixed at manager construction).
    loop_in_flight_cap
        The claim-loop in-flight cap (fixed at manager construction). Governs
        how many issues may be in the Gordon→Kleiner→Eli loop at once.
    logger
        Where the fail-soft warnings go (defaults to the ``agent_manager`` log).

    The pure derivers (``pill_deriver`` / ``withhold_deriver`` / ``blockers_deriver``
    / ``in_flight_counter``) are injected too — defaulting to the real scheduler
    functions — so the assembly seam stays explicit and fully fakeable.
    """

    def __init__(
        self,
        *,
        list_runs: Callable[[], list[dict]],
        gateway_factory_provider: Callable[[], Callable[[], object]],
        loop_gate_provider: Callable[[], object],
        completed_ack,
        problems_ack,
        run_issue_number: Callable[[dict], "int | None"],
        watch_gate_on: Callable[[], bool],
        count_running_stage_runs: Callable[[str], int],
        scheduler_tick_seq: Callable[[], int],
        change_epoch: Callable[[], int] = (lambda: 0),
        agent_pr_cap: int = DEFAULT_AGENT_PR_CAP,
        loop_in_flight_cap: int = DEFAULT_LOOP_IN_FLIGHT_CAP,
        logger: "logging.Logger | None" = None,
        pill_deriver: Callable[..., dict] = derive_pill_counts,
        withhold_deriver: Callable[..., tuple] = derive_withhold_reason,
        blockers_deriver: Callable[..., list] = derive_loop_blockers,
        in_flight_counter: Callable[..., int] = loop_in_flight_count,
        clock: Callable[[], float] = time.monotonic,
        gh_cache_ttl_s: float = 10.0,
    ) -> None:
        self._list_runs = list_runs
        self._gateway_factory_provider = gateway_factory_provider
        self._loop_gate_provider = loop_gate_provider
        self._completed_ack = completed_ack
        self._problems_ack = problems_ack
        self._run_issue_number = run_issue_number
        self._watch_gate_on = watch_gate_on
        self._count_running_stage_runs = count_running_stage_runs
        self._scheduler_tick_seq = scheduler_tick_seq
        self._change_epoch = change_epoch
        self._agent_pr_cap = agent_pr_cap
        self._loop_in_flight_cap = loop_in_flight_cap
        self._log = logger or _LOG
        self._pill_deriver = pill_deriver
        self._withhold_deriver = withhold_deriver
        self._blockers_deriver = blockers_deriver
        self._in_flight_counter = in_flight_counter
        # Short-TTL memo for the EXPENSIVE gh reads only. pill_counts + loop_status
        # each shell out to `gh` several times (~1.5s + ~2.6s); the panel polls every
        # few seconds and #149 chained them into ONE snapshot RPC, so a consolidated
        # poll ran ~4.8s — over the 5s RPC timeout → intermittent 503 that blanked
        # the whole panel. Each method caches ONLY its own gh reads (NOT the assembled
        # counts): the LOCAL run-store / ack / freeze reads stay live on every call,
        # so acking a halt or toggling the gate reflects in the badge immediately
        # while back-to-back polls reuse the gh snapshot. The two caches are
        # INDEPENDENT so a partial gh outage degrades only the affected view-model
        # (a pill-only gateway never zeros because a loop read failed). gh numbers
        # being a few seconds stale is fine. TTL 0 disables the memo.
        self._clock = clock
        self._gh_cache_ttl_s = gh_cache_ttl_s
        self._pill_gh_cache: "tuple[float, _GhBundle] | None" = None
        self._loop_gh_cache: "tuple[float, _GhBundle] | None" = None

    # ── TTL memo for the expensive gh reads (one cache per view-model) ────────────

    def _cached_gh(self, slot: str, fetch: Callable[[], "_GhBundle"]) -> "_GhBundle":
        """Return the memoized bundle in ``slot`` if younger than the TTL, else
        ``fetch`` a fresh one, restamp, and return it. ``slot`` is the cache attr
        (``"_pill_gh_cache"`` / ``"_loop_gh_cache"``). TTL 0 recomputes every call."""
        now = self._clock()
        cached = getattr(self, slot)
        if cached is not None and (now - cached[0]) < self._gh_cache_ttl_s:
            return cached[1]
        bundle = fetch()
        setattr(self, slot, (now, bundle))
        return bundle

    def _pill_gh(self) -> "_GhBundle":
        """The cached gh reads pill_counts needs (signoff / trade-approval /
        needs-human numbers). A gh error → ``ok=False`` baseline."""
        return self._cached_gh("_pill_gh_cache", self._fetch_pill_gh)

    def _fetch_pill_gh(self) -> "_GhBundle":
        try:
            gw = self._gateway_factory_provider()()
            return _GhBundle(
                ok=True,
                needs_signoff=len(gw.list_needs_signoff_issues()),
                needs_trade_approval=len(gw.list_needs_trade_approval_issues()),
                needs_human_issues=list(gw.list_needs_human_issues()),
            )
        except Exception:  # noqa: BLE001 — never let a gh blip crash the dashboard
            self._log.warning("pill_counts: gateway error — reporting zeros",
                              exc_info=True)
            return _GhBundle.empty()

    def _loop_gh(self) -> "_GhBundle":
        """The cached gh reads loop_status needs (open agent PRs / mid-pipeline issue
        numbers / needs-human numbers). A gh error → ``ok=False`` baseline."""
        return self._cached_gh("_loop_gh_cache", self._fetch_loop_gh)

    def _fetch_loop_gh(self) -> "_GhBundle":
        try:
            gw = self._gateway_factory_provider()()
            return _GhBundle(
                ok=True,
                open_agent_prs=list(gw.list_open_agent_prs()),
                pipeline_issue_nums={
                    iss.number for iss in (
                        gw.list_working_issues()
                        + gw.list_needs_review_issues()
                        + gw.list_approved_issues()
                    )
                },
                needs_human_numbers={iss.number for iss in gw.list_needs_human_issues()},
            )
        except Exception:  # noqa: BLE001 — a gh blip degrades, never crashes the panel
            self._log.warning("loop_status: gateway error — degraded picture",
                              exc_info=True)
            return _GhBundle.empty()

    def warm_gh(self) -> None:
        """Refresh BOTH expensive gh caches (pill + loop) in one shot, off the
        critical path. The manager's snapshot-warm loop calls this every few
        seconds WHILE the agent panel is open (#151 perf), so a subsequent
        pill_counts()/loop_status()/snapshot() reads a warm cache (sub-second)
        instead of paying the ~8-call cold cost on a user-facing push. The
        underlying fetchers already swallow a gh blip into an ``ok=False``
        baseline, so this never raises."""
        self._pill_gh()
        self._loop_gh()

    # ── 🟢🟡🟣🔴 the four-pill HITL badge (#65) ─────────────────────────────────

    def pill_counts(self) -> dict:
        """The four notification-pill counts for the dashboard badge (#65):
        ``{"green", "yellow", "purple", "red"}``.

          * 🟢 green  — succeeded runs the operator has not acknowledged (cleared
            by ``ack_completed`` when the completed view is opened).
          * 🟡 yellow — issues in ``needs-signoff`` (supervised PM gate, Slice C).
          * 🟣 purple — issues in ``needs-trade-approval`` (the #63 money wall).
          * 🔴 red    — ``needs-human`` issues + locally-halted runs + the
            architecture freeze (counts as one).

        Derivation is the PURE ``derive_pill_counts``; this method only GATHERS the
        inputs. Best-effort: a gateway/gh error returns ZEROS (a downed gateway must
        never crash the dashboard) but is logged. The freeze + run-store inputs are
        local (no gh) so they survive a gh blip via the same zero fallback.

        DEDUP: an issue-tied watch halt writes BOTH a needs-human label (GitHub) AND
        a HALTED run-store record — two representations of ONE problem. Counting both
        double-counts the red pill (acking the run dropped it 2→1 with no second run
        to find). So a needs-human issue that already has a halted run is counted
        only via the run; the standalone needs-human issues (no run) still count. We
        key the dedup on ANY halted run for the issue (acked or not) so acknowledging
        the run zeroes the whole problem rather than re-surfacing it as needs-human."""
        # gh inputs come from the TTL-cached pill bundle. A not-ok bundle = a gh blip
        # → report ZEROS, same as the pre-cache early-return (a downed gateway must
        # never crash the panel).
        gh = self._pill_gh()
        if not gh.ok:
            return {"green": 0, "yellow": 0, "purple": 0, "red": 0}
        needs_signoff = gh.needs_signoff
        needs_trade_approval = gh.needs_trade_approval

        # Local inputs (run store + freeze sentinel) — no gh, best-effort each.
        # halted_issue_nums spans ALL halted runs (acked or not) so the dedup below
        # removes a needs-human issue once it has a run, acknowledged or not.
        halted_issue_nums: set[int] = set()
        try:
            runs = self._list_runs()
            problems_acked = self._problems_ack.load()
            halted_runs = sum(
                1 for r in runs
                if r.get("status") == HALTED and r.get("run_id") not in problems_acked
            )
            halted_issue_nums = {
                n for r in runs if r.get("status") == HALTED
                for n in (self._run_issue_number(r),) if n is not None
            }
            acked = self._completed_ack.load()
            unacked_completed = len(
                {r["run_id"] for r in runs
                 if r.get("status") == SUCCEEDED and r.get("run_id")} - acked
            )
        except Exception:  # noqa: BLE001
            halted_runs, unacked_completed = 0, 0

        # Dedup the GitHub needs-human set against issues already represented by a
        # halted run; an issue with no run still counts (it's a pure triage item).
        # A number-less issue (getattr → None) is never in halted_issue_nums, so it
        # always counts — matching the pre-cache behavior.
        needs_human = sum(
            1 for iss in gh.needs_human_issues
            if getattr(iss, "number", None) not in halted_issue_nums
        )
        try:
            is_frozen = self._loop_gate_provider().is_frozen()
        except Exception:  # noqa: BLE001
            is_frozen = False

        return self._pill_deriver(
            needs_signoff=needs_signoff,
            needs_trade_approval=needs_trade_approval,
            needs_human=needs_human,
            halted_runs=halted_runs,
            is_frozen=is_frozen,
            unacked_completed=unacked_completed,
        )

    # ── 🩺 loop health (why the claim loop is / isn't advancing — the panel) ─────

    def loop_status(self) -> dict:
        """The full claim-loop health picture for the dashboard's loop-health panel.

        GATHERS the live gate inputs (arm toggle, freeze, in-flight count, open agent
        PRs, per-stage running runs) and hands them to the PURE deciders
        (``derive_withhold_reason`` for the headline, ``derive_loop_blockers`` for the
        concrete cards) — same gatherer/pure split as ``pill_counts``. Best-effort: a
        gh error degrades the gh-derived fields to a safe baseline and sets
        ``gh_ok=False`` rather than crashing the panel; the local inputs (toggle,
        freeze, run store) survive a gh blip.

        Returns a dict the panel renders directly: ``armed``, ``frozen`` +
        ``frozen_reason``, ``in_flight``, ``open_prs``, ``pr_cap``, ``stage_running``
        (claim/review/land), ``claims_withheld`` + ``withhold_reason`` +
        ``withhold_kind`` (the headline, and which gate caused it so the panel can
        style the benign in-flight hold quietly), ``blockers`` (the cards), and
        ``gh_ok``.
        """
        armed = self._watch_gate_on()
        loop_gate = self._loop_gate_provider()
        try:
            frozen = loop_gate.is_frozen()
            frozen_reason = loop_gate.frozen_reason() if frozen else None
        except Exception:  # noqa: BLE001
            frozen, frozen_reason = False, None

        # Per-stage running counts — cheap run-store reads, no gh. Always available.
        stage_running = {
            "claim": self._count_running_stage_runs("watch-issue-"),
            "review": self._count_running_stage_runs("review-issue-"),
            "land": self._count_running_stage_runs("land-issue-"),
        }

        # gh-derived inputs from the TTL-cached loop bundle. A not-ok bundle degrades
        # to a baseline that reports "can't see GitHub right now" rather than inventing
        # a clear loop; the local inputs (toggle, freeze, run store) above survive it.
        gh = self._loop_gh()
        gh_ok = gh.ok
        open_agent_prs = gh.open_agent_prs
        pipeline_issue_nums = gh.pipeline_issue_nums
        needs_human_nums = gh.needs_human_numbers

        open_prs = len(open_agent_prs)
        # in_flight REUSES the pipeline set already fetched above (mid-pipeline-label
        # issues) + locally-running watch runs (run store, no gh), DEDUPED by issue
        # number so a run that already applied its label doesn't double-count.
        # On a gh error the pipeline set is empty, so in_flight degrades to just the
        # local running-run count.
        running_issue_nums = running_watch_issue_numbers(self._list_runs())
        in_flight = self._in_flight_counter(
            pipeline_issue_nums=pipeline_issue_nums,
            running_watch_issue_nums=running_issue_nums,
        )

        claims_withheld, withhold_reason, withhold_kind = self._withhold_deriver(
            armed=armed, frozen=frozen, in_flight_count=in_flight,
            open_agent_pr_count=open_prs, agent_pr_cap=self._agent_pr_cap,
            loop_in_flight_cap=self._loop_in_flight_cap,
        )
        blockers = self._blockers_deriver(
            open_agent_prs=open_agent_prs,
            pipeline_issue_nums=pipeline_issue_nums,
            needs_human_nums=needs_human_nums,
            frozen=frozen, frozen_reason=frozen_reason,
        )
        return {
            "armed": armed,
            "frozen": frozen,
            "frozen_reason": frozen_reason,
            "in_flight": in_flight,
            "open_prs": open_prs,
            "pr_cap": self._agent_pr_cap,
            "loop_in_flight_cap": self._loop_in_flight_cap,
            "stage_running": stage_running,
            "claims_withheld": claims_withheld,
            "withhold_reason": withhold_reason,
            "withhold_kind": withhold_kind,
            "blockers": blockers,
            "gh_ok": gh_ok,
        }

    # ── ✨ pulse (cheap gh-free AGENTS-tab glow) ─────────────────────────────────

    def pulse_status(self) -> dict:
        """Cheap, gh-free signal driving the Claude-tab AGENTS button glow/pulse.

        ``active`` — any agent run is in flight right now (RUNNING or QUEUED, the
        same liveness definition as _in_flight_agent_ids), read straight from the
        durable run store. ``tick_seq`` — the monotonic scheduler-tick counter;
        the dashboard flashes the button each time it advances (a real loop
        check). ``epoch`` — the change-epoch (#150), bumped on every agent-visible
        state mutation (run create/update/halt, scheduler tick, ack markers); a
        caller can poll just this field to ask "did anything change?" cheaply.
        NO gateway calls — safe to poll every few seconds, unlike loop_status()."""
        active = any(
            r.get("status") in (RUNNING, QUEUED) for r in self._list_runs()
        )
        return {
            "active": active,
            "tick_seq": self._scheduler_tick_seq(),
            "epoch": self._change_epoch(),
        }
