"""
trident_review_pipeline.py — the read-only review agent (Slice B of PRD #58):
**Dr. Isaac Kleiner**.

What this is
------------
Gordon Freeman (the issue→PR worker, trident_agent_pipeline) leaves every
finished PR's issue in ``needs-review``. Kleiner is the second stage of the
human-in-the-loop assembly line: it forms an INDEPENDENT, plain-English opinion
on whether that work is good, so Nathan (a trader, not a coder) can sign off
without reading the diff himself.

The flow (``ReviewPipeline.process``)
-------------------------------------
1. Pick the OLDEST ``needs-review`` issue (oldest = smallest createdAt, like
   Gordon's selector). No issue ⇒ no-op (acted=False).
2. Find the open PR that ``Closes #N`` via the gateway. No PR ⇒ HALT (Slice F):
   an item Kleiner can't review genuinely needs a human, so flip needs-review →
   needs-human, post a 🤖 halt comment, and mark the run ``halted``.
3. Fetch the PR diff (read-only — no worktree, no test re-run; Gordon already
   gated the tests green).
4. Make a THROWAWAY temp dir, build a review prompt that embeds the issue + the
   diff, launch claude in that dir and WAIT for it, then read the
   ``recommendation.md`` it wrote there. Kleiner never needs repo access — the
   whole world it reviews is inside the prompt.
5. Parse the verdict (APPROVE / REJECT / unclear) + keep the full text.
6. Post the FULL recommendation as a PR comment ALWAYS (audit trail, regardless
   of verdict); on the clean path leave a one-line ``🤖 Dr. Isaac Kleiner: …``
   provenance comment on the ISSUE.
7. The verdict is TERMINAL — no routine human gate:
     * clean APPROVE ⇒ advance the issue ``needs-review`` → ``approved`` (Eli's
       integration inbox). Mark the run succeeded.
     * REJECT, OR an unclear/missing verdict ⇒ red flag. Because this manages
       real capital, an ambiguous verdict is a STOP, not a pass: route to
       ``needs-human`` and record a 🔴 halt carrying a concise reason
       (``_reject``). The recommendation was still posted in step 6.
   The ``supervised`` constructor seam (default False = full-auto) flips a CLEAN
   review to ``needs-signoff`` (the dormant 🟡 manual gate) instead of
   ``approved`` — Slice 7 wires the real toggle UI to it.

If ``recommendation.md`` is missing/empty ⇒ HALT to needs-human (Slice F, the
``_fail`` can't-review path — distinct from the ``_reject`` red-flag path), DO
NOT advance.

Read-only: NO worktree, NO test re-run. Hermetic: the diff is embedded in the
prompt; Claude writes its recommendation to a file in a throwaway temp dir.

Pure cores (unit-tested as data tables, no I/O)
-----------------------------------------------
  * ``build_review_prompt``  — issue + diff → claude review prompt
  * ``parse_recommendation`` — claude's written rec → Recommendation(verdict, text)

Every side-effecting seam is INJECTED into ``ReviewPipeline`` so the whole flow
is driven by fakes in tests — no real gh, no real claude, no real filesystem
beyond the injected temp dir. Mirrors trident_agent_pipeline.IssuePrPipeline.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from agents.halt import HaltReport, format_halt_comment, format_halt_report
from agents.pipeline import PipelineResult
from shared.github_gateway import (
    Issue,
    NEEDS_REVIEW_LABEL,
    NEEDS_SIGNOFF_LABEL,
    NEEDS_HUMAN_LABEL,
    APPROVED_LABEL,
)
from agents.kleiner_rework import (
    DEFAULT_MAX_ROUNDS,
    build_fixer_prompt,
    route_review,
)
from agents.review_lens import load_review_lens
from agents.trade_surface import classify_paths

_LOG = logging.getLogger(__name__)

# The agent identity Kleiner stamps on provenance comments (same convention as
# Gordon — the shared settingzbot gh account can't otherwise attribute actions).
BOT_NAME = "Dr. Isaac Kleiner"

# The file Claude writes its recommendation to, inside the run's throwaway cwd.
DEFAULT_OUT_FILENAME = "recommendation.md"

# Retry-don't-halt budget for a review whose claude RAN but produced no usable
# output (the #96 transient "zero assistant turns"). 2 = one retry. See __init__.
DEFAULT_REVIEW_LAUNCH_ATTEMPTS = 2


# ═══════════════════════════════════════════════════════════════════════════════
# Pure core 1 — issue + PR diff → claude review prompt
# ═══════════════════════════════════════════════════════════════════════════════

def build_review_prompt(
    issue: Issue, pr_diff: str, *, out_filename: str = DEFAULT_OUT_FILENAME,
    lens: str = "",
) -> str:
    """Build the claude review prompt for an issue + its PR diff. PURE.

    Gives Claude the issue (number/title/body) and the full PR diff, and asks it
    to form its OWN judgment about correctness, scope-vs-issue, and risk — NOT
    just to echo "the tests passed" (Gordon already gated those). It must write a
    PLAIN-ENGLISH recommendation (no code required of the PM) to ``./<out_filename>``
    in its cwd, beginning with a verdict line ``VERDICT: APPROVE`` or
    ``VERDICT: REJECT``.

    ``lens`` (#67) is the curated "review lens" TEXT — Trident's highest-consequence,
    easy-to-miss-in-tests risk classes (``docs/claude/review_heuristics.md``). It is
    passed IN as a parameter (the I/O seam ``trident_review_lens.load_review_lens``
    reads the file; the CALLER injects the text here) so this builder stays PURE and
    table-testable. When present it SHARPENS the correctness/scope/risk asks below
    (and the ``SCOPE:`` line) — it does not replace them. An EMPTY lens (the
    defensive no-lens fallback) is a clean no-op: no "lens" section is emitted and the
    prompt is otherwise byte-identical to the pre-#67 generic pass.
    """
    body = (issue.body or "").strip() or "(no description provided)"
    diff = (pr_diff or "").strip() or "(empty diff)"
    lens_block = ""
    lens_text = (lens or "").strip()
    if lens_text:
        lens_block = (
            "REVIEW LENS — Trident's highest-consequence, easy-to-miss-in-tests risk "
            "classes. Tests check what the author thought to test; YOUR value is "
            "catching the classes below that no test was written for. Read the diff "
            "THROUGH this lens (it SHARPENS the asks below, it does not replace them); "
            "if the diff trips any of these, say so explicitly:\n"
            f"{lens_text}\n\n"
        )
    return (
        f"You are Dr. Isaac Kleiner, the READ-ONLY review agent for the Trident "
        f"repo. You are reviewing the pull request that closes GitHub issue "
        f"#{issue.number}. You have NO repository access — everything you need is "
        f"below.\n\n"
        f"ISSUE #{issue.number}: {issue.title}\n\n"
        f"ISSUE DESCRIPTION:\n{body}\n\n"
        f"THE PULL REQUEST DIFF:\n"
        f"```diff\n{diff}\n```\n\n"
        f"{lens_block}"
        "YOUR JOB:\n"
        "Form your OWN judgment about this change. Do NOT just say 'the tests "
        "passed' — the producer already gated tests green; your value is a second "
        "opinion. Specifically assess:\n"
        "  - CORRECTNESS: does the diff actually do what the issue asks, correctly?\n"
        "  - SCOPE: does it match the issue's scope — nothing missing, nothing "
        "extra/out-of-scope (e.g. touching unrelated production code)?\n"
        "  - RISK: could this break something, regress behavior, or be unsafe "
        "for live trading?\n\n"
        "WRITE YOUR RECOMMENDATION in PLAIN ENGLISH (the reader is a trader, not "
        "a coder — no code required of them) to the file "
        f"`./{out_filename}` in your current directory. It MUST begin with a "
        "single verdict line, exactly one of:\n"
        "  VERDICT: APPROVE\n"
        "  VERDICT: REJECT\n"
        "followed by your reasoning. You ADVISE; the PM decides. Be honest and "
        "specific about why.\n\n"
        "IF YOU REJECT, add a second tag line classifying the finding so the loop "
        "knows whether it is auto-fixable, exactly one of:\n"
        "  SCOPE: IN_SCOPE      (a contained, fixable defect — e.g. a leaked key, a "
        "body-wipe bug, an off-by-one — in 1–2 files, NOT touching live trading)\n"
        "  SCOPE: OUT_OF_SCOPE  (architectural / out-of-scope / anything touching "
        "the live-trading surface — needs a human)\n"
        "When in doubt, choose OUT_OF_SCOPE — a human will look. (No SCOPE line is "
        "needed for APPROVE.)\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Pure core 2 — claude's written recommendation → verdict + full text
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Recommendation:
    """A parsed review recommendation: a verdict + an optional scope tag + the
    full plain-English text.

    ``scope`` is the finding's classification, parsed from a ``SCOPE:`` line that a
    REJECT may carry (issue #71): ``"in_scope"`` (a contained, fixable defect — the
    rework loop auto-dispatches a fresh fixer), ``"out_of_scope"`` (architectural /
    money-path — escalate), or ``"unclear"`` (no usable SCOPE line). Back-compatible:
    an APPROVE doesn't need a SCOPE line, and a REJECT with no/typo'd SCOPE line
    parses to ``"unclear"`` — which the route core (``route_review``) FAILS SAFE on
    by treating as out-of-scope and escalating."""

    verdict: str   # "approve" / "reject" / "unclear"
    text: str      # the full raw recommendation (stripped)
    scope: str = "unclear"  # "in_scope" / "out_of_scope" / "unclear" (#71)


# "VERDICT: APPROVE" / "VERDICT: REJECT" — case-insensitive, anywhere in the text.
_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REJECT)", re.IGNORECASE)

# "SCOPE: IN_SCOPE" / "SCOPE: OUT_OF_SCOPE" — case-insensitive, anywhere (#71). A
# REJECT carries this to say whether the finding is a contained fixable defect.
_SCOPE_RE = re.compile(r"SCOPE:\s*(IN_SCOPE|OUT_OF_SCOPE)", re.IGNORECASE)

# claude's stdout is captured here in the run's cwd (build_claude_command's
# stdout_path). `claude -p` always PRINTS its answer, so this is the robust
# fallback when the agent didn't write its recommendation file, and the forensic
# record of what it actually said.
_STDOUT_CAPTURE_NAME = "claude_stdout.txt"


def _read_text_quiet(path) -> str:
    """Read a file as BOM-tolerant UTF-8; '' on absence or any error. Never raises
    — used on the can't-review path where a second exception would mask the real
    reason. (PS 5.1 ``Out-File -Encoding utf8`` writes a BOM, hence utf-8-sig.)"""
    try:
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8-sig")
    except Exception:
        pass
    return ""


def _unlink_quiet(path) -> None:
    """Delete a file if present; swallow every error. Used to wipe a PRIOR retry
    attempt's output from the REUSED workdir before the next launch, so a later
    attempt can never read/grade stale leftovers (#118 M7)."""
    try:
        Path(path).unlink()
    except (FileNotFoundError, OSError):
        pass


def _launch_reason_severity(reason: str) -> int:
    """Rank a truthful launch-failure reason so the MOST diagnostic cause survives
    across retry attempts (#118 M3). A TIMEOUT (the masked 30-min-stall failure
    mode — #99/[[footguns#195]]) outranks any other launch failure, which outranks
    an empty/zero-turns blip (no reason). PURE."""
    r = (reason or "").lower()
    if not r:
        return 0
    if "timeout" in r or "timed out" in r:
        return 2
    return 1


def _resolve_transcript_for(cwd) -> str | None:
    """Best-effort path to the Claude-Code transcript a run wrote for ``cwd`` — so
    a halt note can point a human straight at the full session log. None if it
    can't be resolved (never raises)."""
    try:
        from agents.feed import (
            default_transcript_base, project_slug, resolve_transcript_path)
        p = resolve_transcript_path(default_transcript_base() / project_slug(str(cwd)))
        return str(p) if p else None
    except Exception:
        return None


def _halt_sentence(report: HaltReport) -> str:
    """The SENTENCE portion of the halt provenance comment (no 🤖 prefix).

    ``leave_bot_comment`` adds the ``🤖 {bot_name}: `` prefix itself, so passing
    the whole ``format_halt_comment`` string would double the prefix. We strip
    the head so the posted comment ends up byte-identical to
    ``format_halt_comment(report)`` — same shape Gordon uses for its halts."""
    full = format_halt_comment(report)
    _, _, sentence = full.partition(": ")
    return sentence or full


def parse_recommendation(raw_text: str) -> Recommendation:
    """Parse Claude's written recommendation into a Recommendation. PURE.

    The verdict is read from the ``VERDICT:`` line (case-insensitive, may appear
    anywhere); ``unclear`` if no such line is present. ``text`` is the full raw
    recommendation, stripped.

    FAIL-SAFE on CONFLICT (#118 L3): the match-anywhere behavior is deliberate and
    tested (claude sometimes writes ``## Verdict: APPROVE`` mid-document), so we keep
    it — but "first match wins" was a latent footgun. The improbable-but-dangerous
    case is a reviewed diff that QUOTES a verdict token (e.g. a test string
    ``VERDICT: APPROVE``) while the reviewer's REAL line says ``VERDICT: REJECT``;
    first-match-wins could silently flip a REJECT to an APPROVE. So when MULTIPLE
    DISTINCT verdict tokens appear we refuse to guess and return ``unclear`` — which
    the route core treats as a STOP → needs-human (real capital; ambiguity is never
    a silent pass). A single token repeated any number of times is unchanged.

    The scope tag (#71) is read from a ``SCOPE: IN_SCOPE`` / ``SCOPE: OUT_OF_SCOPE``
    line (case-insensitive, anywhere); ``unclear`` if no such line is present. This
    is back-compatible: an APPROVE (which needs no SCOPE line) and a REJECT with a
    missing/typo'd SCOPE line both parse to ``scope == "unclear"`` — the route core
    FAILS SAFE on that (treats it as out-of-scope and escalates), mirroring
    ``scope_litmus`` and Kleiner's existing "unclear verdict = STOP".
    """
    text = (raw_text or "").strip()
    # Match ANYWHERE (deliberate), but collapse to the DISTINCT tokens found: one
    # distinct token (however many times it repeats) is that verdict; zero tokens or
    # two CONFLICTING tokens (APPROVE *and* REJECT) → unclear (fail-safe).
    found = {m.lower() for m in _VERDICT_RE.findall(text)}
    verdict = next(iter(found)) if len(found) == 1 else "unclear"
    sm = _SCOPE_RE.search(text)
    scope = sm.group(1).lower() if sm else "unclear"
    return Recommendation(verdict=verdict, text=text, scope=scope)


# ═══════════════════════════════════════════════════════════════════════════════
# Side-effecting seam protocol (injected — fakeable in tests)
# ═══════════════════════════════════════════════════════════════════════════════

class ReviewGatewayLike(Protocol):
    def list_needs_review_issues(self) -> list[Issue]: ...
    def find_open_pr_for_issue(self, issue_number: int) -> Optional[int]: ...
    def get_pr_diff(self, pr_number: int) -> str: ...
    def get_pr_files(self, pr_number: int) -> list[str]: ...
    def leave_pr_comment(self, pr_number: int, body: str) -> None: ...
    def leave_bot_comment(self, number: int, bot_name: str, sentence: str) -> None: ...
    def set_labels(self, number: int, add=None, remove=None) -> None: ...


# run_launcher: (*, command, cwd, tag) -> bool   (True = run completed OK)
# tempdir_maker: () -> str  (a throwaway dir path; default tempfile.mkdtemp)
#
# fixer_dispatch: (*, pr_number, issue_number, finding, files) -> bool   (#71)
#   The rework loop's FRESH-fixer seam. Distinct from ``run_launcher`` (the
#   reviewer) AND from Gordon's builder launcher — it is its OWN callable, so the
#   fixer is STRUCTURALLY a brand-new agent and can NEVER be the resumed builder
#   (the #60 cost lesson: re-ingesting the builder's transcript costs ~the whole
#   build again). In production it builds a scoped fix prompt (build_fixer_prompt)
#   and runs a fresh claude session to completion in a worktree on the PR's branch,
#   then commits — mirroring how Gordon's own builder launches. Returns True iff
#   the fix was applied + committed (so the loop can re-fetch the diff and
#   re-review). Tests inject a fake that simulates the fix and scripts the next
#   round's verdict — NO real worktree / gh / git / claude.


# ═══════════════════════════════════════════════════════════════════════════════
# Optional run recorder (same shape Gordon uses; no-op by default)
# ═══════════════════════════════════════════════════════════════════════════════

class ReviewRunRecorder(Protocol):
    def start(self, issue: Issue) -> None: ...
    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url: Optional[str], note: str = "") -> None: ...
    def halt(self, issue_number: int, *, report: str) -> None: ...


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
# ReviewPipeline — the orchestrator (Kleiner)
# ═══════════════════════════════════════════════════════════════════════════════

class ReviewPipeline:
    """Drives one needs-review → needs-signoff review cycle. All side-effecting
    seams are injected, mirroring IssuePrPipeline.

    The ``run_launcher`` seam has the SAME shape Gordon uses
    (``(*, command, cwd, tag) -> bool``); production wires it to
    ``build_claude_command`` + ``run_session_to_completion`` (launch claude and
    WAIT), the caller assembling those exactly as the issue→PR pipeline does.
    """

    def __init__(
        self,
        *,
        gateway: ReviewGatewayLike,
        run_launcher: Callable[..., bool],
        tempdir_maker: Optional[Callable[[], str]] = None,
        recorder: Optional[ReviewRunRecorder] = None,
        bot_name: str = BOT_NAME,
        out_filename: str = DEFAULT_OUT_FILENAME,
        supervised: bool = False,
        fixer_dispatch: Optional[Callable[..., bool]] = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        lens_loader: Optional[Callable[[], str]] = None,
        launch_attempts: int = DEFAULT_REVIEW_LAUNCH_ATTEMPTS,
    ) -> None:
        self._gw = gateway
        self._launch = run_launcher
        self._mktemp = tempdir_maker or tempfile.mkdtemp
        # The curated review lens (#67) — an I/O seam read ONCE per review pass and
        # passed as TEXT into the pure build_review_prompt. Defaults to the real
        # loader (reads docs/claude/review_heuristics.md); DEFENSIVE — a missing/empty
        # file degrades to "" (no-lens) and never crashes a review. Injectable so
        # tests can drive it with a temp lens file.
        self._load_lens = lens_loader or load_review_lens
        self._recorder = recorder or _NullRunRecorder()
        self._bot_name = bot_name
        self._out_filename = out_filename
        # supervised=False (default) = full-auto: a clean APPROVE advances to
        # `approved` (Eli's inbox). supervised=True = the dormant 🟡 manual gate:
        # a clean review parks in needs-signoff for a human sign-off instead.
        # Slice 7 wires the real toggle UI to this; for now it's a plain seam.
        self._supervised = supervised
        # The FRESH-fixer seam (#71). A separate callable from ``run_launcher`` so
        # the fixer is STRUCTURALLY a brand-new agent — NEVER the resumed builder
        # (the #60 cost lesson). None ⇒ the rework loop is DISABLED and a
        # non-approve verdict red-flags straight to needs-human (today's #59
        # behavior, fully back-compatible). Tests inject a fake that simulates the
        # fix + scripts the next round's verdict.
        self._fixer_dispatch = fixer_dispatch
        # Bounded rework: after this many failed fix rounds on the same PR, stop
        # auto-fixing and escalate 🔴 (never loop forever). Default 2.
        self._max_rounds = max_rounds
        # Retry-don't-halt (#96): a review whose claude RAN but produced no usable
        # output — the transient "zero assistant turns" blip (live-proven: a re-run
        # minutes later APPROVED cleanly) — re-launches up to this many times before
        # escalating to a human, instead of halting on the first empty result. A
        # genuine verdict (or a parse with content) returns immediately; only an
        # empty/launch-failed attempt is retried. Default 2 (one retry — reviews are
        # expensive; the transient clears on the next run).
        self._launch_attempts = max(1, launch_attempts)

    # ── the cycle ──────────────────────────────────────────────────────────────

    def process(self) -> PipelineResult:
        """Run ONE review cycle, WITH the bounded rework loop (#71). Returns a
        :class:`PipelineResult`.

        No needs-review issue ⇒ no-op (acted=False). Otherwise reviews the OLDEST
        one and posts a recommendation. The verdict is then routed by the PURE core
        ``route_review`` into one of FOUR routes (all driven within THIS single
        ``process()`` call — no cross-invocation state):

          * CLEAN (APPROVE) ⇒ advance to ``approved`` (or ``needs-signoff`` under
            ``supervised``). Done.
          * IN_SCOPE_FIXABLE ⇒ dispatch a FRESH fixer (never the resumed builder —
            the #60 cost lesson) scoped to the finding + the PR's files, then
            re-fetch the diff and RE-REVIEW (round += 1). Loop.
          * OUT_OF_SCOPE_OR_MONEY_PATH ⇒ STOP. The finding is architectural /
            out-of-scope, OR the diff STRUCTURALLY touches the trading surface
            (``classify_paths`` — the money veto OVERRIDES the LLM's scope call).
            Red-flag to needs-human. No auto-fix.
          * ROUND_BUDGET_EXHAUSTED ⇒ STOP. The fix budget (``max_rounds``, default
            2) is spent without converging. Red-flag to needs-human. No more fixes.

        On a missing PR, a missing/empty recommendation, or a fixer that can't
        apply its fix, it HALTS to needs-human (Slice F) — all red routes surface
        on the dashboard's red pill. With no ``fixer_dispatch`` injected the loop is
        DISABLED and a non-approve verdict red-flags straight to needs-human (the
        back-compatible #59 behavior).
        """
        issue = self._select_oldest()
        if issue is None:
            _LOG.info("review poll: no needs-review issue")
            return PipelineResult(acted=False, reason="no needs-review issue")

        n = issue.number
        self._recorder.start(issue)
        _LOG.info("reviewing issue #%d (%s)", n, issue.title)

        # 1) Find the PR that closes this issue.
        pr = self._gw.find_open_pr_for_issue(n)
        if pr is None:
            # Slice F: an item with no PR can't be reviewed → HALT to needs-human.
            return self._fail(n, note="no open PR closes this issue")

        # The bounded rework loop. round_count starts at 0 (the first review is
        # "round 0"); each dispatched fixer + re-review bumps it. The loop is
        # self-contained — no persistence across process() calls is needed.
        round_count = 0
        while True:
            # 2) Review the PR once (fetch diff → run claude → parse). A can't-review
            #    condition returns a HALT PipelineResult here (DO NOT advance).
            rec = self._review_once(issue, pr)
            if isinstance(rec, PipelineResult):
                return rec

            # 3) Post the FULL recommendation as a PR comment ALWAYS — every round —
            #    regardless of verdict (audit trail; best-effort).
            try:
                self._gw.leave_pr_comment(pr, rec.text)
            except Exception:
                _LOG.warning("could not post review comment on PR #%d", pr)

            # 4) STRUCTURAL money-path signal for THIS PR's diff. Decided by the
            #    detector over the PR's changed files — NEVER trusted to the LLM.
            #    Fail toward the wall: a read error counts as touching the surface
            #    (#63 bias — a money-path change auto-fixing is far worse than a
            #    benign one escalating).
            try:
                files = self._gw.get_pr_files(pr)
                touches_money = classify_paths(files).touches_trading_surface
            except Exception as e:
                _LOG.warning("could not read PR #%d files (treating as money-path "
                             "for safety): %s", pr, e)
                touches_money = True

            # 5) PURE route decision — the heart of the slice.
            route = route_review(
                verdict=rec.verdict,
                scope=rec.scope,
                touches_money_path=touches_money,
                round_count=round_count,
                max_rounds=self._max_rounds,
            )

            # 6) CLEAN ⇒ advance (honoring the supervised seam). Provenance on the
            #    issue, then the load-bearing label flip.
            if route.is_clean:
                try:
                    self._gw.leave_bot_comment(
                        n, self._bot_name,
                        f"reviewed PR #{pr} — recommend APPROVE",
                    )
                except Exception:
                    _LOG.warning("could not leave provenance comment on #%d", n)
                target = NEEDS_SIGNOFF_LABEL if self._supervised else APPROVED_LABEL
                self._gw.set_labels(n, remove=[NEEDS_REVIEW_LABEL], add=[target])
                self._recorder.finish(n, succeeded=True, pr_url=None)
                _LOG.info(
                    "issue #%d reviewed → %s (verdict=approve, supervised=%s, "
                    "rounds=%d)", n, target, self._supervised, round_count)
                return PipelineResult(acted=True, issue_number=n, failed=False)

            # 7) IN_SCOPE_FIXABLE ⇒ dispatch a FRESH fixer + re-review. The route
            #    core only permits this when a fixer seam exists AND the budget is
            #    not yet spent AND the diff is off the money path. Apply the fix,
            #    then loop back to RE-REVIEW the fixed PR (round += 1).
            if route.is_fixable and self._fixer_dispatch is not None:
                fix_ok = self._dispatch_fixer(issue, pr, rec)
                if isinstance(fix_ok, PipelineResult):
                    return fix_ok  # a fixer that couldn't apply ⇒ HALT to needs-human
                round_count += 1
                continue  # re-review the now-fixed PR

            # 8) Any STOP route (out-of-scope / money-path / budget exhausted), OR
            #    an IN_SCOPE_FIXABLE with no fixer seam wired (loop disabled — the
            #    back-compatible #59 behavior) ⇒ red-flag to needs-human. The route
            #    reason becomes the 🔴 record's text so the dashboard reads WHY.
            return self._reject(n, reason=route.reason or self._fallback_reject_reason(rec))

    # ── one review pass (fetch diff → run claude → parse) ───────────────────────

    def _review_once(self, issue: Issue, pr: int):
        """Run ONE review pass over PR ``pr``: fetch the diff (read-only), make a
        throwaway temp dir, launch claude and WAIT, read + parse the recommendation.

        Returns a :class:`Recommendation` on success, or a HALT :class:`PipelineResult`
        (via ``_fail``) for any can't-review condition (diff fetch error, temp-dir
        error, launch error, missing/empty recommendation). The caller treats a
        PipelineResult return as a terminal halt. Used on EVERY round of the rework
        loop — each re-review is a fresh claude pass over the re-fetched diff."""
        n = issue.number
        # Fetch the diff (read-only). Re-fetched every round so a re-review sees the
        # fixer's changes.
        try:
            diff = self._gw.get_pr_diff(pr)
        except Exception as e:
            return self._fail(n, note=f"could not fetch PR #{pr} diff: {e}")

        try:
            workdir = self._mktemp()
        except Exception as e:
            return self._fail(n, note=f"could not make temp dir: {e}")

        # Load the curated review lens (#67) — an I/O seam, read fresh each pass so an
        # appended lesson reaches the next review with no code change. The loader is
        # itself defensive (missing/empty ⇒ ""); the extra guard here keeps a
        # surprise loader failure from ever halting a review.
        try:
            lens = self._load_lens()
        except Exception as e:
            _LOG.warning("review lens load failed (running with no lens): %s", e)
            lens = ""
        prompt = build_review_prompt(
            issue, diff, out_filename=self._out_filename, lens=lens)
        from agents.pipeline import build_claude_command, new_transcript_id
        # Capture claude's stdout alongside the file it's asked to write. stdout is
        # the robust fallback (claude -p always prints its answer) AND the forensic
        # record — so a halt says what actually happened, not a guess.
        stdout_path = Path(workdir) / _STDOUT_CAPTURE_NAME
        # Pin a transcript id AND repoint the run's cwd at the temp workdir claude
        # actually launches in. The run record was created (start()) with cwd=repo
        # root, but the review's transcript lands under the WORKDIR slug — so
        # without both the feed resolved the newest unrelated transcript in the
        # operator's own project dir (the feed cross-wiring bug). One id reused
        # across the retry attempts below keeps the whole review in one transcript.
        transcript_id = new_transcript_id()
        command = build_claude_command(
            model="claude", prompt=prompt, stdout_path=str(stdout_path),
            session_id=transcript_id)
        _attach = getattr(self._recorder, "attach_session", None)
        if callable(_attach):
            try:
                _attach(n, transcript_id=transcript_id, cwd=str(workdir))
            except Exception:
                _LOG.exception("could not pin transcript id/cwd on review run for #%d", n)
        tag = f"review:issue-{n}"

        # Retry-don't-halt (#96): re-launch on a launch failure OR a ran-but-empty
        # result (the transient "zero assistant turns" — live-proven to clear on a
        # re-run), up to self._launch_attempts, before escalating to a human. A
        # launch yielding usable output breaks out immediately.
        ok = False
        rec_text = stdout_text = ""
        attempts = self._launch_attempts
        rec_path = Path(workdir) / self._out_filename
        # Track the MOST-SEVERE truthful launch reason ACROSS attempts (#118 M3).
        # The retry loop only ever inspected the LAST attempt, so a 30-min TIMEOUT on
        # attempt 1 followed by a launch-but-empty final attempt printed "zero
        # assistant turns" and DISCARDED the timeout — sending the operator down the
        # wrong diagnostic path. A timeout is the most diagnostic cause, so once any
        # attempt times out we hold that reason and prefer it over the generic
        # zero-turns phrasing below. (A timeout must read as a timeout — #118.)
        severe_reason = ""
        for attempt in range(1, attempts + 1):
            # M7 stale-file gate: the workdir is REUSED across attempts, so wipe any
            # leftover output from a PRIOR attempt BEFORE launching. Without this a
            # later attempt that itself produces nothing could read — and grade — an
            # earlier attempt's recommendation.md / stdout, which is stale relative to
            # THIS attempt's outcome. After the wipe, every read below reflects ONLY
            # the current attempt.
            _unlink_quiet(rec_path)
            _unlink_quiet(stdout_path)
            try:
                ok = self._launch(command=command, cwd=workdir, tag=tag)
            except Exception as e:
                ok = False
                if attempt >= attempts:
                    return self._fail(n, note=self._forensic_note(
                        f"review claude launch raised after {attempts} attempt(s): {e}",
                        workdir, ""))
                _LOG.warning("review #%d launch raised (attempt %d/%d) — retrying: %s",
                             n, attempt, attempts, e)
                continue
            # What did the agent produce? The file it's ASKED to write is primary;
            # its printed stdout is the fallback + forensic record. Both reflect only
            # THIS attempt — the prior attempt's files were wiped above.
            rec_text = _read_text_quiet(rec_path)
            stdout_text = _read_text_quiet(stdout_path)
            if ok and (rec_text.strip() or stdout_text.strip()):
                break  # usable result — stop retrying
            # This attempt yielded no usable result. Keep the most-severe truthful
            # reason across attempts (timeout > other launch failure > empty blip),
            # so an earlier timeout is not lost behind a later empty attempt.
            attempt_reason = getattr(ok, "reason", "")
            if _launch_reason_severity(attempt_reason) > _launch_reason_severity(severe_reason):
                severe_reason = attempt_reason
            if attempt < attempts:
                _LOG.warning(
                    "review #%d produced no usable output (ok=%s, attempt %d/%d) — "
                    "re-launching (likely a transient zero-turns blip)",
                    n, ok, attempt, attempts)

        if not ok:
            # The launcher could not run claude to completion on any attempt.
            # DISTINCT from "ran but empty". ``severe_reason`` carries the truthful,
            # most-severe cause across attempts — timeout vs failed-to-launch — so the
            # note says which (a bool-returning fake leaves it empty → generic phrasing).
            why = severe_reason or "launch failed or timed out"
            return self._fail(n, note=self._forensic_note(
                f"claude run did not complete after {attempts} attempt(s) — {why}",
                workdir, stdout_text))

        # Prefer the structured file; fall back to the printed answer. The same
        # parser handles both (the verdict line matches "VERDICT:" or "## Verdict:").
        raw = rec_text if rec_text.strip() else stdout_text
        if not raw.strip():
            # The FINAL attempt ran to completion but emitted NOTHING. If an EARLIER
            # attempt TIMED OUT, that is the truthful, most-diagnostic cause (#118
            # M3) — surface it as a timeout rather than the generic zero-turns
            # phrasing, which would send the operator down the wrong path.
            if _launch_reason_severity(severe_reason) >= 2:
                return self._fail(n, note=self._forensic_note(
                    f"claude did not complete a usable review across {attempts} "
                    f"attempt(s) — {severe_reason}", workdir, stdout_text))
            # Otherwise it's the genuine "zero assistant turns" case; name it and
            # point at the transcript so the next human knows it's a claude-didn't-run
            # problem, not a file-write or review-verdict problem.
            return self._fail(n, note=self._forensic_note(
                f"claude completed but produced no output across {attempts} attempt(s) "
                "— no recommendation file and empty stdout (likely zero assistant turns)",
                workdir, stdout_text))

        if not rec_text.strip():
            _LOG.warning(
                "review #%d: no %s written — using captured stdout as the "
                "recommendation (claude printed its answer but didn't write the file)",
                n, self._out_filename)
        return parse_recommendation(raw)

    # ── the FRESH fixer (#71) — never the resumed builder (the #60 lesson) ──────

    def _dispatch_fixer(self, issue: Issue, pr: int, rec: Recommendation):
        """Dispatch a FRESH fixer agent for an in-scope finding, scoped to the
        finding text + the PR's changed files. Returns ``None`` on a successful fix
        (the loop then re-reviews) or a HALT :class:`PipelineResult` if the fixer
        couldn't apply its fix.

        The fixer is STRUCTURALLY a brand-new agent: it is the injected
        ``fixer_dispatch`` seam — a SEPARATE callable from the reviewer's
        ``run_launcher`` AND from Gordon's builder launcher — so it can NEVER be the
        resumed original builder. Resuming the builder would re-ingest its whole
        transcript, costing ~the entire build again (the #60 cost lesson). The fixer
        gets only the finding + the 1–2 files (``build_fixer_prompt``) and is told
        to apply the minimal fix AND add a regression test that fails without it."""
        n = issue.number
        try:
            files = tuple(self._gw.get_pr_files(pr))
        except Exception as e:
            _LOG.warning("could not read PR #%d files for the fixer: %s", pr, e)
            files = ()
        # build_fixer_prompt is the production prompt; the fake fixer in tests does
        # not need it, but production wires it through the dispatch seam. Kept here
        # so the fresh-agent contract (finding + files only, no builder transcript)
        # is visible at the call site.
        _ = build_fixer_prompt(issue_number=n, finding=rec.text, files=files)
        try:
            fixed = self._fixer_dispatch(
                pr_number=pr, issue_number=n, finding=rec.text, files=files,
            )
        except Exception as e:
            return self._fail(n, note=f"fixer dispatch errored on PR #{pr}: {e}")
        if not fixed:
            return self._fail(
                n, note=f"fresh fixer could not apply a fix to PR #{pr}")
        try:
            self._gw.leave_bot_comment(
                n, self._bot_name,
                f"dispatched a fresh fixer for PR #{pr} — re-reviewing",
            )
        except Exception:
            _LOG.warning("could not leave fixer provenance comment on #%d", n)
        return None

    @staticmethod
    def _fallback_reject_reason(rec: Recommendation) -> str:
        """A reason for the red record when a route somehow carried none (defensive;
        route_review always supplies one). Mirrors the old #59 wording."""
        if rec.verdict == "reject":
            return "reviewed and recommends REJECT — needs a human decision"
        return "review verdict was unclear — needs a human decision"

    # ── forensic halt notes ─────────────────────────────────────────────────────

    @staticmethod
    def _forensic_note(reason: str, cwd, stdout_text: str) -> str:
        """A TRUTHFUL halt note: the reason, a tail of what claude actually printed
        (if anything), and a pointer to the on-disk transcript. So "why did it
        stop" is answerable from the note itself — no transcript archaeology, and
        no more "did not start" guesses masking a real cause. PURE-ish (best-effort
        transcript lookup; never raises)."""
        parts = [reason]
        tail = (stdout_text or "").strip()
        if tail:
            # Keep the note bounded — the full session lives in the transcript.
            parts.append(f"claude's output (tail): …{tail[-400:]}")
        else:
            parts.append("claude emitted no stdout")
        tpath = _resolve_transcript_for(cwd)
        if tpath:
            parts.append(f"full transcript: {tpath}")
        return " — ".join(parts)

    # ── selection ──────────────────────────────────────────────────────────────

    def _select_oldest(self) -> Optional[Issue]:
        """The OLDEST needs-review issue (smallest createdAt, ISO-8601 sorts
        lexicographically), or None when there are none."""
        issues = sorted(self._gw.list_needs_review_issues(), key=lambda i: i.created_at)
        return issues[0] if issues else None

    # ── failure path: HALT to needs-human (Slice F escape hatch) ───────────────

    def _fail(self, n: int, *, note: str) -> PipelineResult:
        """HALT the run: an item Kleiner can't review (no PR found, no/empty
        recommendation) genuinely needs a human, so flip needs-review →
        needs-human, post a 🤖 halt provenance comment, mark the run ``halted``
        (not failed), and don't transition to needs-signoff. This surfaces it on
        the dashboard's red pill instead of silently sitting in needs-review."""
        _LOG.warning("review of issue #%d halted: %s", n, note)
        report = HaltReport(
            agent_name=self._bot_name, issue_number=n, stage="review", reason=note)
        try:
            self._gw.set_labels(n, remove=[NEEDS_REVIEW_LABEL], add=[NEEDS_HUMAN_LABEL])
        except Exception:
            _LOG.exception("could not bounce issue #%d to needs-human", n)
        # Best-effort 🤖 halt provenance comment (the run's halt report is canonical).
        try:
            self._gw.leave_bot_comment(n, self._bot_name, _halt_sentence(report))
        except Exception:
            _LOG.warning("could not leave halt provenance comment on #%d", n)
        self._recorder.halt(n, report=format_halt_report(report))
        return PipelineResult(acted=True, issue_number=n, failed=True, reason=note)

    # ── red-flag path: a verdict that STOPS (REJECT / unclear) → needs-human ────

    def _reject(self, n: int, *, reason: str) -> PipelineResult:
        """Route a red-flag verdict to needs-human. Distinct from ``_fail``:
        ``_fail`` is "Kleiner COULDN'T review" (no PR / no recommendation); this
        is "Kleiner reviewed FINE but the verdict is a STOP" — a REJECT, or an
        unclear/missing verdict that — because this manages real capital — we
        treat as a stop rather than a silent pass.

        Mechanism mirrors ``_fail`` (flip needs-review → needs-human, post a 🤖
        halt provenance comment, record a 🔴 halt carrying the reason), but the
        stage is ``verdict`` and the reason text is the reject/unclear reason —
        so the run store's red record reads differently from a can't-review halt.
        The recommendation has ALREADY been posted to the PR by the caller; this
        only handles the routing + the red record."""
        _LOG.warning("review of issue #%d red-flagged: %s", n, reason)
        report = HaltReport(
            agent_name=self._bot_name, issue_number=n, stage="verdict", reason=reason)
        try:
            self._gw.set_labels(n, remove=[NEEDS_REVIEW_LABEL], add=[NEEDS_HUMAN_LABEL])
        except Exception:
            _LOG.exception("could not route issue #%d to needs-human", n)
        try:
            self._gw.leave_bot_comment(n, self._bot_name, _halt_sentence(report))
        except Exception:
            _LOG.warning("could not leave red-flag provenance comment on #%d", n)
        self._recorder.halt(n, report=format_halt_report(report))
        return PipelineResult(acted=True, issue_number=n, failed=True, reason=reason)
