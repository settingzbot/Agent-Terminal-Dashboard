"""
trident_kleiner_rework.py — the PURE route core of Kleiner's rework loop (Slice
of PRD #58, issue #71).

What this is
------------
Today (#59) Dr. Isaac Kleiner's verdict is TERMINAL: a clean ``APPROVE`` advances
the issue to ``approved``; ANYTHING else (a REJECT, an unclear verdict) red-flags
it to ``needs-human`` + 🔴. That dead-ends a *fixable* defect — a contained bug
like the #60 key-leak or the #62 ReorderDeps body-wipe — at a human, even though
a fresh agent could land a one-line fix with a regression test.

This slice gives Kleiner the same TIERED AUTHORITY Dr. Eli Vance got in #62
(``trident_eli_architect.decide_replan``): it splits "anything that isn't a clean
APPROVE" into FOUR routes, decided by a PURE function so every branch is
table-testable as data:

  * ``CLEAN``                       — APPROVE ⇒ advance (approved / needs-signoff).
  * ``IN_SCOPE_FIXABLE``            — a contained, in-scope finding ⇒ dispatch a
                                      FRESH fixer (NEVER the resumed builder), let
                                      it apply the fix WITH a regression test, then
                                      RE-REVIEW. Loop until clean.
  * ``OUT_OF_SCOPE_OR_MONEY_PATH``  — an out-of-scope / architectural finding, OR a
                                      diff that touches the live-trading surface ⇒
                                      STOP. NO auto-fix; route to ``needs-human``.
  * ``ROUND_BUDGET_EXHAUSTED``      — too many failed fix rounds on the same PR ⇒
                                      STOP. NO further auto-fix; route to
                                      ``needs-human``.

This is the Kleiner MIRROR of Eli's #62 in/out-of-scope split, and it's the exact
loop the #58 dress rehearsal ran by hand — it caught + auto-fixed two real bugs
green tests missed.

The money veto is STRUCTURAL, never the LLM's call (fail CLOSED)
---------------------------------------------------------------
Even if the review classifies a finding ``in_scope`` (fixable), if the PR's diff
touches the trading surface (``trident_trade_surface.classify_paths`` →
``.touches_trading_surface``) the route is FORCED to
``OUT_OF_SCOPE_OR_MONEY_PATH``. The money path ALWAYS stops; the structural
detector OVERRIDES the LLM's "fixable" claim — same fail-toward-the-wall bias as
#63. The LLM can never talk Kleiner into auto-patching the money path.

Fail SAFE on a malformed/unclear scope classification
-----------------------------------------------------
A non-approve verdict that carries no usable scope tag (a missing / typo'd
``SCOPE:`` line) is treated as ``out_of_scope`` ⇒ escalate. This mirrors
``scope_litmus``'s fail-safe and Kleiner's existing "unclear verdict = STOP": when
in doubt we send it to a human rather than auto-patch on a verdict we don't trust.

The FRESH-fixer contract (the #60 cost lesson)
----------------------------------------------
The fixer that lands an in-scope fix is ALWAYS a FRESH agent, structurally — NEVER
the resumed original builder. Resuming the builder would re-ingest its entire
transcript, costing ~the whole build again (the #60 lesson). The pipeline enforces
this by injecting a SEPARATE ``fixer_dispatch`` seam (its own callable, distinct
from the builder's ``run_launcher``); the route core here never resumes anything —
it only emits the routing decision. See ``trident_review_pipeline.ReviewPipeline``
for the dispatch + re-review orchestration.

Everything in this module is deterministic and side-effect-free (no I/O, no gh, no
git, no clock) so it round-trips through unit tests as data tables. Mirrors
``trident_eli_architect`` (``scope_litmus`` / ``decide_replan``).
"""

from __future__ import annotations

from dataclasses import dataclass

# The four routes Kleiner's rework decision can take. Kept as plain string
# constants (a tagged enum-of-strings, same spirit as IN_SCOPE/OUT_OF_SCOPE in
# trident_eli_architect) so the table tests read as data.
CLEAN = "clean"
IN_SCOPE_FIXABLE = "in_scope_fixable"
OUT_OF_SCOPE_OR_MONEY_PATH = "out_of_scope_or_money_path"
ROUND_BUDGET_EXHAUSTED = "round_budget_exhausted"

