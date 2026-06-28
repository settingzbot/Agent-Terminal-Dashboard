"""trident_agent_halt.py — the PURE half of Slice F (issue #58).

Slice F is the universal halt-and-flag escape hatch of the HITL agent pipeline:
any run that hits something needing a human decision **halts** instead of
guessing. When that happens the orchestration (F-wiring, lands later in the
manager + gateway) must: set the issue's ``needs-human`` label, mark the run
``halted`` in the run store, post a one-line provenance comment on the issue,
and bump the red "needs your call" pill on the dashboard.

This module is the gateway/manager-free CORE of that hatch. It owns only the two
things that are pure data + pure formatting:

  * :class:`HaltReport` — the frozen, table-testable record of *why* a run
    halted, carrying exactly the fields a non-coding PM needs to understand it.
  * :func:`format_halt_report` — renders a HaltReport into a concise, plain-
    English block for the PM (no code, no jargon, no traceback).
  * :func:`format_halt_comment` — renders the one-line ``🤖 <name>:`` provenance
    comment F-wiring posts to the issue.

Everything here is deterministic and side-effect-free (no timestamps, no
randomness, no I/O), so it round-trips through unit tests without a network or a
process. The provenance one-liner deliberately MATCHES the shape of the
gateway's ``format_bot_comment`` (``🤖 {name}: {…}``) without importing it, so
this core has no cross-file dependency on the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HaltReport:
    """Why a run halted — the fields a PM needs to make the call.

    Frozen so a built report can't be mutated out from under whoever formats it.

    Attributes:
        agent_name:   Which bot halted (e.g. "Gordon", "Dr. Kleiner", "Dr. Eli Vance").
        issue_number: The GitHub issue the run was working.
        stage:        Where in the pipeline it halted (e.g. "build", "review",
                      "merge").
        reason:       One-line, plain-English why it halted.
        detail:       Optional longer context. Empty string when there's none.
    """

    agent_name: str
    issue_number: int
    stage: str
    reason: str
    detail: str = ""


def format_halt_report(report: HaltReport) -> str:
    """Render a HaltReport as a concise, plain-English block for the PM. PURE.

    Deterministic (no timestamps/randomness) so it is table-testable. Reads as
    prose a non-coder can act on — no code, no stack traces. The ``Detail``
    section is omitted entirely when ``report.detail`` is blank.
    """
    lines = [
        f"Halted: {report.agent_name} on issue #{report.issue_number}",
        f"Stage: {report.stage}",
        f"Reason: {report.reason}",
    ]
    detail = (report.detail or "").strip()
    if detail:
        lines.append(f"Detail: {detail}")
    lines.append("This needs your call before it can move forward.")
    return "\n".join(lines)


def format_halt_comment(report: HaltReport) -> str:
    """Render the one-line ``🤖 <name>:`` provenance comment for the issue. PURE.

    Matches the gateway's ``format_bot_comment`` shape (``🤖 {name}: {…}``) so
    the shared settingzbot account is still attributable, without importing from
    the gateway. F-wiring posts this to the issue when the run halts.
    """
    name = (report.agent_name or "").strip()
    reason = (report.reason or "").strip()
    return (
        f"🤖 {name}: halted on issue #{report.issue_number} "
        f"— {reason} (needs your call)"
    )
