"""
trident_agent_snapshot.py — consolidated agent-panel view-model assembler
(issue #149, parent #147).

What this is
------------
A read-only module that gathers the WHOLE agent-panel view-model in one shot,
collapsing the six independent status reads (runs, pill counts, pulse,
loop-health, watch-gate, supervised) into a single consolidated object.

This is the sibling of ``StatusAggregator`` (#141), same construction style
(stores + gateway + derivers injected through providers, no writes, no
side-effects). It wraps ``StatusAggregator`` for the three status surfaces it
already owns, and adds the three remaining sections (runs with log-stripping,
watch-gate, supervised) with per-section fail-soft.

Why a separate module
---------------------
- StatusAggregator already handles pill_counts / loop_status / pulse_status.
  Duplicating its gathering logic here would introduce maintenance drift.
- The three additional sections (runs, watch-gate, supervised) are cheap local
  reads the manager already exposes; composing them into one snapshot with
  per-section isolation makes the frontend replace 6 HTTP round-trips + 5 TCP
  RPC calls with 1 of each.

Per-section fail-soft
---------------------
A gather error in any one section degrades only that section to its established
baseline (empty list for runs, zeros for pill counts, pulse defaults,
loop-health degraded, empty dicts for watch-gate/supervised). The snapshot
ALWAYS returns — it never raises out of the whole gather.

The ``activity[].log`` strip
----------------------------
Run records carry an optional ``activity`` list (stage events for transcript-less
land runs). Each entry may carry a ``log`` field holding full command output
(git/npm/pytest stdout — can be large). The snapshot strips ``log`` from every
activity entry; full logs remain reachable via the existing per-run detail path
(``GET /api/agents/runs/{id}/feed``).

Run:  python -m pytest tests/test_agent_snapshot.py -q
"""

from __future__ import annotations

import copy
import logging
from typing import Callable

_LOG = logging.getLogger("agent_manager")


