"""
trident_agent_token_guardrail.py — the per-session token guardrail, lifted out of
``AgentManagerServer`` as a verdict-returning decider (issue #142, parent #138).

What this is
------------
The runaway-agent token guardrail (#73) split into the same DECIDE / APPLY shape
as ``trident_agent_guards.py``: a pure-ish decider that *classifies* a finished
run's consumed-token total and *builds* the over-ceiling :class:`HaltReport`, with
NO gh write and NO run-store transition of its own. The caller (the manager's
run-finish path + the stage recorders) takes the verdict and applies the
side-effects — ``run_store.mark_halted(...)`` (so every run-state transition still
funnels through the run store's ``guard_transition``; HALTED stays sticky, #117)
plus the ``needs-human`` label/comment on the issue.

Why a separate module
---------------------
The pure CLASSIFICATION + ROLLUP cores already live in ``trident_agent_tokens``
(``classify_session`` / ``is_over_ceiling`` / ``rollup_issue``). What stayed welded
into ``AgentManagerServer`` was the *decision wiring* around them: measure a run's
total via the injected counter, decide ok / warn / over, and — when over — assemble
the exact ``HaltReport`` + ``needs-human`` comment the manager filed inline. This
module is that decision layer, lifted out so the manager composes it and its
recorders delegate to it.

The verdict-returning convention (#142)
--------------------------------------
:meth:`TokenGuardrail.evaluate` returns a :class:`TokenVerdict` carrying the
measured token total, the classification, and — for an over-ceiling run — the
``HaltReport`` to mark the run halted with and the one-line ``needs-human``
comment to file. It performs NO gh write and NO store transition: the point of the
verdict shape is that the classification + report can be unit-tested with NO fake
gateway and NO run store (see tests/test_agent_token_guardrail.py). The manager's
``_apply_token_halt`` is the APPLY half — it alone touches ``mark_halted`` + the
gateway.

POST-HOC, not a budget cap (#121, L7)
-------------------------------------
Inherited verbatim from ``trident_agent_tokens``: ``evaluate`` runs at run-COMPLETE,
after the session is already terminal. An "over" verdict LABELS a runaway corpse;
it never stopped the overspend mid-flight. The only mid-flight bound is the per-run
wall-clock deadline (#119), not a token budget.

Run:  python -m pytest tests/test_agent_token_guardrail.py -q
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from agents.halt import HaltReport
from agents.tokens import (
    OVER,
    WARN,
    classify_session,
    rollup_issue,
)

_LOG = logging.getLogger("agent_manager")

# Display string for the per-session token ceiling in halt reports / comments
# (the numeric thresholds live in trident_agent_tokens — SESSION_CEILING).
SESSION_CEILING_DISPLAY = "300k"

# A watch run's agent_id is "watch-issue-<n>" (_RunStoreRecorder). This recovers
# the <n> so a token halt on a watch run can be filed back on its issue.
_WATCH_AGENT_RE = re.compile(r"^watch-issue-(\d+)$")


def _issue_from_watch_agent_id(agent_id: str) -> int | None:
    """The issue number a watch run's ``agent_id`` ("watch-issue-<n>") encodes, or
    None for a non-watch (clock/manual) run id. Lets a token halt on a watch run
    file its report back on the right issue."""
    m = _WATCH_AGENT_RE.match(agent_id or "")
    return int(m.group(1)) if m else None


# The chain-order bot label for an issue's per-session breakdown. The run's
# outcome text names the producing stage (Gordon opens a PR, Eli lands it, …); we
# read a coarse label off it for the "Gordon 121k + Eli 24k" line, defaulting to a
# generic "session" when the outcome is uninformative.
def _chain_stage_label(run: dict) -> str:
    outcome = str(run.get("outcome", "") or "").lower()
    if "land" in outcome or "merged" in outcome or "main" in outcome:
        return "Eli"
    if "review" in outcome or "recommend" in outcome:
        return "Kleiner"
    if "pr" in outcome or "issue→pr" in outcome or "draft" in outcome:
        return "Gordon"
    return "session"


@dataclass(frozen=True)
class TokenVerdict:
    """The result of guarding one finished run's token total. PURE data.

    Frozen so a built verdict can't be mutated out from under whoever applies it.

    Attributes
    ----------
    run_id:
        The run this verdict is about.
    tokens:
        The measured ``output_tokens`` total, or None when the run was UNMEASURED
        (the counter could not locate / isolate its transcript). ``measured`` is the
        named predicate for "tokens is not None".
    classification:
        The :func:`classify_session` bucket — ``"ok"`` / ``"warn"`` / ``"over"`` —
        or None when unmeasured. ``"warn"`` is the accepted-ceiling band (200k–300k),
        logged but NOT filed; ``"over"`` (>300k) is the runaway halt. ``over_ceiling``
        is the named predicate the apply path branches on.
    issue_number:
        The issue to file the ``needs-human`` report on for an over-ceiling run, or
        None when the run can't be tied to one (a clock/manual run) — the apply path
        marks it halted but files nothing on GitHub. Distinct from
        ``halt_report.issue_number`` (which is 0, never None, for the no-issue case so
        the report text reads identically to before).
    halt_report:
        The :class:`HaltReport` to ``mark_halted`` the run with, set ONLY for an
        over-ceiling verdict (else None).
    needs_human_comment:
        The one-line provenance comment the apply path leaves on the issue for an
        over-ceiling run (else None). Pre-built here so the apply half is pure
        plumbing and the wording stays in one place.
    """

    run_id: str
    tokens: int | None
    classification: str | None
    issue_number: int | None = None
    halt_report: HaltReport | None = None
    needs_human_comment: str | None = None

    @property
    def measured(self) -> bool:
        """True iff a token total was measured (the counter found the transcript)."""
        return self.tokens is not None

    @property
    def over_ceiling(self) -> bool:
        """True iff the session blew past the 300k ceiling (classifies "over")."""
        return self.classification == OVER


class TokenGuardrail:
    """Decide a finished run's per-session token guardrail (#73), verdict-returning.

    Constructed with the run-store lister + the token counter (both injected as the
    manager's live-resolving seams), it exposes :meth:`evaluate` — the pure decision
    — plus the per-issue / per-PRD chain rollups the recorders delegate to. It
    performs NO gh write and NO store transition: the manager applies the verdict's
    side-effects through the run store + gateway.

    Parameters
    ----------
    list_runs
        ``() -> list[dict]`` — the manager's lock-held run lister (``_list_runs``),
        used by the chain rollups.
    token_counter_provider
        ``() -> (run_id -> int | None)`` — returns the manager's CURRENT session
        token counter. A PROVIDER (not the counter itself) so a test reassigning
        ``mgr._session_token_counter`` after construction is still honored — same
        indirection idiom as ``StatusAggregator``'s gateway-factory provider. The
        counter returns None for an UNMEASURED run (transcript not locatable /
        isolable), which surfaces as an unmeasured verdict.
    logger
        Where the counter-raised warning goes (defaults to the ``agent_manager`` log).
    """

    def __init__(
        self,
        *,
        list_runs: Callable[[], list[dict]],
        token_counter_provider: Callable[[], Callable[[str], "int | None"]],
        logger: "logging.Logger | None" = None,
    ) -> None:
        self._list_runs = list_runs
        self._token_counter_provider = token_counter_provider
        self._log = logger or _LOG

    # ── the verdict (the DECISION half) ─────────────────────────────────────────

    def evaluate(self, run: dict, *, issue_number: int | None = None) -> TokenVerdict:
        """Classify a finished ``run``'s consumed-token total. PURE-ish: measures via
        the injected counter, then DECIDES — no gh write, no store transition.

        Steps:
          1. Measure via the injected counter (None ⇒ an UNMEASURED verdict; an
             unmeasurable transcript must NEVER block a completion or raise).
          2. Classify ok / warn / over against the 200k / 300k thresholds.
          3. For an OVER-ceiling run, assemble the :class:`HaltReport` + the
             ``needs-human`` comment and resolve which issue to file on (the explicit
             ``issue_number``, else the one the ``watch-issue-<n>`` agent_id encodes,
             else None ⇒ a clock/manual run the apply path halts but files nothing
             for).

        The caller applies the verdict: stamp ``tokens`` on the record, log a "warn",
        and on ``over_ceiling`` run ``mark_halted`` + the ``needs-human`` label/comment.
        """
        run = run or {}
        run_id = str(run.get("run_id") or "")

        try:
            tokens = self._token_counter_provider()(run_id)
        except Exception:
            self._log.exception("token counter raised for run %s", run_id)
            return TokenVerdict(run_id=run_id, tokens=None, classification=None)
        if tokens is None:
            return TokenVerdict(run_id=run_id, tokens=None, classification=None)

        tokens = int(tokens)
        bucket = classify_session(tokens)
        if bucket != OVER:
            return TokenVerdict(run_id=run_id, tokens=tokens, classification=bucket)

        # Over the ceiling — assemble the halt report + the needs-human comment as
        # PURE data. The wording matches the manager's old inline _raise_token_halt
        # verbatim so the filed report / comment read identically to before.
        agent_id = str(run.get("agent_id", "") or "")
        issue_no = (
            issue_number if issue_number is not None
            else _issue_from_watch_agent_id(agent_id)
        )
        agent_label = agent_id or run_id
        reason = (
            f"session consumed {tokens:,} tokens — over the {SESSION_CEILING_DISPLAY} "
            f"per-session ceiling (runaway-agent guardrail)"
        )
        report = HaltReport(
            agent_name=agent_label,
            issue_number=issue_no if issue_no is not None else 0,
            stage="token-guardrail",
            reason=reason,
            detail=(
                f"Run {run_id} finished but burned {tokens:,} tokens against the "
                f"Claude subscription — past the 300k per-session ceiling. This is "
                f"the runaway-session failure mode (e.g. an agent re-ingesting its "
                f"own transcript). Outcome on record: {run.get('outcome') or 'n/a'}."
            ),
        )
        comment = (
            f"halted on token guardrail — session used {tokens:,} tokens "
            f"(over the 300k ceiling); filed for your review (needs your call)"
        )
        return TokenVerdict(
            run_id=run_id,
            tokens=tokens,
            classification=bucket,
            issue_number=issue_no,
            halt_report=report,
            needs_human_comment=comment,
        )

    # ── per-issue / per-PRD chain rollups (#73) ─────────────────────────────────

    def issue_chain_tokens(self, issue_number: int) -> list[tuple[str, int]]:
        """The per-session ``(agent_label, tokens)`` breakdown for one issue's
        chain, read from the run store (#73 per-issue rollup).

        Every loop stage records its run with ``agent_id`` == ``watch-issue-<n>``
        (``_RunStoreRecorder``), so all of an issue's sessions share that key. We
        gather every run for the issue that carries a measured ``tokens`` total,
        OLDEST-first (chain order), labelled by the bot that produced it (from the
        run's outcome / a generic stage name). Unmeasured runs (tokens is None)
        are omitted — the breakdown reports only what was actually measured.

        Pure-ish: reads the run store only, no gh. The caller formats it via
        ``trident_agent_tokens.format_issue_breakdown``."""
        watch_key = f"watch-issue-{issue_number}"
        runs = [
            r for r in self._list_runs()
            if str(r.get("agent_id", "")) == watch_key and r.get("tokens") is not None
        ]
        runs.sort(key=lambda r: r.get("created_at") or 0)  # oldest-first = chain order
        return [(_chain_stage_label(r), int(r["tokens"])) for r in runs]

    def issue_chain_total(self, issue_number: int) -> int:
        """The summed consumed-token total for one issue's chain (#73). Convenience
        over :meth:`issue_chain_tokens` for the per-PRD rollup, which needs each
        child issue's single total."""
        return rollup_issue([t for _, t in self.issue_chain_tokens(issue_number)])
