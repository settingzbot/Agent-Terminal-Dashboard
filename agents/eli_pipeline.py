"""
trident_eli_pipeline.py — the merge-and-land integration agent (Slice 2a of #60):
**Dr. Eli Vance**, the architect/integrator.

What this is
------------
Kleiner (trident_review_pipeline) advances every reviewed-and-approved PR's issue
to ``approved`` — its terminal state and Eli's inbox. Eli is the LAST stage of
the human-in-the-loop assembly line and the ONE privileged agent allowed to push
the protected ``main`` branch. It takes a single INTEGRATION-READY issue (one in
the ``approved`` state) and its approved PR/branch and LANDS it.

The flow (``EliPipeline.process``)
----------------------------------
1. Pick the OLDEST ``approved`` issue (oldest = smallest createdAt, like Gordon's
   and Kleiner's selectors). No issue ⇒ no-op (acted=False).
2. Find the open PR that ``Closes #N`` via the gateway, and its head branch. No
   PR ⇒ HALT (Slice F): an item Eli can't land genuinely needs a human, so flip
   approved → needs-human, post a 🤖 halt comment, mark the run ``halted``.
3. **MERGE** the approved branch onto ``main`` (the injected ``merger`` seam — a
   local-git merge; in tests a fake, never real git). A non-trivial merge
   CONFLICT short-circuits to a 🔴 halt — ``main`` is left untouched.
4. **REBUILD the dashboard bundle ONCE** at integration (the injected
   ``bundle_builder`` seam). This is deliberately a SINGLE rebuild at land time,
   NOT per-PR: every agent PR is source-only (``dashboard_v2/`` is guarded out by
   #51), so the compiled bundle is regenerated exactly once here, on the combined
   tree, avoiding the pairwise ``dashboard-<hash>.js`` conflicts that made the
   #18–#21 batch unmergeable.
5. Run the **FULL test suite** on the combined tree (the injected
   ``full_suite_runner`` seam). This is DISTINCT from
   ``trident_agent_pipeline.run_pytest_in_worktree``, which is SCOPED to a run's
   changed files — Eli runs EVERYTHING, because a merge can break a module no
   single PR touched.
6. **PUSH ``main``** — ONLY if the full suite is green (the injected
   ``main_pusher`` seam, the one place the push-to-main guard is dropped).
7. **FINALIZE** (Slice 2b, #61) — once ``main`` is safely pushed, flip the issue
   ``approved → merged/done``, CLOSE it (reason completed), and leave a 🤖 audit
   comment naming the landed PR. Then detect PRD COMPLETION: if the just-closed
   issue was the LAST open in-scope child of its parent umbrella (e.g. #58),
   close the parent PRD too with a 🤖 summary. The run is recorded SUCCEEDED
   exactly once, AFTER finalize. Finalize is BEST-EFFORT: ``main`` is already
   landed before it runs, so a close/comment hiccup logs a warning and STILL
   returns the SUCCEEDED result — it never halts or unwinds (contrast: a failure
   BEFORE the push still halts).

Green-or-halt contract
----------------------
  * GREEN combined suite ⇒ branch merged onto ``main``, bundle rebuilt once,
    ``main`` pushed, run SUCCEEDED.
  * RED suite, a non-trivial merge CONFLICT, or any HARD ERROR in any seam ⇒ do
    NOT push; ``main`` is left untouched; raise a 🔴 item-pill record with a
    concise report (``_halt`` — mirrors trident_agent_halt's ``HaltReport`` +
    ``recorder.halt(...)``, the same pattern the review pipeline's ``_reject``
    uses). Fail-safe: any unexpected exception lands here, so ``main`` is never
    pushed from a half-done state.

Guard posture
-------------
Eli is the ONE agent that runs WITHOUT the push-to-``main`` guard but KEEPS the
secret-read guard. ``eli_enforce_command`` is a category-filtered sibling of
``trident_agent_run.enforce_run_command``: it runs the SAME pure predicate
(``trident_agent_guards.check_command``) but only enforces the ``secret_read``
category — a ``push_main`` verdict is PERMITTED for Eli (whereas Gordon's posture
blocks it). It does NOT rewrite ``check_command``; it filters which categories
raise.

Pure cores / seams
------------------
Every side-effecting seam is INJECTED into ``EliPipeline`` so the whole flow is
driven by fakes in tests — no real gh, no real git/merge/push, no real bundle
build, no real pytest. Mirrors trident_review_pipeline.ReviewPipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from agents import guards
from agents.run import ForbiddenCommand
from agents.halt import HaltReport, format_halt_comment, format_halt_report
from agents.pipeline import PipelineResult
from agents.eli_architect import (
    ArchAssessment,
    ArchDecision,
    CloseObsolete,
    EditIssue,
    Freeze,
    OpenIssue,
    ReorderDeps,
    ReplanPlan,
    decide_replan,
    replace_blocked_by_section,
)
from shared.loop_gate import LoopGateLike
from agents.trade_surface import (
    IntegrationDecision,
    classify_paths,
    decide_integration,
)
from shared.github_gateway import (
    Issue,
    APPROVED_LABEL,
    NEEDS_HUMAN_LABEL,
    DONE_LABEL,
    READY_LABEL,
)

_LOG = logging.getLogger(__name__)

# The agent identity Eli stamps on provenance comments (same convention as Gordon
# "Gordon Freeman" and Kleiner "Dr. Isaac Kleiner" — the shared settingzbot gh
# account can't otherwise attribute which agent landed the work).
BOT_NAME = "Dr. Eli Vance"


# ═══════════════════════════════════════════════════════════════════════════════
# Guard posture — drop the push-main guard, KEEP the secret-read guard
# ═══════════════════════════════════════════════════════════════════════════════

def eli_enforce_command(command: str) -> str:
    """Raise :class:`ForbiddenCommand` if ``command`` is forbidden on ANY axis
    EXCEPT push-main, else return it unchanged — even when it pushes/merges to
    ``main``.

    Eli is the ONE agent allowed to land on the protected branch, so its posture
    DROPS the ``push_main`` axis but KEEPS every other axis — critically the
    ``secret_read`` axis (the live Lighter key must never reach a transcript,
    integrator or not). This is a category-FILTER over the existing pure predicate
    — it does NOT reimplement the policy.

    It MUST use :func:`trident_agent_guards.blocked_categories` (ALL offending
    categories across ALL segments), NOT ``check_command`` (which returns only the
    FIRST blocked verdict and stops). A compound like
    ``git push origin main && keyring get x`` has BOTH a push-main and a
    secret-read offence; ``check_command`` returns the push-main one first, so a
    naive "permit if category == push_main" filter would let the secret read
    through. By computing ``blocked_categories - {push_main}`` we block whenever
    ANY non-push-main offence is present anywhere in the command, so the
    secret-read backstop is airtight — and the secret axis has no pre-push-hook
    backstop the way the push axis does, so this is the only line of defence.
    Composable: a future guarded category is blocked for Eli by default; only
    push-main is explicitly allowed.
    """
    offending = guards.blocked_categories(command) - {
        guards.CATEGORY_PUSH_MAIN
    }
    if offending:
        # Re-derive a representative verdict for the offending axis so the raised
        # error reports the axis Eli actually blocked on (e.g. the secret read),
        # not the push-main verdict check_command would surface first.
        verdict = _representative_verdict(command, offending)
        _LOG.warning("Eli command blocked (%s): %s", verdict.category, verdict.reason)
        raise ForbiddenCommand(verdict)
    return command


def _representative_verdict(command: str, offending: set[str]):
    """A blocked :class:`~trident_agent_guards.Verdict` whose category is in
    ``offending`` — so the raised ``ForbiddenCommand`` reports the axis Eli
    actually blocked on (e.g. the secret read), not the push-main verdict
    ``check_command`` would surface first.

    Walks each segment (checking secret-read FIRST so it wins over a co-located
    push-main) and returns the first blocked verdict whose category is in
    ``offending``; falls back to a synthetic verdict if none is recovered (should
    not happen — ``offending`` was derived from these same segments)."""
    from agents.guards import (
        _segments, _check_push_main, _check_secret_read, Verdict,
    )
    for segment in _segments(command):
        for check in (_check_secret_read, _check_push_main):
            v = check(segment)
            if v is not None and v.category in offending:
                return v
    cat = next(iter(offending))
    return Verdict(blocked=True, reason=f"command blocked on the {cat} axis", category=cat)


# ═══════════════════════════════════════════════════════════════════════════════
# Seam result types (pure data — table-testable)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MergeOutcome:
    """The result of merging the approved branch onto ``main``.

    ``merged`` True ⇒ a clean merge (Eli proceeds to the bundle build + suite).
    ``merged`` False ⇒ a non-trivial conflict; ``conflict`` carries a concise,
    plain-English description for the halt report. ``main`` is left untouched in
    the conflict case (the merger seam aborts the merge before returning).

    ``auto_resolved_docs`` lists any wiki files (``docs/claude/**``) the merger
    auto-union-merged to land a conflict that touched ONLY documentation — see
    ``trident_eli_runtime.merge_branch_onto_main`` step 3. Empty on a clean merge.
    Surfaced in the land's stage note + finalize comment so the auto-resolve is
    auditable, never silent; a conflict touching any code/config/test file does
    NOT auto-resolve (``merged`` stays False and Eli halts).

    ``output`` is the raw ``git merge`` terminal output (stdout+stderr) — the full
    command log the land feed surfaces under the merge stage so a finished land can
    be inspected (not just the one-line summary). Empty in tests / on a halt."""

    merged: bool
    conflict: str = ""
    auto_resolved_docs: tuple[str, ...] = ()
    output: str = ""


@dataclass(frozen=True)
class SuiteOutcome:
    """The result of the FULL test suite on the combined (merged) tree.

    ``passed`` is the only thing the push gate branches on; ``summary`` is a short
    one-liner (e.g. "120 passed" / "3 failed") embedded in the halt report when
    red. ``output`` is the FULL pytest terminal output (stdout+stderr) — the land
    feed surfaces it under the test stage so a finished land shows the whole run,
    not just the summary line (and the failing-test detail when red). Distinct from
    trident_agent_pipeline.TestOutcome, which is the SCOPED grader's result — this
    is the whole-suite result.

    Frontend check fields (added #183): when the merged tree touches ``web/``, the
    full suite also runs ``tsc -b`` as a frontend typecheck gate (#210). The
    pipeline emits a separate ``tsc`` stage event from these fields so the land feed
    shows a named pass/fail — not buried in the bundle rebuild log."""

    # Not a pytest test class despite the name (pytest would otherwise try to
    # collect it because it starts with "Suite"... it doesn't, but mirror the
    # TestOutcome guard for safety against future renames).
    __test__ = False

    passed: bool
    summary: str = ""
    output: str = ""
    # #183: frontend typecheck gate (tsc -b). False ⇒ not checked (no web/ changes).
    frontend_checked: bool = False
    frontend_passed: bool = True
    frontend_summary: str = ""
    frontend_output: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# Side-effecting seam protocol (injected — fakeable in tests)
# ═══════════════════════════════════════════════════════════════════════════════

class EliGatewayLike(Protocol):
    def list_approved_issues(self) -> list[Issue]: ...
    def find_open_pr_for_issue(self, issue_number: int) -> Optional[int]: ...
    def get_pr_branch(self, pr_number: int) -> Optional[str]: ...
    # #63 trading-surface wall: the changed paths Eli classifies BEFORE merging.
    def get_pr_files(self, pr_number: int) -> list[str]: ...
    def leave_bot_comment(self, number: int, bot_name: str, sentence: str) -> None: ...
    def set_labels(self, number: int, add=None, remove=None) -> None: ...
    # Slice 2b (#61) finalize seams:
    #   list_open_issues — ALL open issues, so PRD-completion is computed over the
    #                      OTHER open children of the just-closed issue's parent.
    #   close_issue      — `gh issue close --reason completed` (closes the landed
    #                      issue, and a parent PRD when its last child is merged).
    def list_open_issues(self) -> list[Issue]: ...
    def close_issue(self, number: int, *, reason: str = "completed") -> None: ...


# merger:            (*, branch) -> MergeOutcome   (clean / conflict; never pushes)
# bundle_builder:    () -> str|None                (the ONE rebuild; returns its output)
# full_suite_runner: () -> SuiteOutcome            (the WHOLE suite on the merged tree)
# main_pusher:       () -> str|None                (`git push origin main`; green only)
#
# bundle_builder / main_pusher return the command's full terminal output (a test
# fake may still return None) — Eli stamps it onto the stage feed so a finished
# land can be inspected. A non-string return (None) is treated as no output.


# ═══════════════════════════════════════════════════════════════════════════════
# Optional run recorder (same shape Gordon / Kleiner use; no-op by default)
# ═══════════════════════════════════════════════════════════════════════════════

class EliRunRecorder(Protocol):
    def start(self, issue: Issue) -> None: ...
    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url: Optional[str], note: str = "") -> None: ...
    def halt(self, issue_number: int, *, report: str) -> None: ...


# ── token-rollup seam (issue #73): inject the run-store-backed cost rollup ───────

class TokenRollup(Protocol):
    """The full-chain token accounting Eli stamps into its close comments (#73).

    INJECTED so the rollup is computed by whoever can see ALL of an issue's
    measured sessions — the MANAGER (it owns the run store, and it stamps every
    session's total at run-complete, INCLUDING Eli's own once Eli's run finishes).
    Eli itself can't measure its own still-running session, so it never sums; it
    only ASKS for the already-measured breakdown and posts it. Both methods return
    None when nothing is measured yet, in which case Eli posts no token line — the
    close/land is never blocked by token accounting.

      * ``issue_breakdown(n)`` → e.g. ``"Gordon 121k + Kleiner 18k + Eli 24k =
        163k"`` for issue #n, or None.
      * ``prd_rollup(prd, children)`` → e.g. ``"PRD #58: ~211k across 2 slices"``,
        or None.
    """

    def issue_breakdown(self, issue_number: int) -> Optional[str]: ...
    def prd_rollup(self, prd_number: int, child_issue_numbers: list[int]) -> Optional[str]: ...


class _NullTokenRollup:
    """No-op rollup (used when Eli runs without a run store — e.g. every existing
    test). Reports nothing measured, so no token line is posted."""

    def issue_breakdown(self, issue_number: int) -> Optional[str]:
        return None

    def prd_rollup(self, prd_number: int, child_issue_numbers: list[int]) -> Optional[str]:
        return None


class _NullRunRecorder:
    """No-op recorder (used when the pipeline runs without a run store)."""

    def start(self, issue: Issue) -> None:
        pass

    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url: Optional[str], note: str = "") -> None:
        pass

    def halt(self, issue_number: int, *, report: str) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers (mirrors the review pipeline's _halt_sentence)
# ═══════════════════════════════════════════════════════════════════════════════

def _halt_sentence(report: HaltReport) -> str:
    """The SENTENCE portion of the halt provenance comment (no 🤖 prefix).

    ``leave_bot_comment`` adds the ``🤖 {bot_name}: `` prefix itself, so passing
    the whole ``format_halt_comment`` string would double the prefix. We strip the
    head so the posted comment ends up byte-identical to
    ``format_halt_comment(report)`` — same shape Gordon and Kleiner use."""
    full = format_halt_comment(report)
    _, _, sentence = full.partition(": ")
    return sentence or full


# ═══════════════════════════════════════════════════════════════════════════════
# Pure core — PRD-completion detection (no I/O; takes the open-issue list as data)
# ═══════════════════════════════════════════════════════════════════════════════

def prd_complete(
    parent_number: int, closed_issue_number: int, open_issues: list[Issue]
) -> bool:
    """True iff the parent PRD ``parent_number`` has NO remaining open in-scope
    child OTHER than ``closed_issue_number``. PURE — no I/O.

    "Child of P" = an OPEN issue whose body names ``Parent #P`` (parsed by the
    gateway into ``Issue.parent_refs``). ``open_issues`` is the live open-issue
    list passed in as data — at the call site the just-closed issue may or may not
    still appear in it (gh's close is async / the list may be stale), so the
    just-closed issue is EXPLICITLY excluded by number. Returns True when every
    remaining child is gone, i.e. the slice that just landed was the last one.

    A parent with NO children at all returns True only if it had exactly the one
    closing child — guarded by the caller (``parents_to_close`` only considers the
    closed issue's OWN parents), so a childless unrelated PRD is never closed."""
    for issue in open_issues:
        if issue.number == closed_issue_number:
            continue
        if parent_number in issue.parent_refs:
            return False
    return True


def parents_to_close(closed_issue: Issue, open_issues: list[Issue]) -> list[int]:
    """The parent PRD numbers to close now that ``closed_issue`` has landed. PURE.

    For each parent the closed issue names (``closed_issue.parent_refs``), the
    parent is returned iff ``prd_complete`` holds — no OTHER open child still names
    it. Order-preserving, de-duplicated (parent_refs is already de-duped). Empty
    list when the closed issue has no parent or a sibling is still open."""
    return [
        p for p in closed_issue.parent_refs
        if prd_complete(p, closed_issue.number, open_issues)
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# EliPipeline — the orchestrator (Eli Vance)
# ═══════════════════════════════════════════════════════════════════════════════

class EliPipeline:
    """Drives one approved → landed merge-and-land cycle. All side-effecting seams
    are injected, mirroring IssuePrPipeline / ReviewPipeline.

    The seams are deliberately distinct callables so production wires real
    behaviour to each and tests fake each independently:

      * ``merger``            — local-git merge of the branch onto ``main`` (never
                                pushes); production routes its commands through
                                ``eli_enforce_command``.
      * ``bundle_builder``    — the single ``npm run deploy:dashboard`` rebuild at
                                integration (not per-PR).
      * ``full_suite_runner`` — the WHOLE pytest suite on the merged tree.
      * ``main_pusher``       — ``git push origin main`` (the one push-main-allowed
                                seam), fired ONLY on a green suite.
    """

    def __init__(
        self,
        *,
        gateway: EliGatewayLike,
        merger: Callable[..., MergeOutcome],
        bundle_builder: Callable[[], Optional[str]],
        full_suite_runner: Callable[[], SuiteOutcome],
        main_pusher: Callable[[], Optional[str]],
        recorder: Optional[EliRunRecorder] = None,
        token_rollup: Optional[TokenRollup] = None,
        bot_name: str = BOT_NAME,
    ) -> None:
        self._gw = gateway
        self._merge = merger
        self._build_bundle = bundle_builder
        self._run_full_suite = full_suite_runner
        self._push_main = main_pusher
        self._recorder = recorder or _NullRunRecorder()
        # #73: the run-store-backed token rollup (injected by the manager). No-op
        # by default, so an Eli run without a run store posts no token line.
        self._tokens = token_rollup or _NullTokenRollup()
        self._bot_name = bot_name

    # ── structured stage log (the transcript-less land feed) ────────────────────

    # The raw command log kept per stage is capped (tail-kept) so a verbose npm /
    # pytest run can't bloat the run record unboundedly. The interesting summary
    # (and any failure) is at the END of these logs, so we keep the tail.
    _STAGE_LOG_CAP = 20000

    def _stage(self, n: int, stage: str, status: str, detail: str = "",
               log: str = "") -> None:
        """Record one stage event on the run via the recorder's OPTIONAL
        ``stage_event`` seam. Eli runs no Claude session, so the run has no
        transcript for the dashboard feed to read; this structured log IS its
        feed (merge → bundle → test → push, or the stage it halted on). ``log`` is
        the stage's FULL terminal output (the actual git/npm/pytest run) — the feed
        renders it as an expandable block so a finished land can be inspected, not
        just the one-line ``detail``. Defensive ``getattr`` — a recorder without
        the method (every existing test fake, and the no-op recorder) is a silent
        no-op, exactly like Gordon/Kleiner's ``attach_session`` call. Best-effort:
        never unwinds the land."""
        emit = getattr(self._recorder, "stage_event", None)
        if not callable(emit):
            return
        log = (log or "")[-self._STAGE_LOG_CAP:]
        try:
            emit(n, stage=stage, status=status, detail=detail, log=log)
        except Exception:
            _LOG.warning("could not record %s stage event on #%d", stage, n)

    # ── the cycle ──────────────────────────────────────────────────────────────

    def process(self) -> PipelineResult:
        """Run ONE merge-and-land cycle. Returns a :class:`PipelineResult`.

        No ``approved`` issue ⇒ no-op (acted=False). Otherwise lands the OLDEST
        one: merge → rebuild bundle once → full suite → push ``main`` (green only).
        A red suite, a non-trivial merge conflict, a missing PR, or any hard error
        HALTS to needs-human (Slice F) with a 🔴 record — ``main`` is never pushed
        from a half-done state — surfacing on the dashboard's red pill.
        """
        issue = self._select_oldest()
        if issue is None:
            _LOG.info("merge poll: no approved issue")
            return PipelineResult(acted=False, reason="no approved issue")

        n = issue.number
        self._recorder.start(issue)
        _LOG.info("landing issue #%d (%s)", n, issue.title)

        # Everything from here is wrapped so ANY unexpected error fail-safes to a
        # halt — main is only ever pushed on the explicit green path below.
        try:
            return self._land(issue)
        except Exception as e:  # fail-safe: never leave main half-pushed silently
            return self._halt(n, stage="land", reason=f"unexpected error landing: {e}")

    def _land(self, issue: Issue) -> PipelineResult:
        n = issue.number

        # 1) Find the PR + its head branch.
        pr = self._gw.find_open_pr_for_issue(n)
        if pr is None:
            return self._halt(n, stage="merge", reason="no open PR closes this issue")

        try:
            branch = self._gw.get_pr_branch(pr)
        except Exception as e:
            return self._halt(n, stage="merge", reason=f"could not resolve PR #{pr} branch: {e}")
        if not branch:
            return self._halt(n, stage="merge", reason=f"PR #{pr} has no head branch")

        # 1b) TRADING-SURFACE WALL (#63). Inspect the diff's paths BEFORE merging.
        #     A money-path touch (execution / bot trade paths / config / secrets)
        #     refuses auto-integration and raises the trade-approval pill; an
        #     indicator-math touch is gated on the baseline-backtest parity check.
        #     The PM clears a wall by applying the clearance label — Eli re-polls
        #     and proceeds on the next pass. This is BEFORE any merge so main is
        #     never touched by a money-path change the PM hasn't cleared.
        try:
            paths = self._gw.get_pr_files(pr)
        except Exception as e:
            return self._halt(
                n, stage="trade-surface",
                reason=f"could not read PR #{pr} changed files: {e}",
            )
        gate = decide_integration(classify_paths(paths), issue.labels)
        if not gate.proceed:
            return self._raise_trade_gate(n, gate)

        # 2) Merge the approved branch onto main (no push yet). A non-trivial
        #    conflict short-circuits — main is left untouched.
        try:
            merge = self._merge(branch=branch)
        except Exception as e:
            return self._halt(n, stage="merge", reason=f"merge errored: {e}")
        if not merge.merged:
            reason = merge.conflict.strip() or "non-trivial merge conflict onto main"
            return self._halt(n, stage="merge", reason=f"merge conflict: {reason}")
        merge_note = f"merged {branch} onto main"
        if merge.auto_resolved_docs:
            merge_note += (
                f" (auto-union-merged {len(merge.auto_resolved_docs)} wiki file(s): "
                f"{', '.join(merge.auto_resolved_docs)})"
            )
        self._stage(n, "merge", "ok", merge_note, log=merge.output)

        # 3) Rebuild the dashboard bundle ONCE at integration (not per-PR).
        try:
            bundle_log = self._build_bundle()
        except Exception as e:
            return self._halt(n, stage="bundle", reason=f"dashboard bundle rebuild failed: {e}")
        self._stage(n, "bundle", "ok", "dashboard bundle rebuilt",
                    log=bundle_log if isinstance(bundle_log, str) else "")

        # 4) Run the FULL suite on the merged tree: pytest + frontend typecheck (#183).
        #
        #   The frontend check (tsc -b) is folded into the full suite runner so a
        #   single gate covers both; the pipeline emits a NAMED ``tsc`` stage event from
        #   the suite's frontend fields so the land feed shows its own pass/fail — not
        #   buried in the bundle rebuild log (#210). The tsc stage fires BEFORE the
        #   halt so a frontend failure still records the named event.
        try:
            suite = self._run_full_suite()
        except Exception as e:
            return self._halt(n, stage="test", reason=f"full test suite errored: {e}")
        if suite.frontend_checked:
            self._stage(n, "tsc", "ok" if suite.frontend_passed else "fail",
                        suite.frontend_summary, log=suite.frontend_output)
        if not suite.passed:
            summary = suite.summary.strip() or "tests failed"
            return self._halt(
                n, stage="test",
                reason=f"full test suite is RED on the merged tree ({summary})",
                log=suite.output,
            )
        self._stage(n, "test", "ok", suite.summary.strip() or "full suite green",
                    log=suite.output)

        # 5) Green ⇒ push main (the one push-main-allowed seam).
        try:
            push_log = self._push_main()
        except Exception as e:
            return self._halt(n, stage="push", reason=f"push to main failed: {e}")
        self._stage(n, "push", "ok", "pushed main",
                    log=push_log if isinstance(push_log, str) else "")

        # Landed — main is safely pushed. FINALIZE (Slice 2b #61) is BEST-EFFORT
        # from here: a close/comment hiccup must NOT halt or unwind, so it's all
        # wrapped and the run is still recorded SUCCEEDED below.
        self._finalize(issue, pr, auto_resolved_docs=merge.auto_resolved_docs)

        # Record SUCCEEDED exactly once, AFTER finalize (the 🟢 green pill).
        self._recorder.finish(n, succeeded=True, pr_url=None)
        _LOG.info("issue #%d landed (PR #%d merged + pushed to main)", n, pr)
        return PipelineResult(acted=True, issue_number=n, failed=False)

    # ── finalize (Slice 2b #61): best-effort; main is already landed ────────────

    def _finalize(self, issue: Issue, pr: int, *, auto_resolved_docs: tuple[str, ...] = ()) -> None:
        """Close the landed issue and detect PRD completion. BEST-EFFORT.

        main is ALREADY pushed before this runs, so every step here is wrapped:
        an API hiccup logs a warning and is swallowed — finalize NEVER halts or
        unwinds (contrast: a failure BEFORE the push halts, per 2a). Steps:

          1. Flip ``approved → merged/done`` (the EXISTING set_labels seam).
          2. Leave a 🤖 audit comment naming the landed PR + that the suite was
             green.
          3. CLOSE the issue (reason completed).
          4. PRD completion: if the just-closed issue was the LAST open in-scope
             child of any parent it names, close that parent PRD too (reason
             completed) with a 🤖 summary. The parent is a PRD, not a slice, so it
             gets NO ``merged/done`` label — just closed + a summary comment.
        """
        n = issue.number

        # 1) approved → merged/done.
        try:
            self._gw.set_labels(n, remove=[APPROVED_LABEL], add=[DONE_LABEL])
        except Exception:
            _LOG.warning("could not flip #%d to merged/done", n)

        # 2) 🤖 audit comment naming the landed PR — plus the #73 per-issue token
        #    breakdown when the manager's rollup has it (the close note carries the
        #    chain cost, e.g. "Gordon 121k + Kleiner 18k + Eli 24k = 163k"). The
        #    rollup is BEST-EFFORT and may be None (nothing measured yet) — main is
        #    already landed, so a missing breakdown never blocks the close.
        sentence = (
            f"merged PR #{pr} onto main and pushed (full suite green) — "
            f"landed and closed (merged/done)"
        )
        if auto_resolved_docs:
            sentence += (
                f". Auto-union-merged {len(auto_resolved_docs)} wiki file(s) that "
                f"conflicted on additive notes ({', '.join(auto_resolved_docs)}) — "
                f"no code conflict, so landed without a human"
            )
        try:
            breakdown = self._tokens.issue_breakdown(n)
        except Exception:
            _LOG.warning("token breakdown lookup failed for #%d", n)
            breakdown = None
        if breakdown:
            sentence += f". Token cost: {breakdown}"
        try:
            self._gw.leave_bot_comment(n, self._bot_name, sentence)
        except Exception:
            _LOG.warning("could not leave audit comment on #%d", n)

        # 3) Close the landed issue.
        try:
            self._gw.close_issue(n, reason="completed")
        except Exception:
            _LOG.warning("could not close #%d", n)

        # 4) PRD-completion detection — close any parent whose last child just landed.
        try:
            self._close_completed_parents(issue)
        except Exception:
            _LOG.warning("PRD-completion check failed for #%d", n)

    def _close_completed_parents(self, issue: Issue) -> None:
        """Close each parent PRD of ``issue`` whose last open in-scope child just
        landed (``parents_to_close`` over the live open-issue list). Best-effort
        per-parent. The parent gets a 🤖 summary + close (reason completed) but NO
        ``merged/done`` label — it's a PRD, not a slice."""
        if not issue.parent_refs:
            return
        open_issues = self._gw.list_open_issues()
        for parent in parents_to_close(issue, open_issues):
            # #73 per-PRD rollup: sum all child slice totals into the PRD close
            # note ("PRD #58: ~211k across N slices"). The just-landed child is
            # the only one we can name from here at close time (its siblings are
            # already closed and out of the open-issue list), so we seed the
            # rollup with it; the manager's run-store-backed seam discovers the
            # full child set from the run records keyed to the PRD. BEST-EFFORT
            # and may be None — main is already landed.
            rollup = None
            try:
                rollup = self._tokens.prd_rollup(parent, [issue.number])
            except Exception:
                _LOG.warning("PRD token rollup lookup failed for #%d", parent)
            summary = (
                f"all in-scope child slices landed (last was #{issue.number}) — "
                f"PRD complete, closing"
            )
            if rollup:
                summary += f". {rollup}"
            try:
                self._gw.leave_bot_comment(parent, self._bot_name, summary)
            except Exception:
                _LOG.warning("could not leave PRD-complete comment on #%d", parent)
            try:
                self._gw.close_issue(parent, reason="completed")
            except Exception:
                _LOG.warning("could not close parent PRD #%d", parent)

    # ── selection ──────────────────────────────────────────────────────────────

    def _select_oldest(self) -> Optional[Issue]:
        """The OLDEST approved issue (smallest createdAt, ISO-8601 sorts
        lexicographically), or None when there are none."""
        issues = sorted(self._gw.list_approved_issues(), key=lambda i: i.created_at)
        return issues[0] if issues else None

    # ── trading-surface wall (#63): refuse to auto-integrate; raise the pill ────

    def _raise_trade_gate(self, n: int, gate: IntegrationDecision) -> PipelineResult:
        """Refuse to auto-integrate a money-path / indicator-math change. main is
        left UNTOUCHED (this fires BEFORE the merge). Bounce ``approved`` →
        ``gate.state_label`` (needs-trade-approval / needs-baseline-backtest), post
        the plain-English brief as a 🤖 comment, and raise a pill via ``recorder.halt``
        TAGGED with ``gate.stage`` so the dashboard reads it as a trading-surface
        refusal / parity gate, not a build/merge halt.

        Distinct from ``_halt``: this is NOT a failure — it's a deliberate WALL
        awaiting a human clearance label. It reuses the HaltReport / recorder.halt
        pill machinery (same as the architecture freeze) because the dashboard
        surface is the same "needs your call" pill; the brief (the full PM-facing
        text) rides in ``detail`` and is posted verbatim as the issue comment."""
        _LOG.warning("trading-surface wall on #%d (%s): %s", n, gate.stage, gate.reason)
        # A wall is a deliberate pause awaiting a human clearance label, NOT a
        # failure — record it as an ``info`` step on the run's structured feed.
        self._stage(n, gate.stage, "info", gate.reason)
        report = HaltReport(
            agent_name=self._bot_name, issue_number=n,
            stage=gate.stage, reason=gate.reason, detail=gate.brief,
        )
        try:
            self._gw.set_labels(n, remove=[APPROVED_LABEL], add=[gate.state_label])
        except Exception:
            _LOG.exception("could not bounce issue #%d to %s", n, gate.state_label)
        # The PM-facing brief goes on the issue verbatim (it names the files + the
        # exact clearance label to apply).
        try:
            self._gw.leave_bot_comment(n, self._bot_name, gate.brief)
        except Exception:
            _LOG.warning("could not leave trading-surface brief on #%d", n)
        self._recorder.halt(n, report=format_halt_report(report))
        return PipelineResult(acted=True, issue_number=n, failed=True, reason=gate.reason)

    # ── halt path: do NOT push; main untouched; raise a 🔴 record (Slice F) ─────

    def _halt(self, n: int, *, stage: str, reason: str, log: str = "") -> PipelineResult:
        """HALT the run: a red suite, a non-trivial merge conflict, a missing PR,
        or any hard error means Eli can't safely land, so it leaves ``main``
        UNTOUCHED and raises a 🔴 record. Flip approved → needs-human, post a 🤖
        halt provenance comment, mark the run ``halted`` (not failed/finished).
        Surfaces on the dashboard's red pill instead of silently re-queuing.

        Mirrors the review pipeline's ``_reject`` / ``_fail`` mechanism (HaltReport
        + format_halt_report + a 🤖 provenance comment); the stage names where in
        the land it stopped (merge / bundle / test / push / land)."""
        _LOG.warning("landing of issue #%d halted at %s: %s", n, stage, reason)
        # Record the halting stage on the run's structured feed (the 🔴 step). The
        # full command output (e.g. the failing pytest run) rides as the stage log
        # so a halted land is inspectable, not just summarized.
        self._stage(n, stage, "fail", reason, log=log)
        report = HaltReport(
            agent_name=self._bot_name, issue_number=n, stage=stage, reason=reason)
        try:
            self._gw.set_labels(n, remove=[APPROVED_LABEL], add=[NEEDS_HUMAN_LABEL])
        except Exception:
            _LOG.exception("could not bounce issue #%d to needs-human", n)
        # Best-effort 🤖 halt provenance comment (the run's halt report is canonical).
        try:
            self._gw.leave_bot_comment(n, self._bot_name, _halt_sentence(report))
        except Exception:
            _LOG.warning("could not leave halt provenance comment on #%d", n)
        self._recorder.halt(n, report=format_halt_report(report))
        return PipelineResult(acted=True, issue_number=n, failed=True, reason=reason)


# ═══════════════════════════════════════════════════════════════════════════════
# Slice 3 (#62) — Eli's ARCHITECTURAL CALL: in-scope re-plan vs out-of-scope freeze
# ═══════════════════════════════════════════════════════════════════════════════
#
# After a land, Eli evaluates the REMAINING open child issues of the parent PRD
# against that PRD (the IMMUTABLE north star). The PURE half — classify the LLM's
# assessment and route it to a ReplanPlan or a Freeze — lives in
# trident_eli_architect (mirroring how parse_recommendation is the pure half of
# Kleiner's review). This class is the EXECUTION half: it takes that pure decision
# and performs the side effects through INJECTED seams (the same gateway shape Eli
# already uses + a durable loop-gate). It performs NO classification itself.

# The provenance audit comment shape (no 🤖 prefix — leave_bot_comment adds it).
def _parent_ref_line(parent: int) -> str:
    """The ``Parent #<PRD>`` backref line stamped into every opened/edited child
    body so the gateway's parent-ref parser links it to the PRD (the north star).
    Matches the ``Parent #N`` convention the gateway's ``_PARENT_RE`` reads."""
    return f"Parent #{parent}"


def _ensure_parent_ref(body: str, parent: int) -> str:
    """Return ``body`` guaranteed to carry a ``Parent #<parent>`` ref. PURE.

    If the body already names this parent (gateway convention), it is returned
    unchanged; otherwise the ref is appended as its own trailing section. Eli
    NEVER edits the PRD — it stamps the child's body so the child links UP to the
    PRD, which is what keeps PRD-completion detection (Slice 2b) honest."""
    from shared.github_gateway import parse_parent_refs
    if parent and parent in parse_parent_refs(body):
        return body
    base = (body or "").rstrip()
    ref = _parent_ref_line(parent)
    return f"{base}\n\n## Parent\n{ref}\n" if base else f"## Parent\n{ref}\n"


class ArchitectPipeline:
    """Drives ONE post-land architectural re-plan cycle (Slice 3, #62).

    Given an :class:`~trident_eli_architect.ArchAssessment` (the LLM's structured
    output — produced upstream; this class does NOT call the LLM), it routes it
    through the pure ``decide_replan`` and then EXECUTES the decision:

      * a :class:`~trident_eli_architect.ReplanPlan` ⇒ run each mechanical action
        through the injected gateway (edit / reorder / close / open), each leaving
        a 🤖 audit comment and (for opens/edits) a ``Parent #<PRD>`` ref. NO freeze.
      * a :class:`~trident_eli_architect.Freeze` ⇒ write the durable freeze state
        + a 🔴 architecture-pill halt record, and perform NO issue mutations.

    Every side effect is an injected seam (gateway + loop-gate + recorder), so the
    whole flow is fake-driven in tests — no real gh, no real freeze file. Mirrors
    EliPipeline's seam-injection style. This class does NOT wire the freeze into
    Gordon's selector / the live serial loop — that is Slice #64.
    """

    def __init__(
        self,
        *,
        gateway: "ArchitectGatewayLike",
        loop_gate: LoopGateLike,
        recorder: Optional[EliRunRecorder] = None,
        bot_name: str = BOT_NAME,
    ) -> None:
        self._gw = gateway
        self._gate = loop_gate
        self._recorder = recorder or _NullRunRecorder()
        self._bot_name = bot_name

    # ── the cycle ──────────────────────────────────────────────────────────────

    def process(self, assessment: ArchAssessment) -> PipelineResult:
        """Route + execute ONE architectural assessment. Returns a PipelineResult.

        in_scope ⇒ execute the mechanical re-plan (acted=True, failed=False).
        out_of_scope / malformed ⇒ FREEZE the loop + a 🔴 architecture pill
        (acted=True, failed=True). A hard error anywhere fail-safes to a freeze —
        when in doubt we STOP and ask the human (real capital)."""
        parent = assessment.parent
        try:
            decision: ArchDecision = decide_replan(assessment)
        except Exception as e:  # fail safe: an un-routable assessment freezes
            return self._freeze(parent, reason=f"could not route assessment: {e}")

        if isinstance(decision, Freeze):
            return self._freeze(decision.parent or parent, reason=decision.reason)

        # in_scope ⇒ execute the plan. Any hard error mid-plan fail-safes to a
        # freeze — a half-applied re-plan is exactly the ambiguous state a human
        # must resolve, so we stop claiming rather than guess.
        try:
            return self._execute(decision)
        except Exception as e:
            return self._freeze(
                decision.parent or parent,
                reason=f"in-scope re-plan errored mid-execution: {e}",
            )

    # ── in-scope: execute the mechanical actions ───────────────────────────────

    def _execute(self, plan: ReplanPlan) -> PipelineResult:
        """Execute each action of an in-scope :class:`ReplanPlan`, in order. Each
        action leaves a 🤖 audit comment; opens/edits stamp ``Parent #<PRD>``.

        The PRD itself is NEVER touched — every action targets a CHILD issue (new
        or existing). A zero-action plan is a clean no-op (Eli looked, found
        nothing to change) — it records SUCCEEDED with no mutations."""
        parent = plan.parent
        opened: list[int] = []
        for action in plan.actions:
            if isinstance(action, OpenIssue):
                opened.append(self._do_open(action, parent))
            elif isinstance(action, EditIssue):
                self._do_edit(action.number, action.body, parent,
                              note="rewrote acceptance criteria")
            elif isinstance(action, ReorderDeps):
                self._do_reorder(action.number, action.blocked_by, parent)
            elif isinstance(action, CloseObsolete):
                self._do_close(action.number, action.reason)
            else:  # defensive: an unknown action type is itself ambiguous ⇒ stop
                raise TypeError(f"unknown re-plan action: {action!r}")

        self._recorder.finish(
            parent, succeeded=True, pr_url=None,
            note=f"in-scope re-plan: {len(plan.actions)} action(s)",
        )
        _LOG.info("architectural re-plan for PRD #%d executed %d action(s) (opened %s)",
                  parent, len(plan.actions), opened)
        return PipelineResult(acted=True, issue_number=parent, failed=False)

    def _do_open(self, action: OpenIssue, parent: int) -> int:
        """Open a new ready-for-agent child carrying a Parent ref, then leave a 🤖
        audit comment on it. Eli MAY apply ready-for-agent here (#62) — the inverse
        of Gordon's #55 self-promote guard."""
        body = _ensure_parent_ref(action.body, parent)
        number = self._gw.open_issue(action.title, body, labels=[READY_LABEL])
        try:
            self._gw.leave_bot_comment(
                number, self._bot_name,
                f"opened in an in-scope re-plan of PRD #{parent} "
                f"(north star) — ready-for-agent",
            )
        except Exception:
            _LOG.warning("could not leave audit comment on newly opened #%d", number)
        return number

    def _do_edit(self, number: int, body: str, parent: int, *, note: str) -> None:
        """Rewrite a child issue's body (acceptance criteria or reordered deps),
        keeping its Parent ref, then leave a 🤖 audit comment."""
        new_body = _ensure_parent_ref(body, parent)
        self._gw.edit_issue(number, body=new_body)
        try:
            self._gw.leave_bot_comment(
                number, self._bot_name,
                f"{note} in an in-scope re-plan of PRD #{parent} (north star)",
            )
        except Exception:
            _LOG.warning("could not leave audit comment on edited #%d", number)

    def _do_reorder(self, number: int, blocked_by, parent: int) -> None:
        """Reorder a child issue's ``Blocked by`` deps as a READ-MODIFY-WRITE so the
        rest of its body survives (#62 fix).

        The old code rendered ONLY the ``## Blocked by`` section and fed that as the
        whole replacement body — ``edit_issue`` is a full-body REPLACE, so reordering
        one issue's deps DESTROYED its "What to build", "Acceptance criteria", and
        every other section. The correct flow:

          1. READ the issue's current body via ``get_issue_body``.
          2. ``replace_blocked_by_section`` — swap ONLY the deps section, preserve
             every other section verbatim (insert if absent / drop if empty).
          3. Ensure the ``Parent #<PRD>`` ref, then edit back the FULL body.

        Still leaves the 🤖 audit comment naming the reorder (via ``_do_edit``)."""
        current = self._gw.get_issue_body(number)
        new_body = replace_blocked_by_section(current, tuple(blocked_by or ()))
        self._do_edit(number, new_body, parent,
                      note="reordered dependencies "
                           f"(blocked by {list(blocked_by or ())})")

    def _do_close(self, number: int, reason: str) -> None:
        """Close an obsoleted child (reason 'not planned' — it was NOT completed),
        then leave a 🤖 audit comment naming why."""
        self._gw.close_issue(number, reason="not planned")
        try:
            self._gw.leave_bot_comment(
                number, self._bot_name,
                f"closed as obsolete in an in-scope re-plan — {reason}",
            )
        except Exception:
            _LOG.warning("could not leave audit comment on closed #%d", number)

    # ── out-of-scope / malformed: FREEZE the loop + a 🔴 architecture pill ──────

    def _freeze(self, parent: int, *, reason: str) -> PipelineResult:
        """Freeze the loop (durable seam) and record a 🔴 ARCHITECTURE pill. Does
        NOT mutate any issue — the work stops and waits for the PM.

        The freeze is written to the injected ``LoopGateLike`` (a state DISTINCT
        from Nathan's watch-pause). The 🔴 pill reuses the HaltReport / recorder
        .halt pattern Eli's land path uses, TAGGED as an architecture freeze in
        the stage so the dashboard's red pill reads it as a scope-drift stop, not
        a build/merge halt. Best-effort on the freeze write + comment; the halt
        record is canonical."""
        _LOG.warning("architecture FREEZE on PRD #%d: %s", parent, reason)
        report = HaltReport(
            agent_name=self._bot_name, issue_number=parent,
            stage="architecture-freeze", reason=reason,
            detail="The remaining work drifted past the PRD's product scope. "
                   "Eli froze the loop (no new claims) until you resolve this in "
                   "a session. This is separate from the manual watch pause.",
        )
        try:
            self._gate.freeze(reason=reason, issue=parent)
        except Exception:
            _LOG.exception("could not write durable freeze state for PRD #%d", parent)
        # Best-effort 🔴 architecture-pill provenance comment on the PRD.
        try:
            self._gw.leave_bot_comment(parent, self._bot_name, _halt_sentence(report))
        except Exception:
            _LOG.warning("could not leave architecture-freeze comment on #%d", parent)
        self._recorder.halt(parent, report=format_halt_report(report))
        return PipelineResult(acted=True, issue_number=parent, failed=True, reason=reason)


# ── the architect pipeline's gateway seam (a SUBSET of EliGatewayLike) ──────────

class ArchitectGatewayLike(Protocol):
    """The gh primitives the architect pipeline executes a re-plan through. A
    subset of the full gateway: opens/edits/closes children + leaves audit
    comments. NO merge/push/full-suite (those are the land path's, not the
    re-plan's)."""

    def open_issue(self, title: str, body: str, labels=None) -> int: ...
    def edit_issue(self, number: int, *, body: Optional[str] = None) -> None: ...
    def close_issue(self, number: int, *, reason: str = "completed") -> None: ...
    def leave_bot_comment(self, number: int, bot_name: str, sentence: str) -> None: ...
    # ReorderDeps is a READ-MODIFY-WRITE: fetch the live body, swap ONLY its
    # ## Blocked by section, edit back the FULL preserved body (#62 fix).
    def get_issue_body(self, number: int) -> str: ...
