"""
trident_agent_pipeline.py — the issue→PR worker pipeline (issue #35, the
CAPSTONE of parent #24).

What this is
------------
The full autonomous issue→PR worker that ties together everything built in
Waves 0-3:

  * the GitHub gateway (#28)       — claim issue, flip labels, open PR
  * the worktree primitive (#35)   — trident_agent_worktree.WorktreeCreator
  * the run guards (#34)           — install_pre_push_hook into the worktree
  * the run engine (#30)           — launch claude as a tagged terminal
  * the scheduler core (#33)       — a watch agent is just another Candidate

⚠️ SHIPPED OFF BY DEFAULT. Autonomous firing is gated behind the
``TRIDENT_AGENT_WATCH_ENABLED`` env flag (``watch_enabled()``), which DEFAULTS
TO FALSE. The scheduler checks it before processing ANY watch agent; until
Nathan sets it, the watch trigger is inert and logs loudly that it is disabled.
This is the "first real autonomous run is human-supervised" acceptance gate —
the host throws the switch.

The flow (``IssuePrPipeline.process``), only when the gate is ON
---------------------------------------------------------------
1. Claim the oldest eligible unblocked ``ready-for-agent`` issue via the gateway;
   flip ``ready-for-agent`` → ``agent-working`` (prevents double-pick). No
   eligible issue ⇒ no-op.
2. Create a git worktree off latest ``main`` on ``agent/issue-<n>-<slug>``.
3. Install the #34 pre-push hook into the worktree.
4. Launch ``claude`` in the worktree (headless, loosened permissions) with a
   prompt built from the issue.
5. TEST-GATE: run pytest in the worktree on SYSTEM Python 3.12; parse green/red.
6. Green ⇒ normal PR with results; red ⇒ DRAFT PR marked failed with the output.
   PR carries ``Closes #N`` + the generated footer; issue → ``needs-review`` and
   Gordon leaves a one-line provenance comment naming the PR.
7. On any hard failure (run error, worktree error, push/PR-creation error): HALT
   (Slice F escape hatch) — flip to ``needs-human``, post a 🤖 halt provenance
   comment, KEEP the worktree/branch, mark the run ``halted`` (not failed), and DO
   NOT retry. The DRAFT-PR-on-red-tests path is NOT a halt — it still lands on
   ``needs-review`` for Kleiner.

Pure cores (unit-tested as data tables, no I/O)
-----------------------------------------------
  * ``build_issue_prompt``  — issue → claude prompt
  * ``build_claude_command``— model + prompt → headless claude launch command
  * ``decide_pr``           — TestOutcome → PrDecision (normal vs draft)

Every side-effecting seam is INJECTED into ``IssuePrPipeline`` so the whole flow
is driven by fakes in tests — no real worktree, no real claude, no real push, no
real gh, no real recursive pytest.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from agents import guards
from agents import provider
from agents.halt import HaltReport, format_halt_comment, format_halt_report
from agents.worktree import WorktreeError, WorktreeInfo
from shared.github_gateway import (
    Issue,
    READY_LABEL,
    WORKING_LABEL,
    NEEDS_HUMAN_LABEL,
    NEEDS_REVIEW_LABEL,
    NEEDS_TRIAGE_LABEL,
)

_LOG = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Master gate — autonomous watch firing is OFF by default
# ═══════════════════════════════════════════════════════════════════════════════
#
# The SOLE control over the autonomous watch loop is the persisted dashboard
# toggle (watch_gate.json, via AgentManager.set_watch_gate / watch_gate_on).
# The env var below is LEGACY and IGNORED — it no longer arms anything.
#
# WHY (2026-06-23): the gate used to be "toggle OR env force-on". A stale
# TRIDENT_AGENT_WATCH_ENABLED=1 in the environment silently overrode an OFF
# toggle, arming the scheduler nobody armed — the rogue-watcher incident where
# the watch loop autonomously grabbed issues. The env force-on was dropped so
# the dashboard toggle is authoritative; watch_enabled() survives only as an
# informational reader (watch_gate_status reports it for visibility).

# The legacy env flag name. DEPRECATED: read for visibility only — it gates
# NOTHING. The dashboard toggle is the only arm control now.
WATCH_ENABLED_ENV = "TRIDENT_AGENT_WATCH_ENABLED"

_TRUTHY = {"1", "true", "yes", "on"}


def watch_enabled() -> bool:
    """DEPRECATED / informational only — True if the legacy env flag is set truthy.

    This NO LONGER GATES anything. The sole control over the autonomous watch
    loop is the persisted dashboard toggle (AgentManager.watch_gate_on). This
    reader survives so watch_gate_status() can surface "legacy env is set but
    IGNORED" for operator visibility — it must NOT be used to arm the loop.

    The env force-on was dropped 2026-06-23 because a stale value could override
    an OFF toggle and arm the scheduler nobody armed (the rogue-watcher incident).
    """
    return os.environ.get(WATCH_ENABLED_ENV, "").strip().lower() in _TRUTHY


# ═══════════════════════════════════════════════════════════════════════════════
# Pure core 1 — issue → claude prompt
# ═══════════════════════════════════════════════════════════════════════════════

# The 🤖 generated footer every autonomous PR carries (mirrors CLAUDE.md's PR
# footer convention).
PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


def build_issue_prompt(issue: Issue) -> str:
    """Build the claude prompt for an issue. PURE.

    The prompt instructs the headless agent to implement the issue ON THE CURRENT
    (already-checked-out worktree) branch, commit its work, and stop — the
    ORCHESTRATOR opens the PR, not the agent (the agent's pre-push hook would
    block any push to main anyway, and PR creation is the pipeline's job).
    """
    body = (issue.body or "").strip() or "(no description provided)"
    return (
        f"You are an autonomous engineering agent working GitHub issue #{issue.number} "
        f"on the Trident repo.\n\n"
        f"TITLE: {issue.title}\n\n"
        f"DESCRIPTION:\n{body}\n\n"
        "You are already inside a fresh git worktree on a dedicated feature branch "
        f"(agent/issue-{issue.number}-...). Implement the issue here.\n\n"
        "RULES:\n"
        "- Read CLAUDE.md and the relevant wiki pages before touching code.\n"
        "- Make the change, add/adjust tests, and COMMIT your work on this branch.\n"
        "- Ship a SOURCE-ONLY change: do NOT build or commit the built dashboard "
        "bundle (`dashboard_v2/`). Never run `npm run deploy:dashboard` or stage "
        "anything under `dashboard_v2/` — the bundle is rebuilt once at merge time, "
        "and a pre-push hook will REJECT any push whose diff touches it.\n"
        "- Do NOT push to main and do NOT open the PR yourself — the orchestrator "
        "opens the PR (it will use `Closes #" + str(issue.number) + "`).\n"
        "- You may FILE a new GitHub issue if you spot follow-up work, but file it "
        "as `needs-triage` and NEVER apply the `ready-for-agent` label — a human "
        "decides what the agent works on. (The orchestrator reverts any issue you "
        "create that carries `ready-for-agent`, so self-applying it does nothing.)\n"
        "- Never read secrets/the keyring; never push or merge to main (a pre-push "
        "hook enforces this).\n"
        "- When done, ensure the working tree is committed so the orchestrator can "
        "open a PR from the branch.\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# model + prompt → headless claude launch command (file-backed; see #190)
# ═══════════════════════════════════════════════════════════════════════════════

# Headless / unattended claude flags chosen for autonomous runs:
#   -p                                print/headless mode (no interactive REPL)
#   --dangerously-skip-permissions    never stall on an approval prompt
# Containment for the loosened permissions comes from worktree isolation + the
# #34 pre-push hook + the #34 launch-command guard — NOT from claude's prompts.
CLAUDE_HEADLESS_FLAGS = "-p --dangerously-skip-permissions"


def new_transcript_id() -> str:
    """A fresh Claude Code session UUID to pin a run's transcript filename.

    Generated by the caller, passed to ``build_claude_command``/``build_run_command``
    as ``session_id`` (becomes ``claude --session-id <uuid>``) AND stored on the
    run record's ``transcript_id`` so the dashboard feed resolves the run's own
    transcript deterministically instead of guessing the newest file in a shared
    project dir. One id per logical claude session."""
    return str(uuid.uuid4())


def build_claude_command(
    *, model: str, prompt: str, stdout_path: str | None = None,
    session_id: str | None = None, _write_prompt_file=None
) -> str:
    """Build the headless claude launch command.

    The prompt is base64-encoded, written to a TEMP FILE, and the command reads
    the file back, decodes it, and pipes it to claude's STDIN. The prompt is
    NEVER placed on the command line. Two footguns are defended here:

      * #169 — PowerShell 5.1 word-splits a prompt passed as an argv string, so
        claude saw stray tokens like ``-m`` (from "python -m pytest") as flags →
        instant no-op exit. base64 + stdin keeps the prompt off argv entirely.
      * #190 — the pty-manager spawns this command as ``powershell.exe -Command
        <command>``, and Windows hard-caps a process command line at 32767 chars.
        Kleiner (the reviewer) embeds the FULL PR diff in his prompt, so a real
        review's base64'd-INLINE command ran ~100KB and ``CreateProcess`` failed
        silently inside ``PtySession.__init__`` — before the session was even
        created — so EVERY review with a non-trivial diff died with a misleading
        "claude did not start" (2026-06-24). Spilling the base64 to a file keeps
        the command line at ~200 chars no matter how large the prompt/diff is.

    The command deletes the temp file immediately after reading it (self-cleaning),
    so the prompt never lingers on disk and no caller-side cleanup is needed.

    ``stdout_path`` (optional): when given, claude's stdout is captured to that
    file as UTF-8 (``Out-File -Encoding utf8``) IN ADDITION to whatever the prompt
    asks claude to write. ``claude -p`` prints its full answer to stdout, so this
    is the AUTHORITATIVE record of what the agent produced — it lets a caller (a)
    use the printed answer as a fallback when the agent didn't write its expected
    artifact, and (b) leave a TRUTHFUL halt note ("claude produced no output" vs
    "claude said X but wrote no file") instead of a guess. Read it back with
    ``utf-8-sig`` (PS 5.1 Out-File writes a BOM).

    ``session_id`` (a uuid): when given, ``--session-id <uuid>`` is appended so
    Claude Code writes the transcript under a KNOWN filename the dashboard feed
    can resolve. The caller stores the same id on the run record's
    ``transcript_id``. Without it Claude picks a random uuid and the feed falls
    back to the newest transcript in the (often shared) project dir — the feed
    cross-wiring bug.

    ``_write_prompt_file`` is an injectable ``(text) -> path`` seam so tests can
    capture the prompt without touching disk; production uses a real temp file.

    Result: ``$p=<decode b64 read from file>; Remove-Item <file>; $p | <model> -p
    --dangerously-skip-permissions [| Out-File <stdout_path>]``. ``<model>`` is the
    launcher verb (claude / claude-ds / claude-go / claude-ds-go); the watch path
    always uses bare ``claude`` (on PATH, so it resolves under the pty-manager's
    ``-NoProfile`` shell).
    """
    b64 = base64.b64encode((prompt or "").encode("utf-8")).decode("ascii")
    write = _write_prompt_file or _write_prompt_tempfile
    path = write(b64)
    # Escape ' for a PowerShell single-quoted literal (temp paths never contain
    # one, but the predicate must be correct regardless of the OS temp dir).
    ps_path = path.replace("'", "''")
    # Pin the transcript filename so the dashboard feed resolves THIS session's
    # transcript (not the newest unrelated one in a shared project dir). Comes
    # before any `| Out-File` so the flag lands on claude, not the pipe.
    sid = f" --session-id {session_id}" if session_id else ""
    run = f"$p | {model} {CLAUDE_HEADLESS_FLAGS}{sid}"
    if stdout_path:
        sp = stdout_path.replace("'", "''")
        # Capture stdout (NOT 2>&1 — merging a native exe's stderr in PS 5.1 wraps
        # each line in a NativeCommandError ErrorRecord, footgun territory). The
        # agent's answer is on stdout; stderr stays on the pty for live view.
        run = f"{run} | Out-File -FilePath '{sp}' -Encoding utf8"
    provider_prefix = provider.get_provider_env_prefix()
    return (
        f"{provider_prefix}"
        f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
        f"[IO.File]::ReadAllText('{ps_path}'))); "
        f"Remove-Item -LiteralPath '{ps_path}' -ErrorAction SilentlyContinue; "
        f"{run}"
    )


def _write_prompt_tempfile(text: str) -> str:
    """Write ``text`` (the base64 prompt) to a temp file and return its absolute
    path. ASCII is sufficient — the content is base64. The launched command
    self-deletes the file right after reading it ([[footguns#190]])."""
    fd, path = tempfile.mkstemp(prefix="agent_prompt_", suffix=".b64")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# Pure core 3 — test result → PR decision (normal vs draft)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TestOutcome:
    """The parsed result of the worktree's test-gate run.

    ``passed`` is the COMBINED gate: True only when BOTH pytest AND the frontend
    typecheck (when it ran) are green. The frontend fields default to "skipped"
    (``fc_passed=True, fc_checked=False``) so pure-backend changes and pre-#184
    callers work without changes.
    """

    # Not a pytest test class despite the name (pytest would otherwise try to
    # collect it because it starts with "Test").
    __test__ = False

    passed: bool
    summary: str = ""   # short one-liner, e.g. "42 passed" / "3 failed"
    output: str = ""    # the captured pytest output (tail), embedded in a draft body

    # Frontend typecheck gate (#184 / #210). Defaults to "skipped / green" so
    # existing callers don't need changes — the combined gate stays green when no
    # frontend check ran.
    fc_passed: bool = True     # True when skipped (no web/ changes) or tsc exits 0
    fc_summary: str = ""
    fc_output: str = ""
    fc_checked: bool = False   # True only when tsc actually ran


@dataclass(frozen=True)
class PrDecision:
    """How to open the PR for a finished run."""

    draft: bool
    body: str
    label_add: str = NEEDS_REVIEW_LABEL
    label_remove: str = WORKING_LABEL


# Cap on how much test output we embed in a draft PR body so the body stays sane.
_MAX_OUTPUT_IN_BODY = 6000


def decide_pr(outcome: TestOutcome, *, issue_number: Optional[int] = None) -> PrDecision:
    """Map a test outcome to a PR decision. PURE.

    Green (both pytest AND frontend gate pass) ⇒ normal PR whose body reports the
    passing summary. Red (either gate failed) ⇒ DRAFT PR marked FAILED with the
    test output embedded for human inspection. Both flip the issue to
    ``needs-review`` (Kleiner reviews the draft and may recommend reject).
    ``issue_number`` is optional here (the orchestrator prepends ``Closes #N`` +
    the footer) so this core stays purely about the green/red branch.

    When the frontend typecheck gate ran (#184 / #210), its result is shown
    alongside pytest so the reviewer sees both gates at a glance.
    """
    out = (outcome.output or "")
    if len(out) > _MAX_OUTPUT_IN_BODY:
        out = out[-_MAX_OUTPUT_IN_BODY:]

    fc_out = (outcome.fc_output or "")
    if len(fc_out) > _MAX_OUTPUT_IN_BODY:
        fc_out = fc_out[-_MAX_OUTPUT_IN_BODY:]

    # Build the combined gate line — shows which gate(s) failed.
    if outcome.fc_checked:
        gate_line = (
            f"Test gate: **{outcome.summary or ('passed' if outcome.passed else 'failed')}"
            f"** | frontend: **{outcome.fc_summary}**.\n\n"
        )
    else:
        gate_line = f"Test gate: **{outcome.summary or 'passed'}**.\n\n"

    if outcome.passed:
        body = (
            "## Autonomous run — tests green\n\n"
            + gate_line +
            "All tests passed in the worktree before this PR was opened.\n"
        )
        return PrDecision(draft=False, body=body)

    # At least one gate failed — draft PR with the failing output(s) embedded.
    sections = []
    if outcome.output:
        # pytest output is ALWAYS present (even if pytest passed, the frontend
        # check is the one that failed — still show pytest output for context).
        label = "pytest output (tail)" if not outcome.output.strip().startswith("FAIL") else "pytest output (FAILED)"
        sections.append(
            f"<details><summary>{label}</summary>\n\n"
            "```\n" + out + "\n```\n\n</details>\n"
        )
    if outcome.fc_checked and outcome.fc_output:
        fc_label = "frontend typecheck output" if outcome.fc_passed else "frontend typecheck output (FAILED)"
        sections.append(
            f"<details><summary>{fc_label}</summary>\n\n"
            "```\n" + fc_out + "\n```\n\n</details>\n"
        )

    body = (
        "## Autonomous run — tests FAILED (draft)\n\n"
        + gate_line +
        "This PR is a DRAFT: the autonomous run could not get the worktree to a "
        "green test suite. Review the output below before taking it further.\n\n"
        + "\n".join(sections)
        + "\n"
    )
    return PrDecision(draft=True, body=body)


# ═══════════════════════════════════════════════════════════════════════════════
# Side-effecting seam protocols (all injected — fakeable in tests)
# ═══════════════════════════════════════════════════════════════════════════════

class GatewayLike(Protocol):
    def select_next_issue(self) -> Optional[Issue]: ...
    def set_labels(self, number: int, add=None, remove=None) -> None: ...
    def open_pr(self, title: str, body: str, head: str, base: str = "main") -> str: ...
    def list_open_issues(self) -> list: ...
    def leave_bot_comment(self, number: int, bot_name: str, sentence: str) -> None: ...


class WorktreeMakerLike(Protocol):
    def create(self, *, issue_number: int, title: str) -> WorktreeInfo: ...


# hook_installer:  (worktree_path) -> hook_path
# run_launcher:    (*, command, cwd, tag) -> bool   (True = run started OK)
# test_runner:     (cwd) -> TestOutcome


# ═══════════════════════════════════════════════════════════════════════════════
# Default real test runner — system Python 3.12 pytest in the worktree
# ═══════════════════════════════════════════════════════════════════════════════

# The grade is SCOPED to the files a run changed (Option A) rather than running
# the whole ~1760-test suite — which is slow, CPU-heavy (backtests), full of
# production-host side effects, and once killed the manager that ran it
# ([[footguns#170]]). A hard timeout bounds a slow/hung scoped grade.
GRADE_TIMEOUT_S = 300.0

_TEST_FILE_RE = re.compile(r"^(test_.+|.+_test)\.py$")


def _is_test_file(path: str) -> bool:
    """PURE. True if ``path``'s filename looks like a pytest module."""
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_TEST_FILE_RE.match(name))


def select_test_targets(changed_files, existing_tests) -> list:
    """PURE. Pick the pytest targets to grade with, scoped to a run's change.

    - ``changed_files``: repo-relative paths the run added/modified.
    - ``existing_tests``: set of test-file paths that exist in the worktree, used
      to map a changed source file ``foo.py`` → its ``test_foo.py``.

    A changed file that IS a test runs directly; a changed non-test ``.py`` pulls
    in any existing ``test_<stem>.py`` / ``<stem>_test.py``. Returns a sorted,
    de-duplicated list — empty ⇒ nothing to grade.
    """
    targets: set = set()
    for raw in changed_files:
        f = (raw or "").strip().replace("\\", "/")
        if not f:
            continue
        if _is_test_file(f):
            targets.add(f)
        elif f.endswith(".py"):
            stem = f.rsplit("/", 1)[-1][:-3]
            wanted = {f"test_{stem}.py", f"{stem}_test.py"}
            for t in existing_tests:
                if t.replace("\\", "/").rsplit("/", 1)[-1] in wanted:
                    targets.add(t.replace("\\", "/"))
    return sorted(targets)


def _git_changed_files(cwd: str) -> list:
    """Repo-relative paths the worktree's branch changed vs origin/main (merge-base
    diff, so an advancing origin/main never pulls in unrelated files). [] on error."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return []
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def _existing_test_files(cwd: str) -> set:
    """Test-file paths (repo-relative, posix) under ``tests/`` in the worktree."""
    base = Path(cwd)
    out: set = set()
    tests_dir = base / "tests"
    if tests_dir.is_dir():
        for p in tests_dir.rglob("*.py"):
            if _is_test_file(p.name):
                out.add(p.relative_to(base).as_posix())
    return out


def run_pytest_in_worktree(cwd: str | os.PathLike, *, timeout_s: float = GRADE_TIMEOUT_S) -> TestOutcome:
    """Grade a run's worktree on SYSTEM Python 3.12 (NOT the incomplete .venv),
    SCOPED to the files the run changed ([[footguns#170]]) and bounded by
    ``timeout_s``. No changed tests ⇒ a pass with a note (the human reviewer
    covers broader regressions).

    ⚠️ Tests of the pipeline NEVER call this — they inject a fake — so the real
    recursive pytest never runs inside the suite.
    """
    cwd = str(cwd)
    changed = _git_changed_files(cwd)
    targets = select_test_targets(changed, _existing_test_files(cwd))
    if not targets:
        return TestOutcome(
            passed=True,
            summary="no test targets for the changed files — grade skipped",
            output="changed files:\n" + "\n".join(changed),
        )
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", "-q", *targets],
            cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        return TestOutcome(
            passed=False,
            summary=f"grade timed out after {timeout_s:.0f}s on {len(targets)} file(s)",
            output=out,
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = proc.returncode == 0
    # The summary line is pytest's last non-empty line (e.g. "42 passed in 1.2s").
    summary = ""
    for line in reversed(output.splitlines()):
        if line.strip():
            summary = line.strip()
            break
    return TestOutcome(
        passed=passed,
        summary=f"[scoped: {len(targets)} file(s)] {summary}",
        output=output,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Branch push — `gh pr create` opens the PR from the REMOTE branch, so the run's
# feature branch must be pushed to origin first.
# ═══════════════════════════════════════════════════════════════════════════════

def push_branch_to_origin(cwd: str | os.PathLike, branch: str, *, timeout_s: float = 120.0) -> None:
    """Push the run's feature branch to origin. Runs from the WORKTREE so the #34
    pre-push guard hook applies — it allows feature branches and blocks only
    main/master. Raises on failure (the pipeline then bounces to ready-for-human).

    The default ``branch_pusher`` for production; pipeline tests inject a fake so
    no real push ever happens in the suite.
    """
    proc = subprocess.run(
        ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
        cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git push of {branch!r} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PipelineResult
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PipelineResult:
    """Outcome of one ``process()`` call (for logging / the scheduler / tests)."""

    acted: bool                       # did we claim & work an issue this call?
    issue_number: Optional[int] = None
    branch: Optional[str] = None
    draft: bool = False               # True ⇒ a draft (failed-tests) PR
    failed: bool = False              # True ⇒ run/worktree error path (no PR)
    pr_url: Optional[str] = None
    reason: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# IssuePrPipeline — the orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class IssuePrPipeline:
    """Drives one issue→PR cycle. All side-effecting seams are injected.

    The orchestration is the only place these seams meet; keeping each seam a
    plain callable/object means a test drives the whole flow with fakes and the
    production wiring (real gateway / real worktree / real claude / real pytest)
    is assembled by the caller in the scheduler.
    """

    def __init__(
        self,
        *,
        gateway: GatewayLike,
        worktree_maker: WorktreeMakerLike,
        hook_installer: Callable[[str], object],
        run_launcher: Callable[..., bool],
        test_runner: Callable[[str], TestOutcome],
        branch_pusher: Optional[Callable[[str, str], None]] = None,
        run_recorder: Optional["RunRecorder"] = None,
        bot_name: str = "Gordon Freeman",
        frontend_checker: Optional[Callable[[str], tuple[bool, str, str, bool]]] = None,
    ) -> None:
        self._gw = gateway
        self._worktree = worktree_maker
        self._install_hook = hook_installer
        self._launch = run_launcher
        self._test = test_runner
        # The agent identity stamped on provenance comments. Gordon IS this
        # pipeline (the issue→PR worker); later pipelines (Kleiner, Dr. Eli Vance) reuse
        # the same constructor pattern with their own name.
        self._bot_name = bot_name
        # Pushes the feature branch to origin before the PR is opened. Defaults to
        # a no-op so a test that doesn't care about the push isn't forced to wire
        # it (and never pushes for real); production injects push_branch_to_origin.
        self._push_branch = branch_pusher or (lambda cwd, branch: None)
        self._recorder = run_recorder or _NullRunRecorder()
        # Frontend typecheck gate (#184 / #210). Defaults to None (skip) so tests
        # that don't wire it are unaffected. Production wires run_frontend_check.
        self._frontend_check = frontend_checker

    # ── the cycle ──────────────────────────────────────────────────────────────

    def process(self) -> PipelineResult:
        """Run ONE issue→PR cycle. Returns a :class:`PipelineResult`.

        No eligible issue ⇒ no-op (acted=False). Otherwise claims the issue,
        builds it in a worktree, test-gates it, and opens a PR (normal or draft).
        On any hard failure it HALTS the run to ``needs-human`` (Slice F), keeps
        the worktree/branch, and DOES NOT retry.
        """
        issue = self._gw.select_next_issue()
        if issue is None:
            _LOG.info("watch poll: no eligible ready-for-agent issue")
            return PipelineResult(acted=False, reason="no eligible issue")

        n = issue.number
        # 1) Claim it: ready-for-agent → agent-working (prevents double-pick).
        self._gw.set_labels(n, remove=[READY_LABEL], add=[WORKING_LABEL])
        self._recorder.start(issue)
        _LOG.info("claimed issue #%d (%s)", n, issue.title)

        # 2) Worktree off latest main.
        try:
            wt: WorktreeInfo = self._worktree.create(issue_number=n, title=issue.title)
        except WorktreeError as e:
            return self._fail(n, branch=None, note=f"worktree creation failed: {e}")
        except Exception as e:  # defensive
            return self._fail(n, branch=None, note=f"worktree creation error: {e}")

        # 2b) Pin the worktree as the run's working directory. The run record was
        #     created in start() BEFORE the worktree existed, so its cwd was the
        #     repo root — and the live feed derives the transcript dir from cwd.
        #     Claude Code writes this run's transcript under the WORKTREE's slug
        #     (it launches with cwd=worktree), so a repo-root cwd made the feed
        #     resolve the newest UNRELATED transcript in the operator's own
        #     project dir. Repointing cwd at the worktree gives the feed a dir
        #     that holds exactly this run's transcript ([[footguns#171]], 2026-06-21).
        wr = getattr(self._recorder, "worktree_ready", None)
        if callable(wr):
            try:
                wr(n, cwd=str(wt.path), branch=wt.branch)
            except Exception:
                _LOG.exception("could not pin worktree cwd on run for #%d", n)

        # 3) Install the #34 pre-push guard hook into the worktree.
        try:
            self._install_hook(wt.path)
        except Exception as e:
            return self._fail(n, branch=wt.branch,
                              note=f"pre-push hook install failed: {e}")

        # 3b) Snapshot the open-issue set BEFORE the agent runs (#55). The agent
        #     may file follow-up issues during its session; any it CREATES that
        #     carry ready-for-agent get reverted to needs-triage afterwards, so it
        #     can never make its own self-invented scope eligible (the #38/#39/#40
        #     failure mode in #50). Snapshot failure is non-fatal — we just can't
        #     diff, so we skip the revert (logged) rather than abort the run.
        pre_issue_numbers = self._snapshot_open_issue_numbers()

        # 4) Run claude in the worktree (headless, loosened permissions). The
        #    launch command is guarded by #34's enforce_run_command upstream.
        #    Pin a transcript id so the dashboard feed resolves THIS run's
        #    transcript deterministically (the worktree cwd was already pinned on
        #    the run via worktree_ready above; the id pins the exact file).
        transcript_id = new_transcript_id()
        command = build_claude_command(
            model=_model_for_issue(issue), prompt=build_issue_prompt(issue),
            session_id=transcript_id,
        )
        _attach = getattr(self._recorder, "attach_session", None)
        if callable(_attach):
            try:
                _attach(n, transcript_id=transcript_id)
            except Exception:
                _LOG.exception("could not pin transcript id on run for #%d", n)
        tag = f"agent:issue-{n}"
        launch_error: Optional[str] = None
        ok = False
        try:
            ok = self._launch(command=command, cwd=wt.path, tag=tag)
        except Exception as e:
            launch_error = f"run launch error: {e}"

        # 4b) #55 guard: revert any issue the agent CREATED this run that it
        #     self-labelled ready-for-agent → needs-triage. Runs BEFORE the
        #     failure early-returns below — a run that errored or TIMED OUT may
        #     still have filed a self-promoted issue before it died, and leaving it
        #     ready-for-agent would make it eligible for re-grab next poll. So the
        #     guard runs on every post-launch path (ok, failed, or timed out), not
        #     just the happy one.
        self._revert_self_promoted_issues(pre_issue_numbers)

        if launch_error is not None:
            return self._fail(n, branch=wt.branch, note=launch_error)
        if not ok:
            # Truthful halt note: the launcher (a LaunchOutcome) carries WHY —
            # timeout vs failed-to-launch vs raised (#99 timed out 30 min and was
            # mislabelled "did not start"). A bool-returning fake has no reason →
            # fall back to the generic phrasing.
            why = getattr(ok, "reason", "") or "claude run did not start / errored"
            return self._fail(n, branch=wt.branch, note=why)

        # 5) TEST-GATE in the worktree (system Python 3.12 pytest).
        try:
            outcome = self._test(wt.path)
        except Exception as e:
            return self._fail(n, branch=wt.branch, note=f"test gate error: {e}")

        # 5fa) Frontend typecheck gate (#184 / #210). Runs when wired (the real
        #      pipeline wires run_frontend_check from trident_eli_runtime; tests
        #      wire None to skip). The check internally gates on whether any
        #      ``web/`` files changed — pure-backend diffs skip it with zero cost.
        #      Runs even when pytest failed so the combined outcome shows BOTH
        #      gates (the same shape as Eli's land feed, [[footguns#210]]).
        if self._frontend_check is not None:
            try:
                fc_passed, fc_summary, fc_output, fc_checked = self._frontend_check(
                    str(wt.path))
                outcome = TestOutcome(
                    passed=outcome.passed and fc_passed,
                    summary=outcome.summary,
                    output=outcome.output,
                    fc_passed=fc_passed,
                    fc_summary=fc_summary,
                    fc_output=fc_output,
                    fc_checked=fc_checked,
                )
            except Exception as e:
                return self._fail(
                    n, branch=wt.branch, note=f"frontend check error: {e}")

        # 5b) Push the feature branch to origin — `gh pr create` opens the PR from
        #     the REMOTE branch, so it must exist there first. Goes through the
        #     worktree's pre-push guard hook (feature branches allowed; main blocked).
        try:
            self._push_branch(wt.path, wt.branch)
        except Exception as e:
            return self._fail(n, branch=wt.branch, note=f"branch push failed: {e}")

        # 6) Decide + open the PR (normal if green, draft if red).
        decision = decide_pr(outcome, issue_number=n)
        title = ("[draft] " if decision.draft else "") + f"#{n}: {issue.title}"
        body = (
            f"Closes #{n}\n\n"
            + decision.body
            + "\n\n---\n"
            + PR_FOOTER
            + "\n"
        )
        try:
            pr_url = self._gw.open_pr(
                title=title, body=body, head=wt.branch, base="main"
            )
        except Exception as e:
            return self._fail(n, branch=wt.branch, note=f"PR creation failed: {e}")

        # 7) Issue → needs-review; leave a Gordon provenance comment; mark done.
        self._gw.set_labels(
            n, remove=[decision.label_remove], add=[decision.label_add]
        )
        # Best-effort provenance: a comment failure must NOT crash the run — the
        # PR is already open, and provenance is nice-to-have, not load-bearing.
        if decision.draft:
            sentence = f"opened draft PR {pr_url} — tests failed, needs a look"
        else:
            sentence = f"opened PR {pr_url} implementing this issue — tests pass"
        try:
            self._gw.leave_bot_comment(n, self._bot_name, sentence)
        except Exception:
            _LOG.warning("could not leave provenance comment on #%d", n)
        self._recorder.finish(n, succeeded=not decision.draft, pr_url=pr_url)
        _LOG.info("issue #%d → PR %s (draft=%s)", n, pr_url, decision.draft)
        return PipelineResult(
            acted=True, issue_number=n, branch=wt.branch,
            draft=decision.draft, failed=False, pr_url=pr_url,
        )

    # ── #55 guard: the agent can't self-promote issues it files ────────────────

    def _snapshot_open_issue_numbers(self) -> Optional[set]:
        """The set of open issue numbers BEFORE the agent runs, or None if it
        can't be read (then the revert is skipped — fail-open, never abort)."""
        try:
            return {i.number for i in self._gw.list_open_issues()}
        except Exception:
            _LOG.warning("could not snapshot open issues — #55 self-promote "
                         "revert is skipped this run")
            return None

    def _revert_self_promoted_issues(self, pre_numbers: Optional[set]) -> None:
        """Strip ready-for-agent → needs-triage from any issue the agent
        SELF-PROMOTED this run. No-op if the pre-run snapshot failed.

        Two gates, both must hold before a revert fires:

        1. The number isn't in the pre-run snapshot (appeared during the run).
        2. It carries ready-for-agent AND a human didn't apply that label.

        Gate 2's human check is the fix for the silent-clobber bug: the operator
        can file a legitimate ready-for-agent issue WHILE a run is in flight — its
        number also fails gate 1, so the number-diff alone would revert the
        operator's prioritised work with no error. ``ready_label_applied_by_human``
        consults the ``labeled`` timeline-event actor (the same mechanism #52's
        eligibility check uses) and, when it confirms a human handed the issue
        over, the revert is skipped. On the shared settingzbot account (no distinct
        ``bot_login``) that check can't attribute the actor and returns False, so
        the guard falls back to the number-diff behaviour.

        An issue the agent filed correctly as needs-triage (or with no triage
        label) carries no ready-for-agent label and is left alone by gate 2."""
        if pre_numbers is None:
            return
        try:
            after = self._gw.list_open_issues()
        except Exception:
            _LOG.warning("could not re-list open issues — #55 revert skipped")
            return
        for issue in after:
            if issue.number in pre_numbers:
                continue  # existed before the run — not the agent's creation
            if READY_LABEL not in (issue.labels or ()):
                continue  # filed without the eligibility label — fine, leave it
            if self._label_applied_by_human(issue.number):
                # A human handed this issue to the agent mid-run — not a self
                # promotion. Keep the label; reverting it would silently bury the
                # operator's prioritised work.
                _LOG.info(
                    "#55 guard: keeping #%d — ready-for-agent was applied by a "
                    "human during the run, not self-promoted", issue.number)
                continue
            try:
                self._gw.set_labels(
                    issue.number, remove=[READY_LABEL], add=[NEEDS_TRIAGE_LABEL])
                _LOG.warning(
                    "#55 guard: reverted agent-created issue #%d "
                    "(ready-for-agent → needs-triage)", issue.number)
            except Exception:
                _LOG.exception(
                    "#55 guard: could not revert agent-created issue #%d",
                    issue.number)

    def _label_applied_by_human(self, number: int) -> bool:
        """Whether a human applied ready-for-agent to #number (gateway timeline
        check). Defensive: a gateway that predates the method, or a read error,
        degrades to False so the revert falls back to number-diff behaviour rather
        than crashing the run."""
        checker = getattr(self._gw, "ready_label_applied_by_human", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(number))
        except Exception:
            _LOG.warning(
                "#55 guard: human-actor check failed for #%d — falling back to "
                "number-diff revert", number)
            return False

    # ── failure path: bounce to human, keep worktree, no retry ─────────────────

    def _fail(self, n: int, *, branch: Optional[str], note: str) -> PipelineResult:
        """HALT the run (Slice F escape hatch): flip the issue to ``needs-human``,
        keep the worktree/branch for inspection, post a 🤖 halt provenance comment,
        mark the run ``halted`` (NOT failed), and DO NOT retry.

        A hard failure here (worktree error, run error, push/PR-creation error) is
        something beyond a simple fix — it needs Nathan's judgment — so it surfaces
        on the dashboard's red pill instead of sitting in the old ready-for-human."""
        _LOG.warning("issue #%d halted: %s", n, note)
        report = HaltReport(
            agent_name=self._bot_name, issue_number=n, stage="build", reason=note)
        try:
            self._gw.set_labels(n, remove=[WORKING_LABEL], add=[NEEDS_HUMAN_LABEL])
        except Exception:
            _LOG.exception("could not bounce issue #%d to needs-human", n)
        # Best-effort 🤖 halt provenance comment (the halt report on the run is the
        # canonical record; the comment is the human-visible breadcrumb).
        try:
            self._gw.leave_bot_comment(
                n, self._bot_name, _halt_sentence(report))
        except Exception:
            _LOG.warning("could not leave halt provenance comment on #%d", n)
        self._recorder.halt(n, report=format_halt_report(report))
        return PipelineResult(
            acted=True, issue_number=n, branch=branch,
            draft=False, failed=True, reason=note,
        )


def _halt_sentence(report: HaltReport) -> str:
    """The SENTENCE portion of the halt provenance comment (no 🤖 prefix).

    ``leave_bot_comment`` adds the ``🤖 {bot_name}: `` prefix itself (via
    ``format_bot_comment``), so passing the whole ``format_halt_comment`` string
    would double the prefix. We re-derive the sentence so the posted comment ends
    up byte-identical to ``format_halt_comment(report)`` — same shape, one prefix.
    """
    full = format_halt_comment(report)
    # format_halt_comment == f"🤖 {name}: {sentence}". Strip the "🤖 {name}: "
    # head so leave_bot_comment re-adds exactly that prefix.
    _, _, sentence = full.partition(": ")
    return sentence or full


def _model_for_issue(issue: Issue) -> str:
    """The launcher verb to run for an issue. Defaults to ``claude`` (Anthropic).

    Kept a tiny seam so a future per-issue model selection (e.g. a label that
    picks claude-ds) has one place to live. For this slice every autonomous run
    uses the default ``claude`` launcher.
    """
    return "claude"


# ═══════════════════════════════════════════════════════════════════════════════
# RunRecorder — optional bridge to the #30 run store
# ═══════════════════════════════════════════════════════════════════════════════

class RunRecorder(Protocol):
    def start(self, issue: Issue) -> None: ...
    def worktree_ready(self, issue_number: int, *, cwd: str, branch: str) -> None: ...
    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url: Optional[str], note: str = "") -> None: ...
    def halt(self, issue_number: int, *, report: str) -> None: ...


class _NullRunRecorder:
    """No-op recorder (used when the pipeline runs without a run store)."""

    def start(self, issue: Issue) -> None:
        pass

    def worktree_ready(self, issue_number: int, *, cwd: str, branch: str) -> None:
        pass

    def finish(self, issue_number: int, *, succeeded: bool,
               pr_url: Optional[str], note: str = "") -> None:
        pass

    def halt(self, issue_number: int, *, report: str) -> None:
        pass