# The scope tags a non-approve review verdict may carry (parsed from the review's
# ``SCOPE:`` line). Re-exported from the parse layer's vocabulary; mirrors
# trident_eli_architect.IN_SCOPE / OUT_OF_SCOPE.
SCOPE_IN_SCOPE = "in_scope"
SCOPE_OUT_OF_SCOPE = "out_of_scope"
# A non-approve verdict with no usable scope tag ⇒ unclear ⇒ fail safe to escalate.
SCOPE_UNCLEAR = "unclear"

# Default ceiling on fix rounds before Kleiner stops auto-fixing and escalates.
# Bounded so a fix that won't converge can never loop forever (acceptance crit).
DEFAULT_MAX_ROUNDS = 2


@dataclass(frozen=True)
class ReworkRoute:
    """The PURE output of :func:`route_review`: which of the four routes Kleiner
    takes for a reviewed PR, plus a plain-English ``reason`` for the audit trail /
    🔴 pill. Frozen so a built route can't be mutated out from under the pipeline.

    ``route`` is one of CLEAN / IN_SCOPE_FIXABLE / OUT_OF_SCOPE_OR_MONEY_PATH /
    ROUND_BUDGET_EXHAUSTED. Convenience predicates below make the pipeline read
    cleanly without re-stringly-typing the constant at every branch."""

    route: str
    reason: str = ""

    @property
    def is_clean(self) -> bool:
        return self.route == CLEAN

    @property
    def is_fixable(self) -> bool:
        return self.route == IN_SCOPE_FIXABLE

    @property
    def is_escalate(self) -> bool:
        """True for either STOP route — out-of-scope/money-path or budget
        exhausted. Both route to ``needs-human``; the pipeline treats them the
        same (escalate), only the ``reason`` differs."""
        return self.route in (OUT_OF_SCOPE_OR_MONEY_PATH, ROUND_BUDGET_EXHAUSTED)


