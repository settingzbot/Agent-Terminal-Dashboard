"""trident_github_gateway.py — GitHub adapter for the issue→PR worker.

Why this module exists
----------------------
The issue→PR worker (parent #24, this slice #28) needs to read the issue
tracker, pick the next thing to work on, mark progress, and open a PR. Every
one of those is a thin shell-out to the `gh` CLI. This module is that adapter —
and ONLY that adapter: no worktree creation, no agent spawning, no run loop.
Those land in later slices that import this gateway.

The whole surface is:

    gw = GitHubGateway()                      # default runner = real `gh`
    gw.ensure_agent_working_label()           # one-time: create the label
    issue = gw.select_next_issue()            # oldest ready-for-agent, unblocked
    gw.set_labels(n, remove=[...], add=[...]) # flip triage labels
    url = gw.open_pr(title=..., head=..., ...) # open the PR, return its URL

The single side-effect seam is the `runner` callable injected into the
constructor: it takes a gh-argv list (without the leading "gh") and returns a
``GhResult``. Tests pass a fake runner so the parsing/selection logic is
exercised with the real GitHub API never touched. This mirrors the repo's
"inject every input" idiom (see tests/test_station_watchdog.py and the
subprocess-as-seam pattern in trident_agent_client.py).

Label vocabulary
----------------
``agent-working`` is the new triage label this slice introduces — it sits
between ``ready-for-agent`` (picked up) and ``ready-for-human`` (PR opened,
needs review). Its colour/description mirror the existing triage labels in
docs/agents/triage-labels.md.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

_LOG = logging.getLogger(__name__)

# CREATE_NO_WINDOW — without this, spawning the console app `gh.exe` from the
# windowless dashboard/agent subprocess makes Windows allocate a fresh console
# that flashes on screen on every issue poll. Windows-only flag; the repo idiom
# (trident_station_tray._NO_WINDOW, scripts/net_watchdog._NO_WINDOW) uses the
# literal so it's a no-op constant on POSIX where it's never applied.
_NO_WINDOW = 0x08000000

# Default repo target for gh. gh normally infers this from the cwd's git
# remote, but pinning it makes the worker safe to run from anywhere (e.g. a
# detached worktree) and keeps tests honest about what gets called.
DEFAULT_REPO = "settingzbot/Trident"

READY_LABEL = "ready-for-agent"
WORKING_LABEL = "agent-working"
HUMAN_LABEL = "ready-for-human"
# The default triage state for an untriaged issue. An autonomous run that FILES a
# new issue must leave it here (never self-apply READY_LABEL) — a human is the
# only one who promotes work to ready-for-agent (#55).
NEEDS_TRIAGE_LABEL = "needs-triage"

# agent-working triage label spec — mirrors docs/agents/triage-labels.md.
# Colour is a fresh amber/orange (distinct from needs-triage's #fbca04) to read
# as "in flight". GitHub wants the hex WITHOUT a leading '#'.
WORKING_LABEL_COLOR = "ededed"
WORKING_LABEL_DESCRIPTION = "An AFK agent is actively working this issue"

# needs-review triage label spec — the terminal state for a SUCCESSFUL agent run
# (Gordon opened a PR; it now awaits Kleiner's review). Distinct from the old
# ready-for-human (which the hard-failure path still uses). GitHub wants the hex
# WITHOUT a leading '#'.
NEEDS_REVIEW_LABEL = "needs-review"
NEEDS_REVIEW_LABEL_COLOR = "fbca04"
NEEDS_REVIEW_LABEL_DESCRIPTION = "Agent produced a PR; awaiting Kleiner's review"

# needs-signoff triage label spec (Slice B) — the terminal state Kleiner leaves:
# it has reviewed the PR and written a plain-English recommendation; the issue
# now awaits the PM's approve/reject sign-off. Green ("ready for the PM"). GitHub
# wants the hex WITHOUT a leading '#'.
NEEDS_SIGNOFF_LABEL = "needs-signoff"
NEEDS_SIGNOFF_LABEL_COLOR = "0e8a16"
NEEDS_SIGNOFF_LABEL_DESCRIPTION = "Kleiner reviewed; awaiting PM sign-off"

# approved / rejected triage label specs (Slice C) — the two terminal states the
# PM's sign-off leaves. approved (blue, "cleared for the next stage") → Eli will
# pick it up later; rejected (red, terminal) → it sits until a consult session.
# GitHub wants the hex WITHOUT a leading '#'.
APPROVED_LABEL = "approved"
APPROVED_LABEL_COLOR = "0052cc"
APPROVED_LABEL_DESCRIPTION = "PM signed off; cleared for Dr. Eli Vance integration"

REJECTED_LABEL = "rejected"
REJECTED_LABEL_COLOR = "b60205"

# merged/done triage label spec (Slice 2b, #61) — the terminal state Eli leaves a
# successfully-LANDED slice in: its branch was merged onto main, the full suite
# was green, main was pushed, and the issue was closed. Distinct from approved
# (cleared-but-not-yet-landed): merged/done means "shipped". Green, like a closed
# PR. GitHub wants the hex WITHOUT a leading '#'.
DONE_LABEL = "merged/done"
DONE_LABEL_COLOR = "1a7f37"
DONE_LABEL_DESCRIPTION = "Eli landed the slice on main and closed it"

# Trading-surface wall labels (#63) — Eli inspects an approved change's diff BEFORE
# landing. A touch on the live-trading surface (execution / bot trade paths /
# trident_config.json / secrets) makes Eli REFUSE to auto-integrate and bounce the
# issue here; a touch on indicator math gates it on a baseline-backtest parity check.
# The PM clears a wall by APPLYING the matching clearance label and leaving the issue
# in approved so Eli re-polls it. GitHub wants every hex WITHOUT a leading '#'.

# needs-trade-approval — the 🔴 money pill: Eli refused a live-trading-surface diff.
# Red, like needs-human — it sits until the PM reads the brief and clears it.
NEEDS_TRADE_APPROVAL_LABEL = "needs-trade-approval"
NEEDS_TRADE_APPROVAL_LABEL_COLOR = "d93f0b"
NEEDS_TRADE_APPROVAL_LABEL_DESCRIPTION = (
    "Eli refused a live-trading-surface change — needs PM trade approval"
)

# trade-approved — the PM's clearance marker on a trading-surface change. With this
# present Eli STOPS refusing and lands it via the normal Slice 2a path. Green.
TRADE_APPROVED_LABEL = "trade-approved"
TRADE_APPROVED_LABEL_COLOR = "0e8a16"
TRADE_APPROVED_LABEL_DESCRIPTION = (
    "PM cleared a live-trading-surface change for Eli to land"
)

# needs-baseline-backtest — the parity gate: the diff changed indicator math, so the
# baseline backtest must be re-run (bot/backtest parity) before it lands. Amber — a
# required pre-land step, not a hard halt.
NEEDS_BASELINE_BACKTEST_LABEL = "needs-baseline-backtest"
NEEDS_BASELINE_BACKTEST_LABEL_COLOR = "fbca04"
NEEDS_BASELINE_BACKTEST_LABEL_DESCRIPTION = (
    "Indicator math changed — run the baseline backtest (parity) before it lands"
)

# parity-checked — confirms the baseline backtest was re-run and bot/backtest parity
# holds. With this present Eli's parity gate passes. Green.
PARITY_CHECKED_LABEL = "parity-checked"
PARITY_CHECKED_LABEL_COLOR = "0e8a16"
PARITY_CHECKED_LABEL_DESCRIPTION = (
    "Baseline backtest re-run; bot/backtest indicator parity confirmed"
)

# needs-human triage label spec (Slice F) — the terminal state the halt escape
# hatch leaves: a run hit something beyond a simple fix and HALTED rather than
# guessing, so the issue needs Nathan's judgment. Distinct from ready-for-human
# (the original triage meaning) — needs-human is specifically "an agent halted".
# Red, like rejected, because it sits until a consult session and feeds the
# dashboard's red "needs your call" pill. GitHub wants the hex WITHOUT a '#'.
NEEDS_HUMAN_LABEL = "needs-human"
NEEDS_HUMAN_LABEL_COLOR = "d93f0b"
NEEDS_HUMAN_LABEL_DESCRIPTION = (
    "An agent halted — needs your judgment (sits until a consult session)"
)
REJECTED_LABEL_DESCRIPTION = "PM rejected; sits (handle in a consult session)"

# A blocker counts as satisfied (no longer blocking) when its issue/PR is in one
# of these states. OPEN is the only blocking state.
_SATISFIED_STATES = frozenset({"CLOSED", "MERGED"})

# "Blocked by #N" — case-insensitive, tolerant of extra spaces and a leading
# list bullet. Deliberately does NOT match a bare "#N" or "Parent #N": only the
# explicit "blocked by" phrasing (the convention in docs/agents/issue-tracker.md)
# establishes a dependency.
_BLOCKED_BY_RE = re.compile(r"blocked\s+by\s+#(\d+)", re.IGNORECASE)

# A "## Blocked by" heading (ATX, any level), regardless of its contents. Used to
# tell a MALFORMED dependency section (heading present but nothing parses) apart
# from an issue that declares no section at all. See has_malformed_blocked_by.
_BLOCKED_BY_HEADING_RE = re.compile(
    r"^[ \t]*#{1,6}[ \t]*blocked\s+by\b.*$", re.IGNORECASE | re.MULTILINE
)
# Any ATX heading — marks the end of the Blocked-by section.
_ATX_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S", re.MULTILINE)
# An explicit "no blockers" declaration inside a Blocked-by section (the canonical
# "None - can start immediately"). Keeps a deliberate no-deps section from being
# flagged as malformed.
_NO_BLOCKERS_RE = re.compile(r"\bnone\b", re.IGNORECASE)

# "Parent #N" — the child→parent backref convention (docs/agents/issue-tracker.md
# slices carry a "## Parent\n#N" section). Tolerates a markdown header, a colon,
# and whitespace/newlines between the word and the number. An issue named as the
# parent by an OPEN child is an umbrella and must NOT be worked directly (#52):
# implementing the umbrella duplicates the superset of its own slices.
_PARENT_RE = re.compile(r"parent[:\s]*#(\d+)", re.IGNORECASE)

# "Closes #N" — a PR body's closing reference to the issue it resolves. GitHub
# treats closes / fixes / resolves (and their -ed/-es variants) as closing
# keywords; we accept the three canonical verbs case-insensitively. Used by
# Slice B (Kleiner) to map an issue → the open PR that closes it.
_CLOSES_RE = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)

# The branch namespace every autonomous run's feature branch lives under
# (trident_agent_worktree.BRANCH_PREFIX is "agent/issue-"). An open PR whose head
# branch starts here is an agent-authored PR — the unit #53's cap counts.
AGENT_BRANCH_PREFIX = "agent/"


class GitHubGatewayError(RuntimeError):
    """Raised when a gh invocation fails in a way the caller must handle."""


@dataclass(frozen=True)
class GhResult:
    """The slice of subprocess.CompletedProcess this gateway reads."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Issue:
    """A parsed issue from `gh issue list`/`gh issue view`."""

    number: int
    title: str
    body: str
    created_at: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    # The login that opened the issue. Used by #52 to skip bot-authored issues
    # the agent invented for itself (unless a human re-triaged them).
    author: str = ""

    @property
    def blocked_by(self) -> list[int]:
        return parse_blocked_by(self.body)

    @property
    def parent_refs(self) -> list[int]:
        return parse_parent_refs(self.body)


