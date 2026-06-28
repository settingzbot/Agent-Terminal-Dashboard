"""
trident_agent_worktree.py — git-worktree creation for the issue→PR worker
(issue #35, parent #24).

Why this module exists
----------------------
The capstone issue→PR worker must build each issue's work in an ISOLATED git
worktree off the latest ``main``, on a fresh ``agent/issue-<n>-<slug>`` branch —
it must NEVER touch the live working tree or the checked-out branch on this
production host. This module owns exactly that:

* ``branch_slug`` / ``branch_name`` — PURE functions turning an issue's number +
  title into a safe branch name. Unit-tested as a data table; no I/O.
* ``WorktreeCreator`` — the single side-effecting seam: it shells out to
  ``git fetch`` + ``git worktree add`` through an INJECTED runner so tests drive
  the whole thing with a fake and never create a real worktree.

What it deliberately does NOT do: run claude, run tests, open PRs, flip labels.
Those are the pipeline's job (trident_agent_pipeline.py) — this module is the
worktree primitive only, mirroring the gateway/guards "one concern per module"
idiom of this wave.

Safety
------
The creator only ever runs ``git fetch origin`` and ``git worktree add -b ...``.
It never checks out, resets, or switches the live tree — ``git worktree add``
creates a *separate* working directory, leaving the host's current branch
untouched. That isolation, plus the #34 pre-push hook the pipeline installs into
the new worktree, is the containment contract for autonomous runs.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

_LOG = logging.getLogger(__name__)

# Where worktrees are created, relative to the repo root. Gitignored runtime dir
# (added to .gitignore in this slice). One subdir per branch.
WORKTREES_DIRNAME = "agent_worktrees"

# The branch namespace for autonomous runs.
BRANCH_PREFIX = "agent/issue-"

# The base ref every worktree branches off. We fetch this first so the worktree
# is built on the freshest main even if the host's local main is stale.
BASE_REMOTE = "origin"
BASE_BRANCH = "main"
BASE_REF = f"{BASE_REMOTE}/{BASE_BRANCH}"

# Default cap on the slug portion of a branch name so the full ref stays sane.
DEFAULT_SLUG_MAX = 50


class WorktreeError(RuntimeError):
    """Raised when a git worktree operation fails in a way the caller handles."""


@dataclass(frozen=True)
class GitResult:
    """The slice of subprocess.CompletedProcess this module reads."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class WorktreeInfo:
    """The result of creating a worktree."""

    branch: str
    path: str


# ─────────────────────────────────────────────────────────────────────────────
# Pure slug helpers (no I/O)
# ─────────────────────────────────────────────────────────────────────────────

_NON_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_DASH_RUN_RE = re.compile(r"-{2,}")


