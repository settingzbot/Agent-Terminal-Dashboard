"""
trident_trade_surface.py — the trading-surface WALL: Eli's pure pre-integration
guard over a diff's paths (Slice of PRD #58, issue #63).

What this is
------------
Before Dr. Eli Vance LANDS an approved change (``trident_eli_pipeline.EliPipeline``),
it INSPECTS the change's diff. Most changes (docs, tests, the dashboard React
source, a research script) are safe to auto-integrate. But a change that touches
the **live-trading surface** — where real capital moves — must NOT auto-land: Eli
refuses and raises a 🟣 trading-surface (trade-approval) pill (the
``needs-trade-approval`` state) carrying a plain-English brief, and waits for the
PM to approve. (🟣 purple is the trade-approval pill in the four-pill design; 🔴 red
is reserved for problems/halts.) A change that touches **indicator math** must stay
in bot/backtest PARITY, so it is gated on a baseline-backtest re-run before it can
land.

This module owns ONLY the PURE, table-testable half of that wall — a deterministic
classification over a list of diff PATHS, plus the pure routing of that
classification (against the issue's approval labels) into a proceed/block decision.
No I/O, no gh, no git, no clock. The EliPipeline supplies the paths (via the
gateway's ``get_pr_files`` seam) and EXECUTES the decision; it performs NO
classification itself. Same "pure core + pipeline executes" split the rest of Eli
uses (``decide_replan`` / ``ArchitectPipeline``).

The two surfaces (grounded in CLAUDE.md's ground rules)
-------------------------------------------------------
  * **Trading surface (the money path).** A touch here can change live order
    submission, position state, the strategy's trade decisions, the live runtime
    config (sizing/params/mode), or the Lighter API key. Eli REFUSES to
    auto-integrate and raises the ``needs-trade-approval`` pill — the PM reads the
    brief and, if it is safe to trade, applies ``trade-approved`` to clear the wall.
    Matching is by FAMILY (a prefix per module family), not an exact filename — so a
    NEW or renamed file in a money-path family (a future ``trident_execution_v3.py``,
    ``trident_lighter_ws.py``) is caught, not silently auto-landed. Families:
        - ``trident_execution*``     — the execution layer (OTOCO, trail) + future
        - ``trident_lighter*``       — live order submission to Lighter.xyz + future
        - ``trident_position*``      — open-position state
        - ``trident_signal*``        — entry/exit DECISIONS (the trade trigger)
        - ``trident_filters*``       — the shared live/backtest trade gates
        - ``trident_htf*``           — HTF grading (strategy sizing/gating input)
        - ``trident_secrets*``       — the Lighter API key (OS keyring)
        - ``trident_bot.py``         — orchestrator: bar driver hook + trade paths
                                       (EXACT — there is one bot orchestrator file)
        - ``dashboard/routes_position*`` — ``/api/position`` close / modify-sl / modify-tp
        - ``dashboard/routes_bot.py``    — bot lifecycle: starts/stops the live bot
        - ``scripts/deploy_config.py``   — pushes strategy params live
        - ``trident_config.default.json`` — the TRACKED strategy config (sizing/params)
  * **Indicator math (the parity surface).** Changing ``IndicatorEngine`` math can
    silently break bot↔backtest parity. Per CLAUDE.md, the baseline backtest must be
    re-run before the change lands. Eli gates it on ``needs-baseline-backtest`` until
    ``parity-checked`` is applied.
        - ``trident_indicator_engine*``  — ``IndicatorEngine`` (the parity-critical math)

Precedence: the money gate (trade-approval) is checked FIRST, then the parity gate.
A diff that touches both is held at the money wall until ``trade-approved``, then at
the parity wall until ``parity-checked`` — two deliberate, separate human clearances.

Fail toward the wall (real capital)
-----------------------------------
The classifier matches by module FAMILY (a prefix per family), so it fails CLOSED:
a NEW or renamed file in a money-path family (a future ``trident_execution_v3.py``,
``trident_lighter_ws.py``, ``trident_position_tracker.py``) is flagged and waits for
a human, rather than slipping through an exact-filename allowlist and auto-landing.
The design bias is to make the trading surface BROAD (signal/filters/grade included,
not just the raw exchange call) — a money-path change auto-landing is far worse than
the PM glancing at a change that turned out benign. When in doubt, the change waits.

The families are a DISJOINT namespace from the autonomous loop's own tooling
(``trident_agent_*``, ``trident_eli_*``, ``trident_github_gateway``,
``trident_loop_gate``, ``trident_review_pipeline``, ``trident_trade_surface``) — the
wall must NOT flag the pipeline's own PRs, or every agent change would false-positive
at the money wall. The test suite proves this disjointness explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from shared.github_gateway import (
    NEEDS_TRADE_APPROVAL_LABEL,
    TRADE_APPROVED_LABEL,
    NEEDS_BASELINE_BACKTEST_LABEL,
    PARITY_CHECKED_LABEL,
)

# ═══════════════════════════════════════════════════════════════════════════════
# The two surface path sets (repo-relative, forward-slashed — gh's diff convention)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Two kinds of pattern, each matched against a path's TAIL (the path normalised to
# forward slashes, equal to the pattern's base or ending in "/" + that base, so a
# repo-relative or sub-prefixed/worktree path both resolve):
#
#   * a FAMILY pattern ends in "*" (e.g. "trident_execution*") and matches any
#     basename starting with that prefix — so NEW files in a money-path family
#     (a future trident_execution_v3.py) are caught. This is what makes the wall
#     fail CLOSED on unknowns.
#   * an EXACT pattern is a literal filename (e.g. "trident_bot.py",
#     "scripts/deploy_config.py") matched on the tail — used where the family would
#     over-reach and there is provably exactly one file (the single bot orchestrator,
#     the one config-deploy script).
#
# Kept as flat tuples so the wall is auditable at a glance.

# Money path — a touch here moves real capital or its keys ⇒ needs PM trade approval.
# FAMILY (prefix) patterns end in "*"; the rest are EXACT tails.
TRADING_SURFACE_PATHS: tuple[str, ...] = (
    "trident_execution*",            # execution layer (OTOCO, trail) + any future file
    "trident_lighter*",              # live order submission to Lighter.xyz + future
    "trident_position*",             # open-position state
    "trident_signal*",               # entry/exit DECISIONS — the trade trigger
    "trident_filters*",              # the shared live/backtest trade gates
    "trident_htf*",                  # HTF grade — strategy sizing/gating input
    "trident_secrets*",              # the Lighter API key (OS keyring)
    "trident_bot.py",                # orchestrator: bar driver hook + trade paths (EXACT)
    "dashboard/routes_position*",    # /api/position close / modify-sl / modify-tp
    "dashboard/routes_bot.py",       # bot lifecycle: starts/stops the live bot (EXACT)
    "scripts/deploy_config.py",      # pushes strategy params live (EXACT)
    "trident_config.default.json",   # the TRACKED strategy config (sizing/params) (EXACT)
)

# Indicator math — must stay in bot/backtest PARITY ⇒ baseline-backtest gate.
INDICATOR_MATH_PATHS: tuple[str, ...] = (
    "trident_indicator_engine*",     # IndicatorEngine — the parity-critical math + future
)


def _norm(path: str) -> str:
    """Normalise a diff path: strip whitespace, back→forward slashes, drop a
    leading ``./``. PURE. (gh emits forward slashes even on Windows, but a caller
    may hand us a Windows path or a ``./`` prefix.)"""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _tail_matches(path: str, base: str) -> bool:
    """True iff normalised ``path`` IS ``base`` or sits at ``…/<base>``. PURE.
    ``base`` is an exact tail (a filename or ``dir/filename``)."""
    p = _norm(path)
    return p == base or p.endswith("/" + base)


def _matches(path: str, pattern: str) -> bool:
    """True iff ``path`` matches ``pattern``. PURE.

    A FAMILY pattern (ends in ``*``) matches when the path's final component starts
    with the prefix — i.e. the tail at the pattern's directory level begins with the
    stripped prefix. An EXACT pattern matches the path's tail literally.

    Family example: ``trident_execution*`` matches ``trident_execution_v2.py`` and a
    future ``trident_execution_v3.py``, but NOT ``trident_agent_runner.py``."""
    if pattern.endswith("*"):
        prefix = pattern[:-1]                       # e.g. "trident_execution" or "dashboard/routes_position"
        p = _norm(path)
        # Anchor at a path boundary: the prefix must begin the whole path or sit
        # right after a "/". This catches a repo-relative path, a sub-prefixed /
        # worktree path, and a dir-qualified family alike, while never leaking a
        # bare prefix across a directory boundary (the "/" anchor enforces it).
        return p.startswith(prefix) or ("/" + prefix) in p
    return _tail_matches(path, pattern)


def _hits(path: str, patterns: Iterable[str]) -> bool:
    return any(_matches(path, pat) for pat in patterns)


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving de-dupe (a diff can list a path more than once)."""
    seen: list[str] = []
    for it in items:
        if it not in seen:
            seen.append(it)
    return tuple(seen)