# ─────────────────────────────────────────────────────────────────────────────
# Pure parsing helpers (no I/O)
# ─────────────────────────────────────────────────────────────────────────────

def parse_blocked_by(body: Optional[str]) -> list[int]:
    """Return the de-duplicated, order-preserving list of issue numbers a body
    declares itself "Blocked by". Empty list for None/empty/no-match."""
    if not body:
        return []
    seen: list[int] = []
    for m in _BLOCKED_BY_RE.finditer(body):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _blocked_by_section_text(body: str) -> str:
    """The text of the first '## Blocked by' section — from its heading to the
    next ATX heading or end-of-body. '' when there is no such heading."""
    m = _BLOCKED_BY_HEADING_RE.search(body)
    if not m:
        return ""
    after = body[m.end():]
    nxt = _ATX_HEADING_RE.search(after)
    return after[: nxt.start()] if nxt else after


def has_malformed_blocked_by(body: Optional[str]) -> bool:
    """True iff the body declares a "## Blocked by" SECTION that yields ZERO
    parseable dependencies and does not explicitly say "None".

    Guards the #100/#101 footgun (2026-06-26): a bullet like ``- owner/repo#99``
    or a bare ``- #99`` sits under the heading and LOOKS like a blocker, but the
    gateway's ``_BLOCKED_BY_RE`` (``blocked by #N``) does not match it, so
    ``parse_blocked_by`` returns ``[]`` and the issue is silently treated as
    unblocked — an agent then grabs it out of dependency order. ``is_issue_unblocked``
    treats a malformed section as BLOCKING so a broken declaration fails closed
    (held + logged) instead of open (grabbed)."""
    if not body or not _BLOCKED_BY_HEADING_RE.search(body):
        return False  # no section at all → genuinely no declared deps
    if parse_blocked_by(body):
        return False  # well-formed: at least one dep parsed
    # Heading present but nothing parsed: malformed UNLESS it explicitly says "None".
    return not _NO_BLOCKERS_RE.search(_blocked_by_section_text(body))