class AgentStateSnapshot:
    """Read-only assembler that gathers the whole agent-panel view-model in one shot.

    Parameters
    ----------
    status_aggregator
        The ``StatusAggregator`` instance — owns pill_counts / loop_status /
        pulse_status. Injected rather than re-implemented.
    list_runs
        ``() -> list[dict]`` — the manager's lock-held run lister
        (``_list_runs``). Each run record dict is deep-copied and its
        ``activity[].log`` field stripped in the returned snapshot.
    watch_gate_status
        ``() -> dict`` — returns ``{enabled, env_override, effective}``.
    supervised_status
        ``() -> dict`` — returns ``{enabled, effective}``.
    logger
        Where the fail-soft warnings go (defaults to the ``agent_manager`` log).
    """

    # Baseline fallbacks per section — what a degraded section returns.
    _BASELINE_PILLS = {"green": 0, "yellow": 0, "purple": 0, "red": 0}
    _BASELINE_PULSE = {"active": False, "tick_seq": 0, "epoch": 0}
    _BASELINE_STALENESS = {"loaded_sha": None, "repo_head": None, "stale": False}
    _BASELINE_LOOP = {
        "armed": False,
        "frozen": False,
        "frozen_reason": None,
        "in_flight": 0,
        "open_prs": 0,
        "pr_cap": 1,
        "stage_running": {"claim": 0, "review": 0, "land": 0},
        "claims_withheld": True,
        "withhold_reason": "snapshot degraded — retry",
        "withhold_kind": None,
        "blockers": [],
        "gh_ok": False,
    }

    def __init__(
        self,
        *,
        status_aggregator,
        list_runs: Callable[[], list[dict]],
        watch_gate_status: Callable[[], dict],
        supervised_status: Callable[[], dict],
        staleness_status: Callable[[], dict] | None = None,
        logger: "logging.Logger | None" = None,
    ) -> None:
        self._status = status_aggregator
        self._list_runs = list_runs
        self._watch_gate_status = watch_gate_status
        self._supervised_status = supervised_status
        self._staleness_status = staleness_status
        self._log = logger or _LOG

    # ── public API ───────────────────────────────────────────────────────────

    # The two halves of the snapshot, split by COST so the live push can ship
    # the cheap half first (#151 perf):
    #   * CHEAP — in-memory store/local reads only (runs, pulse, watch-gate,
    #     supervised). Microseconds. ``pulse`` carries ``epoch``, which the WS
    #     handler needs to re-sync its change-epoch, so it rides the cheap half.
    #   * GH — the GitHub-derived half (pill_counts + loop_health). ~8 `gh`
    #     round-trips, ~3-5s cold. This is the slow path the panel used to wait
    #     on before ANYTHING rendered.
    _CHEAP_SECTIONS = ("runs", "pulse", "watch_gate", "supervised", "staleness")
    _GH_SECTIONS = ("pill_counts", "loop_health")

    def snapshot(self, sections: str = "full") -> dict:
        """The consolidated agent-panel view-model.

        ``sections`` selects which half to gather:
          * ``"full"`` (default) — all six keys. Used by the HTTP route and any
            caller that wants one complete object.
          * ``"cheap"`` — the in-memory half only (``runs``, ``pulse``,
            ``watch_gate``, ``supervised``). No ``gh`` calls, so it returns in
            microseconds — the live push sends this FIRST so the agent cards
            render instantly instead of waiting behind the GitHub reads.
          * ``"gh"`` — the GitHub-derived half only (``pill_counts``,
            ``loop_health``). The ~8-call slow path; the live push sends it a
            beat after the cheap half to fill in the pill badges + loop banner.

        Each section is gathered independently with its own fail-soft wrapper —
        one section's error never prevents the rest from populating. The cheap
        and gh halves are disjoint, so a ``cheap`` frame merged with a ``gh``
        frame on the client equals a ``full`` snapshot (no duplicated keys).
        """
        gatherers = {
            "runs": self._gather_runs,
            "pill_counts": self._gather_pill_counts,
            "pulse": self._gather_pulse,
            "loop_health": self._gather_loop_health,
            "watch_gate": self._gather_watch_gate,
            "supervised": self._gather_supervised,
            "staleness": self._gather_staleness,
        }
        if sections == "cheap":
            keys = self._CHEAP_SECTIONS
        elif sections == "gh":
            keys = self._GH_SECTIONS
        else:  # "full" (and any unknown value — fail safe to the complete view)
            keys = self._CHEAP_SECTIONS + self._GH_SECTIONS
        return {k: gatherers[k]() for k in keys}

    # ── per-section gatherers ────────────────────────────────────────────────

    def _gather_runs(self) -> list[dict]:
        """All run records (newest first) with ``activity[].log`` stripped."""
        try:
            runs = self._list_runs()
        except Exception:
            self._log.warning("snapshot: runs gather failed — returning []", exc_info=True)
            return []
        # Strip activity[].log — deep-copy so the original store records are
        # untouched. The strip is tolerant of any dict shape (missing keys,
        # non-list activity, etc.).
        return [_strip_activity_log(r) for r in runs]

    def _gather_pill_counts(self) -> dict:
        """The four-pill HITL counts, or the safe zero baseline on error."""
        try:
            return self._status.pill_counts()
        except Exception:
            self._log.warning("snapshot: pill_counts gather failed", exc_info=True)
            return dict(self._BASELINE_PILLS)

    def _gather_pulse(self) -> dict:
        """The cheap AGENTS-tab glow signal, or the idle baseline on error."""
        try:
            return self._status.pulse_status()
        except Exception:
            self._log.warning("snapshot: pulse gather failed", exc_info=True)
            return dict(self._BASELINE_PULSE)

    def _gather_loop_health(self) -> dict:
        """The loop-health snapshot, or the degraded baseline on error."""
        try:
            return self._status.loop_status()
        except Exception:
            self._log.warning("snapshot: loop_health gather failed", exc_info=True)
            return dict(self._BASELINE_LOOP)

    def _gather_watch_gate(self) -> dict:
        """The master watch-gate arm state, or an empty dict on error."""
        try:
            return self._watch_gate_status()
        except Exception:
            self._log.warning("snapshot: watch_gate gather failed", exc_info=True)
            return {}

    def _gather_supervised(self) -> dict:
        """The per-project supervised-mode state, or an empty dict on error."""
        try:
            return self._supervised_status()
        except Exception:
            self._log.warning("snapshot: supervised gather failed", exc_info=True)
            return {}

    def _gather_staleness(self) -> dict:
        """The manager staleness snapshot (loaded SHA vs repo HEAD, #182),
        or the safe not-stale baseline on error."""
        try:
            if self._staleness_status is not None:
                return self._staleness_status()
            return dict(self._BASELINE_STALENESS)
        except Exception:
            self._log.warning("snapshot: staleness gather failed", exc_info=True)
            return dict(self._BASELINE_STALENESS)


# ── helpers ──────────────────────────────────────────────────────────────────


def _strip_activity_log(rec: dict) -> dict:
    """Return a shallow copy of *rec* with ``activity[].log`` stripped from every
    entry. Tolerant of any dict shape — a missing, empty, or non-list ``activity``
    key returns the record unchanged (but still copied to prevent mutation of the
    store's original)."""
    r = copy.deepcopy(rec)
    activity = r.get("activity")
    if isinstance(activity, list):
        for entry in activity:
            if isinstance(entry, dict) and "log" in entry:
                del entry["log"]
    return r