def route_review(
    *,
    verdict: str,
    scope: str,
    touches_money_path: bool,
    round_count: int,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> ReworkRoute:
    """Route a reviewed PR into one of the FOUR rework routes. PURE — no I/O.

    This is the heart of the slice: the tiered authority (clean = advance,
    in-scope fixable = auto-fix, out-of-scope/money-path = STOP, budget exhausted
    = STOP) is decided HERE, as pure data routing, so every branch is
    table-testable without touching gh/git/claude.

    Inputs:
      * ``verdict``           — the parsed review verdict ("approve" / "reject" /
                                "unclear"; ``Recommendation.verdict``).
      * ``scope``             — the scope tag parsed from a non-approve review's
                                ``SCOPE:`` line ("in_scope" / "out_of_scope" /
                                "unclear"). Ignored for an APPROVE verdict.
      * ``touches_money_path``— the STRUCTURAL money-path signal: the PR's diff
                                touches the trading surface
                                (``classify_paths(...).touches_trading_surface``).
                                Decided by the detector, NEVER trusted to the LLM.
      * ``round_count``       — how many fix rounds have already been spent on this
                                PR (0 on the first review).
      * ``max_rounds``        — the ceiling (default 2). At/over it, no further
                                auto-fix is allowed.

    Decision order (the precedence is load-bearing):

      1. **APPROVE ⇒ CLEAN.** The verdict is clean; advance. (A clean verdict
         never carries a scope tag and never auto-fixes.)
      2. **MONEY-PATH VETO ⇒ OUT_OF_SCOPE_OR_MONEY_PATH.** Checked BEFORE the
         scope tag, and it OVERRIDES it: even a verdict the LLM marked
         ``in_scope`` (fixable) escalates if the diff touches the trading surface.
         The money path always stops; the structural detector beats the LLM
         (fail CLOSED, the #63 bias).
      3. **OUT-OF-SCOPE / UNCLEAR SCOPE ⇒ OUT_OF_SCOPE_OR_MONEY_PATH.** An
         out-of-scope / architectural finding, OR a non-approve verdict with no
         usable scope tag (fail-safe: when we can't tell, treat as out-of-scope
         and escalate — same bias as ``scope_litmus`` and Kleiner's "unclear =
         STOP").
      4. **ROUND BUDGET EXHAUSTED ⇒ ROUND_BUDGET_EXHAUSTED.** The finding is
         in-scope AND off the money path, but we've already spent the budget of
         fix rounds without converging. Stop auto-fixing; escalate. Checked AFTER
         the money/scope STOPs so a money-path finding never reads as a mere
         "ran out of rounds".
      5. **Otherwise ⇒ IN_SCOPE_FIXABLE.** In-scope, off the money path, under the
         round budget: dispatch a FRESH fixer and re-review.
    """
    v = (verdict or "").strip().lower()
    if v == "approve":
        return ReworkRoute(route=CLEAN, reason="clean APPROVE — advance")

    # From here on the verdict is non-approve (reject / unclear): a finding exists.

    # 2) Money-path veto — STRUCTURAL, overrides whatever the LLM said about scope.
    #    Fail CLOSED: the money path always stops, no auto-fix, no exceptions.
    if touches_money_path:
        return ReworkRoute(
            route=OUT_OF_SCOPE_OR_MONEY_PATH,
            reason=(
                "the PR's diff touches the live-trading surface — the money path "
                "always stops; refusing auto-fix and escalating to a human "
                "(structural veto overrides the review's scope call)"
            ),
        )

    # 3) Scope tag (fail SAFE on a missing/unclear tag ⇒ out-of-scope ⇒ escalate).
    s = (scope or "").strip().lower()
    if s != SCOPE_IN_SCOPE:
        if s == SCOPE_OUT_OF_SCOPE:
            reason = (
                "the finding is out-of-scope / architectural — not a contained "
                "fixable defect; escalating to a human (no auto-fix)"
            )
        else:
            reason = (
                f"the review carried no clear scope tag (scope={scope!r}) — "
                "treating as out-of-scope and escalating for a human decision "
                "(fail-safe; an unclear classification is a STOP)"
            )
        return ReworkRoute(route=OUT_OF_SCOPE_OR_MONEY_PATH, reason=reason)

    # 4) In-scope + off the money path, but the fix-round budget is spent.
    if round_count >= max_rounds:
        return ReworkRoute(
            route=ROUND_BUDGET_EXHAUSTED,
            reason=(
                f"the auto-fix budget is exhausted ({round_count}/{max_rounds} "
                "rounds) without a clean verdict — stopping the rework loop and "
                "escalating to a human (bounded; never loop forever)"
            ),
        )

    # 5) In-scope, off the money path, under budget ⇒ dispatch a FRESH fixer.
    return ReworkRoute(
        route=IN_SCOPE_FIXABLE,
        reason=(
            "the finding is a contained, in-scope defect off the money path — "
            "dispatching a FRESH fixer (never the resumed builder; the #60 cost "
            "lesson) to apply the fix with a regression test, then re-reviewing"
        ),
    )


def build_fixer_prompt(
    *,
    issue_number: int,
    finding: str,
    files: tuple[str, ...] = (),
) -> str:
    """Build the scoped fix prompt for the FRESH fixer agent. PURE.

    The fixer is a BRAND-NEW agent (never the resumed builder — the #60 cost
    lesson: re-ingesting the builder's transcript costs ~the whole build again).
    It gets ONLY the finding text + the 1–2 named files, and is told to apply the
    MINIMAL fix AND add a regression test that FAILS without the fix. It does NOT
    get the builder's history; the finding is the whole world it needs.

    ``finding`` is Kleiner's review text (why the PR is wrong); ``files`` are the
    1–2 file paths the fix should be scoped to (named so the fixer doesn't wander
    the repo). Kept pure so the prompt is table-testable as data.
    """
    finding_txt = (finding or "").strip() or "(no finding text provided)"
    if files:
        files_line = (
            "Scope your change to ONLY these files (do not touch anything else):\n"
            + "\n".join(f"  - {f}" for f in files)
            + "\n\n"
        )
    else:
        files_line = ""
    return (
        f"You are a FRESH fixer agent for the Trident repo, dispatched to fix ONE "
        f"contained defect that the reviewer (Dr. Isaac Kleiner) found in the pull "
        f"request that closes issue #{issue_number}. You are a brand-new agent — "
        f"you have NONE of the original builder's context, and you do not need it. "
        f"Everything you need is the finding below.\n\n"
        f"THE FINDING (what is wrong and must be fixed):\n{finding_txt}\n\n"
        f"{files_line}"
        "YOUR JOB:\n"
        "  1. Apply the MINIMAL fix for exactly this finding — nothing more. Do "
        "NOT refactor, re-scope, or 'improve' anything beyond the bug.\n"
        "  2. Add a REGRESSION TEST that FAILS without your fix and PASSES with it "
        "— it must prove the specific bug is closed, not just exercise the code.\n"
        "  3. Stay off the live-trading surface entirely (this is a contained, "
        "in-scope fix; if the real fix needs the money path, STOP and say so "
        "instead of touching it).\n"
        "  4. When done, ensure the working tree is committed so the orchestrator "
        "can update the PR from the branch.\n"
    )