def branch_slug(title: str, *, max_len: int = DEFAULT_SLUG_MAX) -> str:
    """Turn an issue title into a safe, lowercase branch slug. PURE.

    Lowercases, replaces every run of non ``[a-z0-9_-]`` characters with a single
    dash, collapses dash runs, trims leading/trailing dashes, and truncates to
    ``max_len`` (without leaving a trailing dash). Falls back to ``"issue"`` when
    nothing usable remains, so the branch name is always well-formed.
    """
    s = (title or "").lower()
    s = _NON_SLUG_RE.sub("-", s)
    s = _DASH_RUN_RE.sub("-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "issue"


def branch_name(issue_number: int, title: str, *, max_len: int = DEFAULT_SLUG_MAX) -> str:
    """``agent/issue-<n>-<slug>`` for an issue. PURE."""
    return f"{BRANCH_PREFIX}{int(issue_number)}-{branch_slug(title, max_len=max_len)}"


def _worktree_dirname(branch: str) -> str:
    """A filesystem-safe directory name for a branch (slashes → dashes)."""
    return branch.replace("/", "-")


# ─────────────────────────────────────────────────────────────────────────────
# Default real git runner
# ─────────────────────────────────────────────────────────────────────────────

def _default_runner(repo_root: str) -> Callable[[Sequence[str]], GitResult]:
    """A runner that invokes the real ``git`` CLI in ``repo_root``."""

    def run(argv: Sequence[str]) -> GitResult:
        proc = subprocess.run(
            ["git", *argv],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    return run


# ─────────────────────────────────────────────────────────────────────────────
# WorktreeCreator — the single side-effecting seam
# ─────────────────────────────────────────────────────────────────────────────

class WorktreeCreator:
    """Creates an isolated worktree off latest ``main`` on a fresh agent branch.

    The git side-effect goes through ``self._run`` (the injected runner), so the
    whole class is unit-testable with ``git`` stubbed — no real worktree is ever
    created in the suite.
    """

    def __init__(
        self,
        repo_root: str,
        *,
        runner: Callable[[Sequence[str]], GitResult] | None = None,
        worktrees_dir: str | None = None,
    ) -> None:
        self.repo_root = str(repo_root)
        self._run = runner or _default_runner(self.repo_root)
        self._worktrees_dir = Path(worktrees_dir or Path(self.repo_root) / WORKTREES_DIRNAME)

    def create(self, *, issue_number: int, title: str) -> WorktreeInfo:
        """Fetch latest ``origin/main`` and add a worktree on a new agent branch.

        Returns the :class:`WorktreeInfo`. Raises :class:`WorktreeError` if either
        git call fails (the caller — the pipeline — treats that as a run failure
        and does NOT retry). Never touches the live working tree or branch.

        Re-run safety (issue #117): the branch+path are a DETERMINISTIC function
        of the issue number/title, and a halted run keeps both by design. So a
        re-pick of the same issue would collide on the existing branch AND path
        and halt forever. We reclaim that stale state first (see
        ``_reclaim_stale``), and we ensure ``core.longpaths`` is set so a deep
        checkout inside the worktree can't trip Windows' 260-char MAX_PATH limit
        (the #114 ``Filename too long`` halt).
        """
        branch = branch_name(issue_number, title)
        path = self._worktrees_dir / _worktree_dirname(branch)

        # 1) Refresh the base ref so we branch off the freshest main.
        fetched = self._run(["fetch", BASE_REMOTE, BASE_BRANCH])
        if fetched.returncode != 0:
            raise WorktreeError(
                f"git fetch {BASE_REMOTE} {BASE_BRANCH} failed "
                f"(exit {fetched.returncode}): {fetched.stderr.strip()}"
            )

        # 2) Windows MAX_PATH defense-in-depth. A worktree checks out the WHOLE
        #    repo, so a deep file inside it (e.g. the 154-char Notion-export paths
        #    under "Nathans smart money strategy/…") plus the worktree-dir prefix
        #    can exceed 260 chars and make `git worktree add` die with "Filename
        #    too long" (#114). The operator's `git config --global core.longpaths
        #    true` hotfix is machine-local and dies on a fresh clone; setting it
        #    on THIS repo before every add is the durable, clone-surviving fix.
        #    Best-effort: it may already be set, and the slug cap below is the
        #    second layer, so a config hiccup must not block the run.
        self._ensure_longpaths()

        # 3) Reclaim any stale worktree+branch a previous (halted) run left at the
        #    deterministic branch/path so the re-pick doesn't collide and halt.
        self._reclaim_stale(branch, path)

        # 4) Create the worktree on a NEW branch off origin/main. `git worktree
        #    add -b <branch> <path> <base>` makes a separate working dir — the
        #    host's current branch is never touched.
        added = self._run([
            "worktree", "add",
            "-b", branch,
            str(path),
            BASE_REF,
        ])
        if added.returncode != 0:
            raise WorktreeError(
                f"git worktree add for {branch} failed "
                f"(exit {added.returncode}): {added.stderr.strip()}"
            )

        _LOG.info("created worktree %s on branch %s", path, branch)
        return WorktreeInfo(branch=branch, path=str(path))

    def _ensure_longpaths(self) -> None:
        """Set ``core.longpaths=true`` on this repo before any ``git worktree
        add`` (Windows MAX_PATH defense). Best-effort: a failure is logged but
        never raised — longpaths may already be set globally, and the slug cap is
        the independent second layer."""
        res = self._run(["config", "core.longpaths", "true"])
        if res.returncode != 0:
            _LOG.warning(
                "could not set core.longpaths (exit %d): %s — relying on the slug "
                "cap alone for MAX_PATH safety",
                res.returncode, res.stderr.strip(),
            )

    def _reclaim_stale(self, branch: str, path: Path) -> None:
        """Remove a stale worktree+branch left by a previous (halted) run so a
        re-pick of the same issue doesn't collide on the deterministic branch/path.

        Each step is independently best-effort — a non-zero exit just means there
        was nothing to reclaim (first run on this issue), so we only LOG when
        something was actually reclaimed (the reclaim must not be silent). Order
        matters: drop the worktree registration first (a checked-out branch can't
        be deleted), prune stale registrations, then delete the branch; finally
        sweep a leftover directory that was never a registered worktree."""
        reclaimed: list[str] = []

        # `--force` because the halted run's worktree legitimately holds its
        # uncommitted work — we are deliberately discarding it to re-run clean.
        removed = self._run(["worktree", "remove", "--force", str(path)])
        if removed.returncode == 0:
            reclaimed.append(f"worktree {path}")

        # Prune registrations whose directory is already gone (a partially
        # cleaned-up prior run), so the upcoming `add` sees a clean slate.
        self._run(["worktree", "prune"])

        # Delete the agent branch the halted run created.
        deleted = self._run(["branch", "-D", branch])
        if deleted.returncode == 0:
            reclaimed.append(f"branch {branch}")

        # Filesystem fallback: a leftover dir that git never registered (or
        # couldn't remove) still blocks `worktree add` with "already exists".
        if path.exists():
            try:
                shutil.rmtree(path)
                reclaimed.append(f"leftover dir {path}")
            except OSError as e:  # pragma: no cover - rare fs race
                _LOG.warning("could not remove leftover worktree dir %s: %s", path, e)

        if reclaimed:
            _LOG.info(
                "reclaimed stale agent run state before re-create: %s",
                ", ".join(reclaimed),
            )