def parse_parent_refs(body: Optional[str]) -> list[int]:
    """Return the de-duplicated, order-preserving list of issue numbers a body
    names as its "Parent #N". Empty list for None/empty/no-match. Mirrors
    parse_blocked_by but for the child→parent backref convention."""
    if not body:
        return []
    seen: list[int] = []
    for m in _PARENT_RE.finditer(body):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def parse_closes(body: Optional[str]) -> list[int]:
    """Return the de-duplicated, order-preserving list of issue numbers a PR body
    declares it "Closes" (also "Fixes"/"Resolves" — gh's closing keywords).
    Empty list for None/empty/no-match. Mirrors parse_blocked_by/parse_parent_refs."""
    if not body:
        return []
    seen: list[int] = []
    for m in _CLOSES_RE.finditer(body):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def format_bot_comment(bot_name: str, sentence: str) -> str:
    """Format a one-line provenance comment for an issue. PURE.

    Returns ``🤖 {bot_name}: {sentence}`` with surrounding whitespace stripped
    from both halves. This is the canonical shape every agent (Gordon, Kleiner,
    Dr. Eli Vance) leaves so a reader can tell WHICH bot acted — the shared settingzbot
    gh account can't otherwise attribute the action.
    """
    return f"🤖 {(bot_name or '').strip()}: {(sentence or '').strip()}"


def format_pm_decision(verdict: str) -> str:
    """Format the PM's final sign-off decision for an issue comment. PURE.

    This is the HUMAN's verdict (story 19: "my final decision logged alongside
    the bot's recommendation"), so it leads with 👤 — NOT the 🤖 provenance
    prefix the bots use. ``verdict`` must be exactly ``"approve"``, ``"reject"``,
    or ``"trade-approve"`` (the #63/#65 money-wall clearance); anything else
    raises ValueError (the gate exposes only those transitions).
    """
    if verdict == "approve":
        return "👤 PM decision: APPROVED — cleared for Dr. Eli Vance integration"
    if verdict == "reject":
        return (
            "👤 PM decision: REJECTED — will not be merged "
            "(handle in a consult session)"
        )
    if verdict == "trade-approve":
        return (
            "👤 PM decision: TRADING-SURFACE APPROVED — money wall cleared; "
            "Eli will land it the normal way"
        )
    raise ValueError(
        f"unknown PM verdict: {verdict!r} "
        "(expected 'approve'/'reject'/'trade-approve')"
    )


def _author_login(obj: dict) -> str:
    """The author login from a gh issue JSON object (``author.login``), or ""."""
    author = obj.get("author")
    if isinstance(author, dict):
        return author.get("login", "") or ""
    return ""


