"""
trident_eli_architect.py — the PURE cores of Eli's architectural call (Slice 3 of
PRD #58, issue #62).

What this is
------------
After Dr. Eli Vance LANDS a slice (``trident_eli_pipeline.EliPipeline``), Eli
turns to the REMAINING open child issues of that slice's parent PRD and asks a
harder question than "did this PR pass review?": **do the remaining issues still
make sense against the PRD — the IMMUTABLE north star?** A merge can make a
sibling issue obsolete, reorder a dependency, or reveal that the work has drifted
past the PRD's product scope entirely.

This module owns ONLY the pure, table-testable half of that decision — the same
"LLM produces a structured verdict; a PURE core routes it" split Kleiner uses
(``parse_recommendation`` → verdict; the pipeline routes it). Here:

  * :class:`ArchAssessment` — the LLM's structured output (frozen dataclass): a
    ``classification`` ("in_scope" / "out_of_scope"), a short ``rationale``, and
    — for in_scope — a list of proposed MECHANICAL ``actions`` (small tagged
    records). Mirrors how ``Recommendation`` / ``ReviewRecord`` carry Kleiner's
    verdict. The actions are PROPOSALS; the pipeline (with the injected gateway)
    is what EXECUTES them.
  * :class:`ScopeVerdict` — the result of the scope litmus.
  * :func:`scope_litmus` — pure classification/routing of an assessment into a
    ScopeVerdict.
  * :func:`decide_replan` — pure: in_scope ⇒ a :class:`ReplanPlan` (the concrete,
    ordered actions Eli will execute autonomously); out_of_scope OR a malformed/
    unclear assessment ⇒ a :class:`Freeze` (STOP — raise the 🔴 architecture pill
    and freeze the loop until the PM resolves it).

Tiered authority (the design this slice encodes)
------------------------------------------------
  * **In-scope (mechanical) re-planning is AUTONOMOUS.** Eli may rewrite
    acceptance criteria (``EditIssue``), reorder/adjust dependencies
    (``ReorderDeps``), close obsolete issues (``CloseObsolete``), and OPEN new
    ``ready-for-agent`` issues (``OpenIssue``). Each carries a ``Parent #<PRD>``
    ref + a 🤖 audit comment when executed.
  * **Out-of-scope (semantic drift past the PRD's product scope) is a STOP.** Eli
    does NOT improvise a redesign — it FREEZES the whole loop (no new claims) and
    surfaces a 🔴 architecture pill for the PM to resolve in a session.
  * The PRD itself is **NEVER** edited by Eli — every action targets a CHILD
    issue (edit / close / open). That invariant lives in the action vocabulary
    here (no action can target the PRD as an edit/close subject — the pipeline
    only ever feeds child-issue numbers) and is asserted by the pipeline tests.

Fail SAFE (real capital)
------------------------
A malformed, contradictory, or unclear assessment is treated as ``out_of_scope``
⇒ ``Freeze``. When in doubt, freeze and ask the human rather than autonomously
re-planning on a verdict we don't trust. This mirrors Kleiner treating an unclear
review verdict as a STOP, not a pass.

Everything here is deterministic and side-effect-free (no I/O, no gh, no git, no
clock) so it round-trips through unit tests as data tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

# The two classifications the LLM may emit. Anything else is malformed ⇒ freeze.
IN_SCOPE = "in_scope"
OUT_OF_SCOPE = "out_of_scope"
_VALID_CLASSIFICATIONS = frozenset({IN_SCOPE, OUT_OF_SCOPE})


# ═══════════════════════════════════════════════════════════════════════════════
# Action vocabulary — the small tagged records an in_scope assessment proposes
# ═══════════════════════════════════════════════════════════════════════════════
#
# Each is a frozen dataclass (a "tagged record"): the pipeline pattern-matches on
# the concrete type to choose the gateway call to make. Every action targets a
# CHILD issue — never the PRD. ``OpenIssue`` is the one that CREATES a child
# (Eli, unlike Gordon's worker #55, MAY stamp ready-for-agent here — that's the
# whole point of this slice). The others mutate existing children.

@dataclass(frozen=True)
class EditIssue:
    """Rewrite a child issue's body (e.g. new acceptance criteria). ``number`` is
    the child issue; ``body`` is the full replacement body. NEVER the PRD."""

    number: int
    body: str


@dataclass(frozen=True)
class ReorderDeps:
    """Adjust a child issue's ``Blocked by`` dependencies. ``number`` is the
    child issue; ``blocked_by`` is the new ordered list of issue numbers it should
    declare it is blocked by. Executed as a body-rewrite of the issue's
    dependency section (the gateway turns this into the concrete edit), so it is
    a SIBLING of EditIssue kept distinct for audit clarity — a reorder reads
    differently from an acceptance-criteria rewrite in the provenance trail."""

    number: int
    blocked_by: tuple[int, ...] = ()


@dataclass(frozen=True)
class CloseObsolete:
    """Close a child issue the just-landed work made obsolete. ``number`` is the
    child; ``reason`` is the plain-English why (for the 🤖 audit comment). The
    gh close reason is always "not planned" — an obsoleted-by-redesign issue was
    NOT completed, so it must not read as "done" (distinct from Eli's finalize
    close, which is "completed")."""

    number: int
    reason: str = "obsoleted by the just-landed work"


@dataclass(frozen=True)
class OpenIssue:
    """Open a NEW child issue. ``title`` / ``body`` are its content; the pipeline
    stamps ``ready-for-agent`` + a ``Parent #<PRD>`` ref + a 🤖 audit comment.

    This is the one action that CREATES work. Eli is explicitly permitted to apply
    ``ready-for-agent`` here (the inverse of Gordon's #55 self-promote guard) —
    but ONLY in-scope and ONLY with the audit trail, both enforced by routing
    every OpenIssue through ``decide_replan`` (in_scope only) and the pipeline's
    audit step."""

    title: str
    body: str


# The union of every mechanical action an in_scope re-plan may propose.
Action = Union[EditIssue, ReorderDeps, CloseObsolete, OpenIssue]


# ═══════════════════════════════════════════════════════════════════════════════
# Body surgery — swap ONLY the "## Blocked by" section, preserve the rest
# ═══════════════════════════════════════════════════════════════════════════════
#
# A ``ReorderDeps`` must change an issue's dependency section WITHOUT touching its
# "What to build" / "Acceptance criteria" / any other section. The #62 data-loss
# bug was rendering ONLY the blocked-by section and feeding that as the whole body
# to ``edit_issue`` (a full-body replace) — every other section was destroyed.
#
# This is the pure read-MODIFY-write core: given the issue's CURRENT body and the
# new ordered ``blocked_by`` tuple, return a new body that is identical except for
# the ``## Blocked by`` section. It lives here (not in the pipeline) so it is
# cleanly table-testable as data with no I/O.
#
# Heading contract: the section is a markdown ``## Blocked by`` header followed by
# ``- Blocked by #N`` bullet lines — the exact phrasing
# ``trident_github_gateway._BLOCKED_BY_RE`` / ``parse_blocked_by`` reads, so the
# rewritten section still round-trips through the dependency parser.

# The canonical heading + bullet phrasing (kept in lock-step with the gateway's
# _BLOCKED_BY_RE so the rewritten section still parses via parse_blocked_by).
_BLOCKED_BY_HEADING = "## Blocked by"

# Matches an existing "## Blocked by" section: the heading line through to (but not
# including) the next ATX heading (``#``-prefixed line) or end-of-body. Multiline so
# ``^`` anchors each line; DOTALL so the body of the section (the bullet lines) is
# swallowed. The heading match is case-insensitive and tolerant of trailing space.
_BLOCKED_BY_SECTION_RE = re.compile(
    r"(?im)^[ \t]*##[ \t]+blocked[ \t]+by[ \t]*$.*?(?=^[ \t]*#|\Z)",
    re.DOTALL,
)


def _render_blocked_by_section(blocked_by: tuple[int, ...]) -> str:
    """The ``## Blocked by`` section body for ``blocked_by`` — heading + one
    ``- Blocked by #N`` bullet per dep, trailing newline. Empty tuple ⇒ "" (the
    caller drops the section entirely). PURE.

    Phrasing matches the gateway's ``_BLOCKED_BY_RE`` so the section round-trips
    through ``parse_blocked_by``."""
    nums = [int(n) for n in (blocked_by or ())]
    if not nums:
        return ""
    lines = "\n".join(f"- Blocked by #{n}" for n in nums)
    return f"{_BLOCKED_BY_HEADING}\n{lines}\n"


def replace_blocked_by_section(body: str, blocked_by: tuple[int, ...]) -> str:
    """Return ``body`` with ONLY its ``## Blocked by`` section swapped for the new
    ordered ``blocked_by`` deps — every other section, and their order, is left
    byte-for-byte intact. PURE (no I/O); the read-modify-write core of a
    ``ReorderDeps`` (fixes the #62 full-body-wipe data-loss bug).

    Three cases:

      * ``body`` HAS a ``## Blocked by`` section ⇒ its contents are replaced in
        place (position preserved). If ``blocked_by`` is empty the section is
        REMOVED (a reorder to "no blockers"), leaving the surrounding text intact.
      * ``body`` has NO such section and ``blocked_by`` is non-empty ⇒ the section
        is INSERTED. Insert position: APPENDED as a trailing section (deterministic
        + non-destructive — it never splits an existing section, and a trailing
        deps block matches how the issue template places it).
      * No section and empty ``blocked_by`` ⇒ ``body`` returned unchanged.

    The rewritten section uses the canonical ``- Blocked by #N`` phrasing so it
    still parses via ``trident_github_gateway.parse_blocked_by`` (parse contract
    preserved)."""
    base = body or ""
    new_section = _render_blocked_by_section(blocked_by)

    if _BLOCKED_BY_SECTION_RE.search(base):
        # Existing section: replace in place (or drop it when blocked_by is empty).
        if not new_section:
            # Remove the section, then tidy the seam so we don't leave a run of
            # blank lines where the section used to be.
            without = _BLOCKED_BY_SECTION_RE.sub("", base)
            return _collapse_blank_runs(without)
        # The matched span ran up to the next heading / EOF; it may have eaten the
        # blank-line gap before that heading, so re-pad to one blank line.
        replaced = _BLOCKED_BY_SECTION_RE.sub(
            lambda _m: new_section.rstrip("\n") + "\n", base, count=1
        )
        return _collapse_blank_runs(replaced)

    # No existing section.
    if not new_section:
        return base
    base_stripped = base.rstrip()
    if not base_stripped:
        return new_section
    return f"{base_stripped}\n\n{new_section}"


def _collapse_blank_runs(text: str) -> str:
    """Collapse any run of 3+ newlines down to a paragraph break (two newlines),
    so removing/replacing a section never leaves a widening gap. Preserves leading
    content; ensures a single trailing newline when there was any content."""
    collapsed = re.sub(r"\n{3,}", "\n\n", text)
    # Normalise the very start/end: no leading blank lines, exactly one trailing \n
    # if there is content.
    collapsed = collapsed.lstrip("\n")
    if collapsed.strip():
        return collapsed.rstrip("\n") + "\n"
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# The LLM's structured output — the assessment (mirrors Recommendation)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ArchAssessment:
    """Eli's structured architectural assessment of the remaining child issues
    against the parent PRD. The LLM produces this; the PURE cores below route it.

    Frozen so a built assessment can't be mutated out from under the router.

    Attributes:
        classification: "in_scope" (mechanical re-planning is allowed) or
            "out_of_scope" (semantic drift past the PRD's product scope ⇒ STOP).
            ANY other value is treated as malformed ⇒ Freeze (fail safe).
        rationale: A short plain-English why, for the PM and the audit trail.
        actions: The proposed mechanical actions — ONLY meaningful for in_scope.
            Each is a tagged ``Action`` record. Empty is legal (an in_scope
            assessment that finds nothing to change is a clean no-op re-plan).
        parent: The PRD issue number this assessment is about (the north star).
            Stamped onto every opened/edited child as ``Parent #<parent>``.
    """

    classification: str
    rationale: str = ""
    actions: tuple[Action, ...] = field(default_factory=tuple)
    parent: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Scope litmus — pure classification/routing
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ScopeVerdict:
    """The result of the scope litmus: is this assessment safe to act on
    autonomously, or is it a STOP?

    ``in_scope`` True ⇒ mechanical re-planning is authorized (decide_replan will
    build a ReplanPlan). False ⇒ a STOP (decide_replan returns a Freeze). ``reason``
    is the plain-English why — for the 🔴 architecture pill when it's a STOP."""

    in_scope: bool
    reason: str = ""


def scope_litmus(assessment: ArchAssessment) -> ScopeVerdict:
    """Classify an assessment into a :class:`ScopeVerdict`. PURE — no I/O.

    The litmus is the PRD's product-scope alignment as the LLM judged it:

      * ``classification == "in_scope"`` ⇒ authorized (mechanical re-planning).
      * ``classification == "out_of_scope"`` ⇒ a STOP (semantic drift).
      * ANYTHING ELSE (empty, typo, None-ish, an unexpected value) ⇒ a STOP,
        treated as out-of-scope. This is the FAIL-SAFE: an assessment we can't
        trust must freeze and ask the human, never autonomously re-plan — this
        manages real capital. Mirrors Kleiner treating an unclear verdict as a
        stop rather than a silent pass.
    """
    classification = (assessment.classification or "").strip().lower()
    if classification == IN_SCOPE:
        return ScopeVerdict(
            in_scope=True,
            reason=(assessment.rationale or "").strip()
            or "in scope — mechanical re-planning authorized",
        )
    if classification == OUT_OF_SCOPE:
        return ScopeVerdict(
            in_scope=False,
            reason=(assessment.rationale or "").strip()
            or "out of scope — semantic drift past the PRD's product scope",
        )
    # Malformed / unclear ⇒ fail safe to a STOP.
    return ScopeVerdict(
        in_scope=False,
        reason=(
            f"unclear architectural assessment "
            f"(classification={assessment.classification!r}) — treating as "
            f"out-of-scope and freezing for a human decision"
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# The architectural decision — in_scope ⇒ ReplanPlan; out_of_scope ⇒ Freeze
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ReplanPlan:
    """The concrete, ordered mechanical actions Eli will execute autonomously for
    an in_scope assessment. ``actions`` preserves the assessment's order so the
    audit trail reads in the order Eli intended. ``parent`` is the PRD the actions
    are re-planning under — stamped as ``Parent #<parent>`` on every open/edit."""

    parent: int
    actions: tuple[Action, ...] = field(default_factory=tuple)
    rationale: str = ""


@dataclass(frozen=True)
class Freeze:
    """A STOP: the loop must FREEZE (no new claims) until the PM resolves the
    architectural question in a session. Carries the plain-English ``reason`` for
    the 🔴 architecture pill, and the ``parent`` PRD that triggered it. This is a
    DISTINCT state from "Nathan paused the loop" — the pipeline records it as an
    architecture freeze, not a manual pause."""

    reason: str
    parent: int = 0


# The two outcomes of the architectural decision.
ArchDecision = Union[ReplanPlan, Freeze]


def decide_replan(assessment: ArchAssessment) -> ArchDecision:
    """Route an assessment to its architectural decision. PURE — no I/O.

    Runs the scope litmus, then:

      * in_scope ⇒ a :class:`ReplanPlan` carrying the assessment's actions, in
        order, under its parent PRD. (Zero actions is legal — a clean no-op
        re-plan that still records "Eli looked and found nothing to change".)
      * out_of_scope OR malformed/unclear ⇒ a :class:`Freeze` carrying the
        litmus reason. NO actions are returned on the freeze path — the loop
        stops and waits for the human; Eli does not improvise a redesign.

    This is the heart of the slice: the tiered authority (mechanical = autonomous,
    semantic drift = STOP) is decided HERE, as pure data routing, so every branch
    is table-testable without touching gh/git.
    """
    verdict = scope_litmus(assessment)
    if not verdict.in_scope:
        return Freeze(reason=verdict.reason, parent=assessment.parent)
    return ReplanPlan(
        parent=assessment.parent,
        actions=tuple(assessment.actions),
        rationale=verdict.reason,
    )