# ═══════════════════════════════════════════════════════════════════════════════
# The surface verdict — pure classification of a diff's paths
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SurfaceVerdict:
    """What surfaces a diff touches. The PURE output of :func:`classify_paths`.

    Carries the exact paths that matched each surface (not just booleans) so the
    plain-English PM brief can NAME the money-path / indicator files. Frozen so a
    built verdict can't be mutated out from under the router."""

    trading_surface_paths: tuple[str, ...] = field(default_factory=tuple)
    indicator_math_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def touches_trading_surface(self) -> bool:
        return bool(self.trading_surface_paths)

    @property
    def touches_indicator_math(self) -> bool:
        return bool(self.indicator_math_paths)

    @property
    def is_clean(self) -> bool:
        """True iff the diff touches NEITHER surface — safe to auto-integrate."""
        return not (self.trading_surface_paths or self.indicator_math_paths)


def classify_paths(paths: Iterable[str]) -> SurfaceVerdict:
    """Classify a diff's changed PATHS against the trading + indicator surfaces.
    PURE — no I/O.

    A path may hit both surfaces in principle (it does not today — the two sets are
    disjoint), so each is collected independently. Order-preserving and de-duped so
    the brief reads in the diff's order. The empty/clean case (docs, tests, the
    React source, scripts) returns an all-empty verdict ⇒ ``is_clean``.
    """
    trading: list[str] = []
    indicator: list[str] = []
    for raw in paths or ():
        norm = _norm(raw)
        if not norm:
            continue
        if _hits(norm, TRADING_SURFACE_PATHS):
            trading.append(norm)
        if _hits(norm, INDICATOR_MATH_PATHS):
            indicator.append(norm)
    return SurfaceVerdict(
        trading_surface_paths=_dedupe(trading),
        indicator_math_paths=_dedupe(indicator),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Plain-English briefs (the PM-facing why + what-to-do — no jargon)
# ═══════════════════════════════════════════════════════════════════════════════

def format_trade_approval_brief(verdict: SurfaceVerdict) -> str:
    """The plain-English brief for a money-path refusal. PURE.

    Names the live-trading files that changed and tells the PM exactly how to
    clear the wall. No code, no jargon — a non-coding PM can act on it."""
    files = ", ".join(verdict.trading_surface_paths) or "the live-trading surface"
    return (
        "This change touches the live-trading surface (the money path): "
        f"{files}. Eli will NOT auto-integrate a change that can move real "
        f"capital or its keys. Review the diff and, if it is safe to trade, "
        f"apply the '{TRADE_APPROVED_LABEL}' label to clear the wall — Eli will "
        "then land it the normal way."
    )


def format_parity_brief(verdict: SurfaceVerdict) -> str:
    """The plain-English brief for an indicator-math (parity) gate. PURE."""
    files = ", ".join(verdict.indicator_math_paths) or "indicator math"
    return (
        f"This change touches indicator math ({files}). The bot and the backtest "
        "must stay in parity, so the baseline backtest has to be re-run and parity "
        f"confirmed before it lands. Once that's done, apply the "
        f"'{PARITY_CHECKED_LABEL}' label and Eli will land it."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# The integration decision — proceed, or block at the money / parity wall
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class IntegrationDecision:
    """The pre-merge gate decision. PURE output of :func:`decide_integration`.

    ``proceed`` True ⇒ Eli may land via the normal Slice 2a path. False ⇒ Eli
    bounces the issue to ``state_label`` and raises a pill carrying ``reason`` (a
    short headline for the run record) + ``brief`` (the full PM-facing text posted
    as the issue comment). ``stage`` tags the pill so the dashboard reads it as a
    trade-approval refusal vs a parity gate, not a build/merge halt."""

    proceed: bool
    state_label: str = ""
    stage: str = ""
    reason: str = ""
    brief: str = ""


def decide_integration(
    verdict: SurfaceVerdict, labels: Iterable[str]
) -> IntegrationDecision:
    """Route a :class:`SurfaceVerdict` + the issue's current labels into the
    pre-merge gate decision. PURE — no I/O.

      * Trading surface touched AND no ``trade-approved`` label ⇒ BLOCK to
        ``needs-trade-approval`` (the PM money wall). Checked FIRST.
      * Indicator math touched AND no ``parity-checked`` label ⇒ BLOCK to
        ``needs-baseline-backtest`` (the parity wall).
      * Otherwise ⇒ PROCEED (clean, or the relevant human clearance is present).

    The PM clears a wall by APPLYING the clearance label (``trade-approved`` /
    ``parity-checked``) and leaving the issue in ``approved`` so Eli re-polls it;
    on that next pass the gate sees the clearance and proceeds. This two-pass
    handshake is why the clearance is a label the gate reads, not a side channel.
    """
    have = {(lbl or "").strip() for lbl in (labels or ())}

    if verdict.touches_trading_surface and TRADE_APPROVED_LABEL not in have:
        return IntegrationDecision(
            proceed=False,
            state_label=NEEDS_TRADE_APPROVAL_LABEL,
            stage="trade-approval",
            reason=(
                "diff touches the live-trading surface — refusing auto-integration "
                "until the PM approves"
            ),
            brief=format_trade_approval_brief(verdict),
        )

    if verdict.touches_indicator_math and PARITY_CHECKED_LABEL not in have:
        return IntegrationDecision(
            proceed=False,
            state_label=NEEDS_BASELINE_BACKTEST_LABEL,
            stage="parity",
            reason=(
                "diff touches indicator math — baseline-backtest parity check "
                "required before landing"
            ),
            brief=format_parity_brief(verdict),
        )

    return IntegrationDecision(proceed=True)