def _issue_from_json(obj: dict) -> Issue:
    return Issue(
        number=int(obj["number"]),
        title=obj.get("title", ""),
        body=obj.get("body") or "",
        created_at=obj.get("createdAt", ""),
        labels=tuple(lbl.get("name", "") for lbl in obj.get("labels", [])),
        author=_author_login(obj),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Default real runner
# ─────────────────────────────────────────────────────────────────────────────

def _default_runner(argv: Sequence[str]) -> GhResult:
    """Run the real `gh` CLI with the given argv (without the leading 'gh')."""
    proc = subprocess.run(
        ["gh", *argv],
        capture_output=True,
        text=True,
        # gh emits UTF-8 (issue/PR titles carry em-dashes, curly quotes). Without
        # an explicit encoding, text=True decodes with the OS locale (cp1252 on
        # Windows), which RAISES on bytes it can't map — silently returning empty
        # output and dropping the very PR/issue we're listing. Decode UTF-8 with
        # errors="replace" so a stray byte degrades to a glyph, never a crash.
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return GhResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# ─────────────────────────────────────────────────────────────────────────────
# Gateway
# ─────────────────────────────────────────────────────────────────────────────

class GitHubGateway:
    """Thin wrapper around the `gh` CLI — read/label/PR adapter only.

    All gh calls go through ``self._run`` (the injected runner), so the entire
    class is unit-testable with `gh` stubbed.
    """

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        runner: Callable[[Sequence[str]], GhResult] = _default_runner,
        *,
        bot_login: Optional[str] = None,
    ) -> None:
        self.repo = repo
        self._run = runner
        # The account the agent uses to FILE issues. When set, issues authored by
        # this login are treated as bot-invented and skipped unless a human
        # re-applied ready-for-agent (#52). DEFAULTS TO None — on the live host
        # the agent and Nathan share one gh account (settingzbot), so author can't
        # distinguish bot-filed from human-filed work; leaving it None keeps the
        # author filter INERT (and never strands Nathan's own issues) until a
        # distinct bot identity exists. The umbrella-parent filter below is
        # account-independent and stays live regardless.
        self.bot_login = bot_login
        # Cache blocker-state lookups within a single selection pass so two
        # issues sharing a blocker don't double-hit gh.
        self._blocker_cache: dict[int, bool] = {}

    # ── low-level ────────────────────────────────────────────────────────────

    def _gh(self, argv: Sequence[str], *, allow_fail: bool = False) -> GhResult:
        """Invoke gh with --repo pinned. Raises GitHubGatewayError on non-zero
        unless allow_fail (then the caller inspects the result)."""
        full = [*argv, "--repo", self.repo]
        res = self._run(full)
        if res.returncode != 0 and not allow_fail:
            raise GitHubGatewayError(
                f"gh {' '.join(argv)} failed (exit {res.returncode}): "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )
        return res

    # ── reads ────────────────────────────────────────────────────────────────

    def list_ready_issues(self) -> list[Issue]:
        """All open issues carrying the ready-for-agent label, as returned by
        gh (caller does not rely on gh's ordering — selection sorts)."""
        res = self._gh([
            "issue", "list",
            "--label", READY_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def list_working_issues(self) -> list[Issue]:
        """All open issues carrying the agent-working label (an agent has CLAIMED
        the issue and is mid-run — it may not have opened a PR yet). Mirrors
        list_ready_issues but on agent-working — feeds the #64 strict-serial
        in-flight count so a claimed-but-not-yet-PR'd issue still blocks a new
        claim. Caller sorts/counts."""
        res = self._gh([
            "issue", "list",
            "--label", WORKING_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def list_needs_review_issues(self) -> list[Issue]:
        """All open issues carrying the needs-review label (the state Gordon
        leaves a finished PR in). Mirrors list_ready_issues but on needs-review —
        this is Kleiner's poll source (Slice B). Caller sorts for selection."""
        res = self._gh([
            "issue", "list",
            "--label", NEEDS_REVIEW_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def list_needs_signoff_issues(self) -> list[Issue]:
        """All open issues carrying the needs-signoff label (the state Kleiner
        leaves after writing its recommendation). Mirrors list_needs_review_issues
        but on needs-signoff — this is the PM's poll source (Slice C), and feeds
        the dashboard sign-off pill count. Caller sorts for selection."""
        res = self._gh([
            "issue", "list",
            "--label", NEEDS_SIGNOFF_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def list_needs_human_issues(self) -> list[Issue]:
        """All open issues carrying the needs-human label (the state the halt
        escape hatch leaves — Slice F). Mirrors list_needs_signoff_issues but on
        needs-human; this feeds the dashboard's red "needs your call" pill count.
        Caller sorts for selection."""
        res = self._gh([
            "issue", "list",
            "--label", NEEDS_HUMAN_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def list_needs_trade_approval_issues(self) -> list[Issue]:
        """All open issues carrying the needs-trade-approval label (the state Eli
        leaves when it REFUSES to auto-integrate a live-trading-surface diff — #63).
        Mirrors list_needs_human_issues; feeds the dashboard's 🔴 trading-surface
        pill. Caller sorts for selection."""
        res = self._gh([
            "issue", "list",
            "--label", NEEDS_TRADE_APPROVAL_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def list_open_issues(self) -> list[Issue]:
        """ALL open issues (not just ready-for-agent ones). Used to discover
        umbrella parents: a child slice references its parent via "Parent #N" in
        its body, and a child can be in any triage state, so the parent set must
        be computed over every open issue — not only the ready ones."""
        res = self._gh([
            "issue", "list",
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def get_issue_body(self, number: int) -> str:
        """Return issue #number's current body via `gh issue view --json body`.

        The READ half of the read-modify-write Eli's in-scope re-plan needs for a
        ``ReorderDeps`` action: to swap ONLY an issue's ``## Blocked by`` section
        without destroying the rest of its body, the architect pipeline must first
        fetch the live body, surgically replace the deps section, and edit back the
        FULL preserved body (a blind ``edit_issue`` with just the deps section would
        wipe "What to build" / "Acceptance criteria" — the #62 data-loss bug). A
        missing ``body`` field yields the empty string. Raises GitHubGatewayError on
        a non-zero gh exit. Mirrors ``is_blocker_satisfied``'s `gh issue view --json`
        wrapper style."""
        res = self._gh(["issue", "view", str(number), "--json", "body"])
        return json.loads(res.stdout or "{}").get("body") or ""

    def umbrella_parents(self) -> set[int]:
        """The set of issue numbers named as the parent by at least one OPEN
        child issue. Any ready issue in this set is an umbrella/parent and is
        excluded from selection (#52) — its slices are the real work."""
        parents: set[int] = set()
        for issue in self.list_open_issues():
            parents.update(issue.parent_refs)
        return parents

    def is_blocker_satisfied(self, number: int) -> bool:
        """True if issue/PR #number is CLOSED or MERGED (no longer blocking),
        OR if it carries the ``needs-triage`` label — those are PRDs / spec
        documents that define scope rather than work items that get "done."
        A PRD blocker is always satisfied so its children can proceed.

        Cached per gateway-pass. `gh issue view` resolves both issues and PRs;
        a merged PR reports state MERGED, a completed issue reports CLOSED."""
        if number in self._blocker_cache:
            return self._blocker_cache[number]
        res = self._gh([
            "issue", "view", str(number),
            "--json", "number,state,stateReason,labels",
        ], allow_fail=True)
        if res.returncode != 0:
            # Couldn't resolve the blocker — treat as STILL blocking (safe
            # default: never run an issue whose dependency we can't confirm).
            _LOG.warning("could not resolve blocker #%d: %s", number,
                         res.stderr.strip())
            satisfied = False
        else:
            data = json.loads(res.stdout or "{}")
            state = (data.get("state") or "").upper()
            if state in _SATISFIED_STATES:
                satisfied = True
            else:
                # PRDs / spec documents carry needs-triage and are never
                # "done" — treat them as satisfied so their child slices
                # can be claimed.
                labels = {l["name"] for l in data.get("labels") or []}
                satisfied = "needs-triage" in labels
        self._blocker_cache[number] = satisfied
        return satisfied

    def is_issue_unblocked(self, issue: Issue) -> bool:
        """True if every 'Blocked by #N' on the issue points at a satisfied
        blocker (or there are none).

        A MALFORMED Blocked-by section (heading present but zero parseable deps —
        e.g. ``- owner/repo#99`` instead of ``- Blocked by #99``) fails CLOSED:
        the issue is held as blocked, not released as ready, so a broken
        dependency declaration can't let an agent grab it out of order (the
        #100/#101 footgun, 2026-06-26)."""
        if has_malformed_blocked_by(issue.body):
            _LOG.warning(
                "issue #%d has a '## Blocked by' section that parses to ZERO "
                "dependencies — holding it as blocked. Rewrite the bullets to the "
                "canonical '- Blocked by #N' phrasing to release it.",
                issue.number,
            )
            return False
        return all(self.is_blocker_satisfied(n) for n in issue.blocked_by)

    def ready_label_applied_by_human(self, number: int) -> bool:
        """True iff the ``ready-for-agent`` label on issue #number was applied by a
        human — an actor distinct from ``bot_login`` — per the issue timeline.

        Two callers, one mechanism (the ``labeled`` timeline-event actor):

        * #52 eligibility — a bot-authored issue is only worked if a human later
          re-applied the label (the agent can't promote its own invented scope).
        * #55 self-promotion guard — the post-run revert must NOT clobber an issue
          a human deliberately handed to the agent. The dangerous case is the
          operator filing a legitimate ``ready-for-agent`` issue WHILE a run is in
          flight: its number isn't in the pre-run snapshot, so the number-diff
          alone can't tell it apart from an agent self-promotion. A confirmed
          non-bot ``labeled`` actor is exactly that "a human handed it over" signal.

        Returns False when ``bot_login`` is unset (no distinct bot identity ⇒ the
        shared settingzbot account can't attribute the actor) or the timeline can't
        be read. Conservative by design: only a CONFIRMED non-bot actor returns
        True, so the #55 caller falls back to the number-diff revert rather than
        granting a keep-the-label escape it can't justify."""
        bot = self.bot_login
        if not bot:
            return False
        res = self._run([
            "api",
            f"repos/{self.repo}/issues/{number}/timeline",
            "--paginate",
        ])
        if res.returncode != 0:
            _LOG.warning("could not read timeline for #%d: %s", number,
                         res.stderr.strip())
            return False
        try:
            events = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            return False
        for ev in events:
            if ev.get("event") != "labeled":
                continue
            label = (ev.get("label") or {}).get("name", "")
            actor = (ev.get("actor") or {}).get("login", "")
            if label == READY_LABEL and actor and actor != bot:
                return True
        return False

    def _human_retriaged(self, number: int) -> bool:
        """#52 eligibility alias — see :meth:`ready_label_applied_by_human`. Only
        called when ``bot_login`` is configured (gated in ``is_issue_eligible``)."""
        return self.ready_label_applied_by_human(number)

    def is_issue_eligible(self, issue: Issue, umbrella_parents: set[int]) -> bool:
        """Whether a ready issue may be worked autonomously (#52). Excludes:

        1. Umbrella/parent issues — named as the parent by an open child.
        2. Bot-authored issues — filed by ``bot_login`` and never re-triaged by a
           human (only checked when ``bot_login`` is configured; see __init__).

        Blocked-by is checked separately by ``is_issue_unblocked``."""
        if issue.number in umbrella_parents:
            return False
        if self.bot_login and issue.author == self.bot_login:
            if not self._human_retriaged(issue.number):
                return False
        return True

    def select_next_issue(self) -> Optional[Issue]:
        """The OLDEST ELIGIBLE ready-for-agent issue whose blockers are all
        satisfied.

        Oldest = smallest createdAt (ISO-8601 sorts lexicographically).
        Ineligible issues (umbrella parents; un-re-triaged bot-authored issues —
        see ``is_issue_eligible``) are skipped. Returns None when no ready issue
        is both eligible and unblocked."""
        self._blocker_cache.clear()
        umbrella = self.umbrella_parents()
        issues = sorted(self.list_ready_issues(), key=lambda i: i.created_at)
        for issue in issues:
            if not self.is_issue_eligible(issue, umbrella):
                continue
            if self.is_issue_unblocked(issue):
                return issue
        return None

    def count_open_agent_prs(self) -> int:
        """How many OPEN PRs were opened by an autonomous run — counted by head
        branch in the ``agent/`` namespace (#53's in-flight cap). The scheduler
        withholds new watch runs while this is at/over the cap."""
        res = self._gh([
            "pr", "list",
            "--state", "open",
            "--limit", "200",
            "--json", "number,headRefName",
        ])
        data = json.loads(res.stdout or "[]")
        return sum(
            1 for pr in data
            if str(pr.get("headRefName", "")).startswith(AGENT_BRANCH_PREFIX)
        )

    def list_open_agent_prs(self) -> list[dict]:
        """Open PRs in the ``agent/`` branch namespace, each with the issue it
        targets parsed off its branch (``agent/issue-<N>-…``). Powers the dashboard
        loop-health panel's orphaned-PR detection — the panel needs the PR number,
        title, and issue per open agent PR (``count_open_agent_prs`` only counts).
        Returns ``[{number, title, branch, issue}]`` (``issue`` None if the branch
        doesn't encode one). Mirrors ``count_open_agent_prs``' gh-wrapper style."""
        res = self._gh([
            "pr", "list",
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,headRefName",
        ])
        data = json.loads(res.stdout or "[]")
        out: list[dict] = []
        for pr in data:
            branch = str(pr.get("headRefName", ""))
            if not branch.startswith(AGENT_BRANCH_PREFIX):
                continue
            m = re.search(r"issue-(\d+)", branch)
            out.append({
                "number": int(pr["number"]),
                "title": pr.get("title") or "",
                "branch": branch,
                "issue": int(m.group(1)) if m else None,
            })
        return out

    def close_pr(self, pr_number: int, *, comment: Optional[str] = None) -> None:
        """Close PR #pr_number via `gh pr close` (the loop-health panel's mechanical
        unblock). An ORPHANED agent PR — one whose issue left the pipeline, so nothing
        will ever advance or merge it — pins the #53 claim gate; the operator closes
        it from the dashboard to let the loop resume. ``comment`` leaves a closing
        note for the audit trail. Raises GitHubGatewayError on a non-zero gh exit so
        the caller (the resolve RPC) can surface it. Mirrors ``close_issue``'s style."""
        argv = ["pr", "close", str(pr_number)]
        if comment:
            argv += ["--comment", comment]
        self._gh(argv)

    def find_open_pr_for_issue(self, issue_number: int) -> Optional[int]:
        """The number of the first OPEN PR whose body "Closes #issue_number"
        (Slice B). Lists open PRs and matches via parse_closes. None if no open
        PR closes the issue."""
        res = self._gh([
            "pr", "list",
            "--state", "open",
            "--limit", "200",
            "--json", "number,body",
        ])
        data = json.loads(res.stdout or "[]")
        for pr in data:
            if issue_number in parse_closes(pr.get("body") or ""):
                return int(pr["number"])
        return None

    def get_pr_diff(self, pr_number: int) -> str:
        """The unified diff of PR #pr_number (`gh pr diff`). READ-ONLY — Kleiner
        embeds this in its review prompt so it never needs repo/worktree access."""
        res = self._gh(["pr", "diff", str(pr_number)])
        return res.stdout or ""

    def get_pr_files(self, pr_number: int) -> list[str]:
        """The repo-relative paths PR #pr_number changes (`gh pr diff --name-only`).
        READ-ONLY — the INPUT to Eli's trading-surface wall (#63): the detector
        (``trident_trade_surface.classify_paths``) is a PURE function over exactly
        this list. One path per line; blank lines dropped. Mirrors ``get_pr_diff``'s
        gh-wrapper style."""
        res = self._gh(["pr", "diff", str(pr_number), "--name-only"])
        return [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]

    def list_approved_issues(self) -> list[Issue]:
        """All open issues carrying the approved label (the state Kleiner leaves a
        cleanly-reviewed PR in). Mirrors list_needs_signoff_issues but on approved —
        this is Eli's poll source (Slice 2a, #60): the integration inbox. Caller
        sorts for selection."""
        res = self._gh([
            "issue", "list",
            "--label", APPROVED_LABEL,
            "--state", "open",
            "--limit", "200",
            "--json", "number,title,body,createdAt,labels,author",
        ])
        data = json.loads(res.stdout or "[]")
        return [_issue_from_json(o) for o in data]

    def get_pr_branch(self, pr_number: int) -> Optional[str]:
        """The head branch name of PR #pr_number (`gh pr view --json headRefName`).
        Eli (Slice 2a, #60) needs the branch to merge it onto main. Returns None if
        gh reports no head branch."""
        res = self._gh(["pr", "view", str(pr_number), "--json", "headRefName"])
        data = json.loads(res.stdout or "{}")
        return data.get("headRefName") or None

    def merge_pr_to_main(self, pr_number: int, *, method: str = "merge") -> None:
        """Land PR #pr_number on main via `gh pr merge` (Slice 2a, #60).

        Eli is the ONE agent allowed to do this — every other agent's push-to-main
        is blocked by the guard (trident_agent_guards.CATEGORY_PUSH_MAIN). This is
        the thin gh primitive behind Eli's injected merge seam; the green-or-halt
        decision (whether to call it, and the full-suite gate) lives in
        EliPipeline, not here. ``method`` is the gh merge strategy
        (merge/squash/rebase). Raises GitHubGatewayError on a non-zero gh exit
        (e.g. a non-trivial conflict gh can't auto-resolve) so the caller halts."""
        flag = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}.get(
            method, "--merge"
        )
        self._gh(["pr", "merge", str(pr_number), flag])

    def close_issue(self, number: int, *, reason: str = "completed") -> None:
        """Close issue #number via `gh issue close` (Slice 2b, #61).

        The thin gh primitive behind Eli's finalize step: once a slice is LANDED
        (merged onto main, full suite green, main pushed) Eli closes its issue.
        Also reused to close a parent PRD when its last in-scope child is merged.
        ``reason`` is gh's close reason (completed / not planned). Raises
        GitHubGatewayError on a non-zero gh exit so the caller can log it — though
        Eli's finalize treats a close failure as best-effort (main is already
        safely landed), NOT a halt. Mirrors merge_pr_to_main's gh-wrapper style."""
        self._gh(["issue", "close", str(number), "--reason", reason])

    def open_issue(
        self,
        title: str,
        body: str,
        labels: Optional[Iterable[str]] = None,
    ) -> int:
        """Open a NEW issue via `gh issue create` and return its number (Slice 3,
        #62 — Eli's in-scope re-plan opens fresh child issues).

        Distinct from ``open_pr``: this creates an ISSUE, not a PR, and returns
        the new issue NUMBER (not a URL). gh prints the new issue's URL on stdout
        (``…/issues/123``); we parse the trailing number off it. ``labels`` are
        applied at creation — Eli is the ONE pipeline actor permitted to stamp
        ``ready-for-agent`` on an issue it files (the inverse of Gordon's #55
        self-promote guard), because an in-scope re-plan's new work is meant to be
        claimed. The caller (the architect pipeline) is responsible for the
        ``Parent #<PRD>`` ref in the body + the 🤖 audit comment. Raises
        GitHubGatewayError on a non-zero gh exit, or if no issue number could be
        parsed from gh's output."""
        argv: list[str] = ["issue", "create", "--title", title, "--body", body]
        for lbl in (labels or []):
            argv += ["--label", lbl]
        res = self._gh(argv)
        out = (res.stdout or "").strip()
        # gh prints the new issue URL, e.g. https://github.com/owner/repo/issues/123
        m = re.search(r"/issues/(\d+)\s*$", out)
        if not m:
            # Fall back to any trailing integer (some gh versions print just the
            # number); still fail loudly if nothing numeric is present.
            m = re.search(r"(\d+)\s*$", out)
        if not m:
            raise GitHubGatewayError(
                f"gh issue create succeeded but returned no issue number: {out!r}"
            )
        return int(m.group(1))

    def edit_issue(self, number: int, *, body: Optional[str] = None) -> None:
        """Edit issue #number via `gh issue edit` (Slice 3, #62 — Eli rewrites a
        child's acceptance criteria or reorders its ``Blocked by`` deps as a
        body-rewrite).

        Only ``body`` is supported (the one field an in-scope re-plan changes).
        Label flips have their own primitive (``set_labels``); keeping this
        body-only avoids overloading it. Raises GitHubGatewayError on a non-zero
        gh exit, or if called with nothing to change. Mirrors ``set_labels`` /
        ``close_issue``'s gh-wrapper style."""
        if body is None:
            raise GitHubGatewayError("edit_issue called with nothing to change")
        self._gh(["issue", "edit", str(number), "--body", body])

    # ── writes ───────────────────────────────────────────────────────────────

    def set_labels(
        self,
        number: int,
        add: Optional[Iterable[str]] = None,
        remove: Optional[Iterable[str]] = None,
    ) -> None:
        """Flip an issue's labels in one gh edit call."""
        argv: list[str] = ["issue", "edit", str(number)]
        for lbl in (add or []):
            argv += ["--add-label", lbl]
        for lbl in (remove or []):
            argv += ["--remove-label", lbl]
        if len(argv) == 3:
            raise GitHubGatewayError("set_labels called with nothing to add or remove")
        self._gh(argv)

    def leave_bot_comment(self, number: int, bot_name: str, sentence: str) -> None:
        """Post a one-line provenance comment on issue #number.

        This is the UNIVERSAL provenance primitive every agent in the pipeline
        (Gordon, and later Kleiner / Dr. Eli Vance) calls to stamp WHICH bot acted —
        the shared settingzbot gh account can't otherwise attribute which agent
        flipped a label or opened a PR. The comment shape is fixed by
        ``format_bot_comment``: ``🤖 {bot_name}: {sentence}``.
        """
        self._gh([
            "issue", "comment", str(number),
            "--body", format_bot_comment(bot_name, sentence),
        ])

    def leave_comment(self, number: int, body: str) -> None:
        """Post a PLAIN comment on issue #number (`gh issue comment`) — no 🤖
        provenance prefix. Distinct from leave_bot_comment: this is the verbatim
        body the caller supplies, used for the PM's human-authored sign-off
        decision (Slice C). The body is whatever the caller passes."""
        self._gh([
            "issue", "comment", str(number),
            "--body", body,
        ])

    def leave_pr_comment(self, pr_number: int, body: str) -> None:
        """Post a comment on PR #pr_number (`gh pr comment`). Kleiner posts its
        FULL plain-English recommendation here (the issue gets only the short
        provenance one-liner via leave_bot_comment)."""
        self._gh([
            "pr", "comment", str(pr_number),
            "--body", body,
        ])

    def open_pr(self, title: str, body: str, head: str, base: str = "main") -> str:
        """Open a PR via gh and return its URL (gh prints the URL on stdout)."""
        res = self._gh([
            "pr", "create",
            "--title", title,
            "--body", body,
            "--head", head,
            "--base", base,
        ])
        url = (res.stdout or "").strip().splitlines()[-1] if res.stdout.strip() else ""
        if not url:
            raise GitHubGatewayError("gh pr create succeeded but returned no URL")
        return url

    # ── label bootstrap ──────────────────────────────────────────────────────

    def ensure_agent_working_label(self) -> bool:
        """Create the agent-working label if it doesn't exist.

        Returns True if it was created, False if it already existed. Raises
        GitHubGatewayError on any other gh failure. Idempotent — safe to call
        on every worker startup."""
        res = self._gh([
            "label", "create", WORKING_LABEL,
            "--color", WORKING_LABEL_COLOR,
            "--description", WORKING_LABEL_DESCRIPTION,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {WORKING_LABEL} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    def ensure_needs_review_label(self) -> bool:
        """Create the needs-review label if it doesn't exist.

        Returns True if it was created, False if it already existed. Raises
        GitHubGatewayError on any other gh failure. Idempotent — safe to call
        on every worker startup. Exact mirror of ``ensure_agent_working_label``."""
        res = self._gh([
            "label", "create", NEEDS_REVIEW_LABEL,
            "--color", NEEDS_REVIEW_LABEL_COLOR,
            "--description", NEEDS_REVIEW_LABEL_DESCRIPTION,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {NEEDS_REVIEW_LABEL} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    def ensure_needs_signoff_label(self) -> bool:
        """Create the needs-signoff label if it doesn't exist (Slice B).

        Returns True if it was created, False if it already existed. Raises
        GitHubGatewayError on any other gh failure. Idempotent — safe to call
        on every worker startup. Exact mirror of ``ensure_needs_review_label``."""
        res = self._gh([
            "label", "create", NEEDS_SIGNOFF_LABEL,
            "--color", NEEDS_SIGNOFF_LABEL_COLOR,
            "--description", NEEDS_SIGNOFF_LABEL_DESCRIPTION,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {NEEDS_SIGNOFF_LABEL} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    def ensure_approved_label(self) -> bool:
        """Create the approved label if it doesn't exist (Slice C).

        Returns True if it was created, False if it already existed. Raises
        GitHubGatewayError on any other gh failure. Idempotent. Exact mirror of
        ``ensure_needs_signoff_label``."""
        res = self._gh([
            "label", "create", APPROVED_LABEL,
            "--color", APPROVED_LABEL_COLOR,
            "--description", APPROVED_LABEL_DESCRIPTION,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {APPROVED_LABEL} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    def ensure_rejected_label(self) -> bool:
        """Create the rejected label if it doesn't exist (Slice C).

        Returns True if it was created, False if it already existed. Raises
        GitHubGatewayError on any other gh failure. Idempotent. Exact mirror of
        ``ensure_approved_label``."""
        res = self._gh([
            "label", "create", REJECTED_LABEL,
            "--color", REJECTED_LABEL_COLOR,
            "--description", REJECTED_LABEL_DESCRIPTION,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {REJECTED_LABEL} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    def ensure_needs_human_label(self) -> bool:
        """Create the needs-human label if it doesn't exist (Slice F halt hatch).

        Returns True if it was created, False if it already existed. Raises
        GitHubGatewayError on any other gh failure. Idempotent — safe to call on
        every worker startup. Exact mirror of ``ensure_needs_signoff_label``."""
        res = self._gh([
            "label", "create", NEEDS_HUMAN_LABEL,
            "--color", NEEDS_HUMAN_LABEL_COLOR,
            "--description", NEEDS_HUMAN_LABEL_DESCRIPTION,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {NEEDS_HUMAN_LABEL} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    # ── trading-surface wall labels (#63) ────────────────────────────────────

    def _ensure_label(self, name: str, color: str, description: str) -> bool:
        """Create a label if it doesn't exist. Returns True if created, False if it
        already existed; raises GitHubGatewayError on any other gh failure.
        Idempotent. The shared core of the ``ensure_*_label`` family — the #63 wall
        labels use it directly."""
        res = self._gh([
            "label", "create", name,
            "--color", color,
            "--description", description,
        ], allow_fail=True)
        if res.returncode == 0:
            return True
        msg = (res.stderr + res.stdout).lower()
        if "already exists" in msg:
            return False
        raise GitHubGatewayError(
            f"gh label create {name} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    def ensure_needs_trade_approval_label(self) -> bool:
        """Create the needs-trade-approval label if absent (#63). Idempotent."""
        return self._ensure_label(
            NEEDS_TRADE_APPROVAL_LABEL,
            NEEDS_TRADE_APPROVAL_LABEL_COLOR,
            NEEDS_TRADE_APPROVAL_LABEL_DESCRIPTION,
        )

    def ensure_trade_approved_label(self) -> bool:
        """Create the trade-approved label if absent (#63). Idempotent."""
        return self._ensure_label(
            TRADE_APPROVED_LABEL,
            TRADE_APPROVED_LABEL_COLOR,
            TRADE_APPROVED_LABEL_DESCRIPTION,
        )

    def ensure_needs_baseline_backtest_label(self) -> bool:
        """Create the needs-baseline-backtest label if absent (#63). Idempotent."""
        return self._ensure_label(
            NEEDS_BASELINE_BACKTEST_LABEL,
            NEEDS_BASELINE_BACKTEST_LABEL_COLOR,
            NEEDS_BASELINE_BACKTEST_LABEL_DESCRIPTION,
        )

    def ensure_parity_checked_label(self) -> bool:
        """Create the parity-checked label if absent (#63). Idempotent."""
        return self._ensure_label(
            PARITY_CHECKED_LABEL,
            PARITY_CHECKED_LABEL_COLOR,
            PARITY_CHECKED_LABEL_DESCRIPTION,
        )
