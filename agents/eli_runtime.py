"""
trident_eli_runtime.py — the production side-effecting helpers for Eli's land
seams (issue #78, parent #58).

What this is
------------
``EliPipeline`` (trident_eli_pipeline) is a PURE orchestrator driven by four
injected seams: ``merger`` / ``bundle_builder`` / ``full_suite_runner`` /
``main_pusher``. Eli's own module deliberately ships NONE of the real
implementations so its whole flow stays fake-driven in tests. This sibling is
where those real implementations live — the same split Gordon uses
(``trident_agent_pipeline`` ships ``run_pytest_in_worktree`` /
``push_branch_to_origin``; the manager wires them into ``IssuePrPipeline``).

The four helpers
----------------
  * ``merge_branch_onto_main`` — LOCAL ``git merge --ff-only <branch>`` of the
    approved branch onto ``main``. NEVER pushes. A non-fast-forwardable branch or
    any conflict returns ``MergeOutcome(merged=False, conflict=...)`` WITHOUT
    leaving a dirty tree (it aborts the merge first). Every git command is routed
    through ``eli_enforce_command`` (the secret-read guard stays on; push-main is
    permitted but this helper never pushes anyway).
  * ``rebuild_dashboard_bundle`` — the SINGLE ``npm run deploy:dashboard`` rebuild
    at integration, run in ``<workspace>/web``. Raises on a non-zero exit.
  * ``run_full_suite`` — the WHOLE ``python -m pytest`` suite (system 3.12, NOT
    the incomplete .venv) on the merged tree. Also runs the frontend typecheck
    (``tsc -b``, #183 / #210) when ``web/`` files changed. Parses pass/fail into a
    ``SuiteOutcome``.
  * ``run_frontend_check`` — runs ``tsc -b`` in ``<workspace>/web``. Skips when
    no ``web/`` files changed. The SHARED HELPER (#183) — callable by both Eli's
    land gate and the implementer grader (future #4).
  * ``push_main`` — ``git -C <workspace> push origin main``. Raises on a non-zero
    exit (the ONE push-main-allowed seam; only ever called on a green suite). On a
    non-fast-forward rejection (``main`` advanced during the suite) it recovers the
    push IFF only the wiki (``docs/claude/**``) raced in — integrate + retry once;
    any code/config/test racer leaves ``main`` untested, so it raises to halt.

merge rationale (prefer-ff, fall back to a clean auto-merge)
-----------------------------------------------------------
The by-hand land used ``git merge --ff-only`` — every agent PR branches off a
``main`` held still by the strict-serial loop (one issue in flight at a time,
#64), so in the common case the approved branch is a clean descendant of
``main`` and a fast-forward keeps history linear. We still TRY the fast-forward
first for exactly that reason.

But "main is held still" is not actually guaranteed: a hand commit straight to
``main`` (or another agent landing first) advances it after a branch was cut, so
the two diverge and the fast-forward is refused. The old code treated EVERY such
refusal as a human-needed conflict and halted — but a diverged-but-conflict-free
branch is not ambiguous, it just needs a merge commit. So on an ff refusal we now
fall back to ``git merge --no-ff``: a clean merge LANDS (with a merge commit),
and ONLY a real content conflict halts. The recurring "Not possible to
fast-forward" stall ([[footguns#197]]) is gone. Append-only wiki logs union-merge
via ``.gitattributes`` so two log appends never count as a conflict.

Why a separate module
---------------------
Same reason Gordon's runtime helpers live beside its pipeline: keeping the real
git/npm/pytest calls out of ``trident_eli_pipeline`` means Eli's orchestration
tests never shell out, and these helpers are independently testable with a real
git repo fixture if needed. The manager assembles them into the real
``EliPipeline`` in ``_build_real_eli_pipeline``.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from agents.eli_pipeline import MergeOutcome, SuiteOutcome, eli_enforce_command

_LOG = logging.getLogger(__name__)

# Files Eli may auto-resolve when a merge conflicts on them and ONLY them: the
# LLM wiki (``docs/claude/**``). These are additive-by-design notes (footguns,
# index, architecture, decisions, the append-only log) that two sessions routinely
# edit while the fleet runs, producing cosmetic "conflicts" with no semantic clash.
# A union merge keeps BOTH sides (worst case a duplicate the monthly lint sweeps
# up); crucially, docs can't change the test suite, so auto-resolving them cannot
# smuggle a broken landing past the green-gate. ANYTHING outside this prefix —
# code, config, tests — is a real human-needed conflict and still halts (#119/#113).
_AUTORESOLVE_PREFIX = "docs/claude/"

# Generous backstops — a land is rare and must not be killed mid-merge/mid-push.
MERGE_TIMEOUT_S = 120.0
BUNDLE_TIMEOUT_S = 600.0   # an npm build can be slow on a cold cache
SUITE_TIMEOUT_S = 1800.0   # the WHOLE suite on the merged tree
PUSH_TIMEOUT_S = 120.0
FRONTEND_CHECK_TIMEOUT_S = 120.0   # tsc -b (TypeScript typecheck only, no vite build)


def _run_git(workspace: str, args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess:
    """Run a ``git -C <workspace> ...`` command, routing the full command line
    through ``eli_enforce_command`` first (the secret-read guard stays on; the
    push-main axis is permitted for Eli). Returns the CompletedProcess (callers
    branch on returncode). Raises ForbiddenCommand if the guard blocks it."""
    cmd = ["git", "-C", workspace, *args]
    eli_enforce_command(" ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)


def _combine(cp: subprocess.CompletedProcess) -> str:
    """The full stdout+stderr of a completed command, trimmed. This is the raw
    terminal output Eli's stage feed surfaces so a finished land can be inspected
    after the fact (the actual ``git merge`` / ``npm`` / ``pytest`` / ``git push``
    output, not just the one-line summary)."""
    return ((cp.stdout or "") + (cp.stderr or "")).strip()


def _unmerged_files(workspace: str) -> list[str]:
    """Repo-root-relative paths git left in conflict (unmerged) after a failed
    merge — ``git diff --name-only --diff-filter=U``. Forward-slash paths."""
    cp = _run_git(
        workspace, ["diff", "--name-only", "--diff-filter=U"], timeout_s=MERGE_TIMEOUT_S
    )
    if cp.returncode != 0:
        return []
    return [ln.strip().replace("\\", "/") for ln in (cp.stdout or "").splitlines() if ln.strip()]


def _classify_conflicts(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split conflicted paths into (auto-resolvable wiki docs, everything else).

    Pure — the load-bearing decision Nathan signed off on: a conflict touching
    ONLY ``docs/claude/**`` is small-fry (Eli unions it); a conflict touching any
    other path (code/config/tests) is real and must halt for a human."""
    safe, unsafe = [], []
    for p in paths:
        (safe if p.replace("\\", "/").startswith(_AUTORESOLVE_PREFIX) else unsafe).append(p)
    return safe, unsafe


def _union_resolve_doc(workspace: str, rel_path: str) -> None:
    """Union-merge one conflicted wiki file in place: keep BOTH sides' lines on
    every clashing hunk (no markers), then stage it. Pulls the three merge stages
    (:1 base / :2 ours=main / :3 theirs=branch) out of the index and runs
    ``git merge-file --union``. A stage missing (e.g. file added on one side only)
    is treated as empty. Raises RuntimeError if the union can't be produced."""
    def _stage(num: int) -> str:
        cp = _run_git(workspace, ["show", f":{num}:{rel_path}"], timeout_s=MERGE_TIMEOUT_S)
        return cp.stdout if cp.returncode == 0 else ""

    base_txt, ours_txt, theirs_txt = _stage(1), _stage(2), _stage(3)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        base_f, ours_f, theirs_f = tdp / "base", tdp / "ours", tdp / "theirs"
        base_f.write_text(base_txt, encoding="utf-8")
        ours_f.write_text(ours_txt, encoding="utf-8")
        theirs_f.write_text(theirs_txt, encoding="utf-8")
        # git merge-file is repo-independent (operates on the 3 files directly); -p
        # prints the union to stdout instead of overwriting `ours`. The secret-read
        # guard still applies via eli_enforce_command.
        cmd = ["git", "merge-file", "-p", "--union", str(ours_f), str(base_f), str(theirs_f)]
        eli_enforce_command(" ".join(cmd))
        merged = subprocess.run(cmd, capture_output=True, text=True, timeout=MERGE_TIMEOUT_S)
        # --union always resolves, so a non-zero here is a real failure (bad path, etc.).
        if merged.returncode not in (0,):
            raise RuntimeError(
                f"git merge-file --union failed for {rel_path}: "
                f"{(merged.stderr or merged.stdout).strip()}"
            )
    (Path(workspace) / rel_path).write_text(merged.stdout, encoding="utf-8")
    add = _run_git(workspace, ["add", "--", rel_path], timeout_s=MERGE_TIMEOUT_S)
    if add.returncode != 0:
        raise RuntimeError(f"could not stage union-resolved {rel_path}: {(add.stderr or add.stdout).strip()}")


def _auto_resolve_doc_conflicts(workspace: str, safe_paths: list[str]) -> bool:
    """Union-resolve every wiki file in ``safe_paths`` and COMMIT the in-progress
    merge (``git commit --no-edit``), completing the land. Returns True on success;
    on any failure logs, aborts the merge, and returns False (caller then halts)."""
    try:
        for rel in safe_paths:
            _union_resolve_doc(workspace, rel)
        commit = _run_git(workspace, ["commit", "--no-edit"], timeout_s=MERGE_TIMEOUT_S)
        if commit.returncode != 0:
            raise RuntimeError(f"commit of union-merge failed: {(commit.stderr or commit.stdout).strip()}")
    except Exception as e:
        _LOG.warning("auto-resolve of wiki conflicts failed, abandoning to halt: %s", e)
        _run_git(workspace, ["merge", "--abort"], timeout_s=MERGE_TIMEOUT_S)
        return False
    _LOG.info("auto-union-merged wiki conflicts and committed the merge: %s", ", ".join(safe_paths))
    return True


def merge_branch_onto_main(workspace: str, *, branch: str) -> MergeOutcome:
    """LOCAL merge of ``branch`` onto ``main`` — fast-forward when possible, a
    clean auto-merge when ``main`` has moved underneath. NEVER pushes.

    The merge ALWAYS starts from a clean slate (#166): it fetches ``origin/main``,
    resets ``main`` to it (discarding any stale merge from a prior halted attempt),
    fetches the LATEST ``origin/<branch>`` head, and merges from that REMOTE
    tracking ref. This guarantees the merge uses the current branch tip — even when
    the operator pushed fixes to the branch after a halt. The concrete flow:

      1. Checkout ``main``, fetch ``origin/main``, reset ``main`` to it.
      2. Fetch ``origin/<branch>`` so the merge sees the CURRENT remote head, not a
         stale local copy cached across a halt→resume boundary (#166).
      3. Tries ``git merge --ff-only origin/<branch>``. Succeeds whenever the
         branch is still a clean descendant of ``main`` (the serial-loop common
         case) and keeps ``main``'s history linear.
      4. If the fast-forward is refused (``main`` advanced after the branch was
         cut — a hand commit, or another agent landing — so the two diverged),
         falls back to ``git merge --no-ff --no-edit origin/<branch>``. A *clean*
         merge (no real conflict) returns ``MergeOutcome(merged=True)`` with a
         merge commit on ``main``. This is the fix for the recurring "Not possible
         to fast-forward" halt ([[footguns#197]]): a diverged-but-conflict-free
         branch is NOT an ambiguous state a human must resolve, so we land it.
      5. A no-ff conflict is TRIAGED, not blindly halted (#113/#119). If every
         conflicted file is under ``docs/claude/`` (the LLM wiki — additive notes,
         indexes, the log), Eli union-merges them (keeps both sides), commits the
         merge, and lands — returning ``MergeOutcome(merged=True,
         auto_resolved_docs=...)``. If ANY conflicted file is code/config/test, it
         aborts the merge (tree left clean) and returns ``merged=False`` with the
         offending files named — a real human-needed clash, so Eli halts. Wiki
         docs can't change the test suite, so the auto-resolve can never push a
         broken land past the green-gate that runs next.

    Append-only wiki logs (``docs/claude/log.md``) also carry a ``merge=union``
    attribute (see ``.gitattributes``), so two sessions each appending an entry
    union-merge automatically in step 4 without ever reaching the step-5 triage —
    that exact clash was the only thing that stalled the #97 land. The step-5
    triage generalises that fix to the rest of the wiki (footguns, index,
    architecture) which the narrow ``log.md`` attribute didn't cover (#119).

    The push is a SEPARATE seam (``push_main``); this helper only ever merges
    locally.
    """
    # Make sure we're on main before merging onto it.
    co = _run_git(workspace, ["checkout", "main"], timeout_s=MERGE_TIMEOUT_S)
    if co.returncode != 0:
        return MergeOutcome(
            merged=False,
            conflict=f"could not checkout main: {(co.stderr or co.stdout).strip()}",
        )

    # ← #166: fetch origin/main and reset to it so any stale merge from a prior
    #   halted attempt is discarded. The land ALWAYS starts from a clean slate —
    #   the merge from the first (halted) attempt must not survive into the resume.
    fm = _run_git(
        workspace, ["fetch", "origin", "main"], timeout_s=MERGE_TIMEOUT_S
    )
    if fm.returncode != 0:
        return MergeOutcome(
            merged=False,
            conflict=f"could not fetch origin/main: {(fm.stderr or fm.stdout).strip()}",
        )
    reset = _run_git(
        workspace, ["reset", "--hard", "origin/main"], timeout_s=MERGE_TIMEOUT_S
    )
    if reset.returncode != 0:
        return MergeOutcome(
            merged=False,
            conflict=f"could not reset main to origin/main: {(reset.stderr or reset.stdout).strip()}",
        )

    # ← #166: fetch the branch from origin so the merge sees the CURRENT remote
    #   head — not a stale local copy that was cached across a halt→resume
    #   boundary. Fail closed: the branch head is unknowable without the fetch.
    fb = _run_git(
        workspace, ["fetch", "origin", branch], timeout_s=MERGE_TIMEOUT_S
    )
    if fb.returncode != 0:
        return MergeOutcome(
            merged=False,
            conflict=f"could not fetch origin/{branch}: {(fb.stderr or fb.stdout).strip()}",
        )
    remote_ref = f"origin/{branch}"

    # 3) Fast-forward when the branch is still a clean descendant (linear history).
    ff = _run_git(
        workspace, ["merge", "--ff-only", remote_ref], timeout_s=MERGE_TIMEOUT_S
    )
    if ff.returncode == 0:
        _LOG.info("merged %s onto main (fast-forward)", branch)
        return MergeOutcome(merged=True, output=_combine(ff))

    # 4) ff refused ⇒ main diverged. Try a real (no-ff) merge. A clean merge lands
    #    with a merge commit; only a true content conflict falls through to halt.
    _LOG.info(
        "main moved under %s (no fast-forward) — attempting a clean no-ff merge",
        branch,
    )
    merge = _run_git(
        workspace,
        ["merge", "--no-ff", "--no-edit", remote_ref],
        timeout_s=MERGE_TIMEOUT_S,
    )
    if merge.returncode == 0:
        _LOG.info("merged %s onto main (merge commit; main had diverged)", branch)
        return MergeOutcome(merged=True, output=_combine(merge))

    # 5) The no-ff merge conflicted. TRIAGE before halting (#113/#119): a conflict
    #    that touches ONLY the LLM wiki (docs/claude/**) is additive small-fry — two
    #    sessions edited the same notes/index while the fleet ran — so Eli unions it
    #    (keeps both sides), commits the merge, and lands. A conflict touching ANY
    #    code/config/test file is a real human-needed clash and still halts. Docs
    #    can't affect the suite, so the auto-resolve can't sneak a broken land past
    #    the green-gate that runs next.
    stderr = (merge.stderr or merge.stdout).strip()
    conflicted = _unmerged_files(workspace)
    safe, unsafe = _classify_conflicts(conflicted)
    if conflicted and not unsafe:
        _LOG.info(
            "merge of %s conflicts only on wiki docs %s — attempting union auto-resolve",
            branch, ", ".join(safe),
        )
        if _auto_resolve_doc_conflicts(workspace, safe):
            _LOG.info("merged %s onto main (wiki conflicts auto-union-merged)", branch)
            auto_note = (
                f"{_combine(merge)}\n"
                f"[auto-union-merged wiki conflicts: {', '.join(safe)}]"
            ).strip()
            return MergeOutcome(
                merged=True, auto_resolved_docs=tuple(safe), output=auto_note,
            )
        # auto-resolve already aborted the merge; fall through to the halt below.
        return MergeOutcome(
            merged=False,
            conflict=f"wiki conflict auto-resolve failed ({', '.join(safe)}); needs a human",
        )

    # A real conflict — abort so the tree is never left dirty, and name the
    # offending non-doc files so the halt report points a human at the right place.
    _run_git(workspace, ["merge", "--abort"], timeout_s=MERGE_TIMEOUT_S)
    if unsafe:
        detail = "real conflict in code/config/test file(s): " + ", ".join(unsafe)
    else:
        detail = stderr or "merge conflict onto main"
    _LOG.warning(
        "merge of %s onto main hit a real conflict (left main untouched): %s",
        branch, detail,
    )
    return MergeOutcome(merged=False, conflict=detail)


def rebuild_dashboard_bundle(workspace: str) -> str:
    """Run the SINGLE ``npm run deploy:dashboard`` rebuild at integration, in
    ``<workspace>/web``. Returns the full build output (stdout+stderr) so the land
    feed can show what the build actually did; raises RuntimeError on a non-zero
    exit.

    This is the one bundle rebuild for the whole land (every agent PR is
    source-only — ``dashboard_v2/`` is guarded out by #51 — so the compiled bundle
    is regenerated exactly once here, on the merged tree)."""
    web_dir = Path(workspace) / "web"
    # shell=True with a STRING command (2026-06-23): on Windows ``npm`` is really
    # ``npm.cmd``, and a ``shell=False`` exec of the bare name "npm" can't resolve a
    # .cmd shim → ``[WinError 2] The system cannot find the file specified`` (which
    # halted Eli's first real land). Going through the shell lets cmd.exe apply
    # PATHEXT and find npm.cmd, exactly as the interactive ``npm run deploy:dashboard``
    # does. The command is a fixed literal (no interpolated input), so there is no
    # injection surface. Works on POSIX too (runs via /bin/sh).
    proc = subprocess.run(
        "npm run deploy:dashboard",
        cwd=str(web_dir), capture_output=True, text=True,
        timeout=BUNDLE_TIMEOUT_S, shell=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"npm run deploy:dashboard failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-2000:]}"
        )
    return _combine(proc)


def _web_files_changed(workspace: str) -> bool:
    """True if any file under ``web/`` changed vs ``origin/main`` on the merged tree.

    Uses the three-dot diff (merge-base) so only the PR's own changes are counted —
    an advancing ``origin/main`` during the land never pulls in unrelated files.
    Returns True on ANY error (fail-open: a failed git diff means we can't know
    what changed, so we run tsc — a needless tsc on an unchanged tree is a fast
    no-op, but skipping a needed check is dangerous (#183))."""
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return True   # fail-open: git error ⇒ run tsc to be safe
        for line in (proc.stdout or "").splitlines():
            stripped = line.strip().replace("\\", "/")
            if stripped.startswith("web/") and not stripped.startswith("web/dist/"):
                return True
        return False
    except Exception:
        return True   # fail-open: any error ⇒ run tsc


def run_frontend_check(workspace: str) -> tuple[bool, str, str, bool]:
    """Run ``npx tsc -b`` in ``<workspace>/web`` as a TypeScript typecheck gate.

    Skips when no files under ``web/`` changed (fast path — no pointless tsc run on
    a pure-backend change). Returns ``(passed, summary, output, checked)``:

    * ``passed`` — True when the check is skipped (no web changes) OR tsc exits 0.
    * ``summary`` — a one-liner for the land feed (e.g. "tsc typecheck passed").
    * ``output`` — the full tsc stdout+stderr so a red gate shows the exact errors.
    * ``checked`` — True when tsc actually ran (False = skipped, no web changes).

    This is the SHARED HELPER (#183) — both Eli's land gate (via ``run_full_suite``)
    and the implementer grader (future #4) call it. ``tsc -b`` IS the de-facto
    frontend test (#210); no vitest/jest runner is wired.

    Uses ``shell=True`` (same as ``rebuild_dashboard_bundle``) so Windows resolves
    ``npx.cmd`` via PATHEXT. The command is a fixed literal — no injection surface.
    """
    if not _web_files_changed(workspace):
        return True, "no web/ files changed — frontend check skipped", "", False

    web_dir = Path(workspace) / "web"
    try:
        proc = subprocess.run(
            "npx tsc -b",
            cwd=str(web_dir), capture_output=True, text=True,
            timeout=FRONTEND_CHECK_TIMEOUT_S, shell=True,
        )
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()
        return (
            False,
            f"frontend typecheck timed out after {FRONTEND_CHECK_TIMEOUT_S:.0f}s",
            out,
            True,
        )
    output = _combine(proc)
    passed = proc.returncode == 0
    if passed:
        summary = "tsc typecheck passed"
    else:
        # Pull a short summary: "Found N errors." or the last error line.
        summary = "tsc typecheck FAILED"
        for line in reversed((proc.stdout or "").splitlines()):
            lower = line.strip().lower()
            if "error" in lower and not lower.startswith("npm"):
                summary = line.strip()
                break
    return passed, summary, output, True


# ── pre-existing-red check (Option A, 2026-06-27) ──────────────────────────────
# When the merged-tree suite is red, Eli runs the SAME failing tests against
# origin/main before halting.  If the failure signature is identical the red was
# already on main → the PR didn't introduce it → Eli warns and proceeds (the
# gate is green w.r.t. the PR's own changes).  A different or new failure still
# halts — the check is conservative so a genuine new red can never slip through.


def _parse_failing_tests(output: str) -> list[str]:
    """Extract the pytest node IDs of every FAILED test from ``-q`` output.

    Matches lines of the form::

        FAILED tests/path/test_file.py::TestClass::test_name
        FAILED tests/path/test_file.py::TestClass::test_name - AssertionError: ...

    Returns an empty list when the output carries no failures (shouldn't happen
    — the caller only calls this when the suite is already red)."""
    import re as _re
    _FAILED_RE = _re.compile(
        r"^FAILED\s+(\S+?)(?:\s+-\s+.+)?$", _re.MULTILINE
    )
    ids: list[str] = []
    for m in _FAILED_RE.finditer(output):
        tid = m.group(1)
        if tid not in ids:
            ids.append(tid)
    return ids


def _check_pre_existing(workspace: str, test_ids: list[str]) -> tuple[bool, str]:
    """Run *test_ids* against ``origin/main`` (via a temp detached worktree) and
    return ``(is_pre_existing, main_output)``.

    *is_pre_existing* is True when every *test_id* also FAILED on main with the
    same test ID — i.e. the red was already there before the PR landed.  The temp
    worktree is removed even on error (best-effort — a leak is harmless disk
    clutter).  Any error in the check itself (git failure, worktree timeout) is
    treated as NOT pre-existing (fail-closed: when we can't prove the red was
    already there, halt and let a human decide).
    """
    import os
    tmp = tempfile.mkdtemp(prefix="eli_check_")
    try:
        # Create a detached worktree at origin/main so the merged workspace is
        # untouched.  origin/main was already fetched by merge_branch_onto_main
        # before the suite ran, so this is a local no-network op.
        add = subprocess.run(
            ["git", "-C", workspace, "worktree", "add", "--detach", tmp,
             "origin/main"],
            capture_output=True, text=True, timeout=30,
        )
        if add.returncode != 0:
            _LOG.warning(
                "pre-existing check: could not create worktree at origin/main: %s",
                (add.stderr or add.stdout).strip(),
            )
            return False, ""

        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "-q", "--no-header", "--tb=short",
                 *test_ids],
                cwd=tmp, capture_output=True, text=True, timeout=120,
            )
            main_output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            _LOG.warning("pre-existing check: pytest on origin/main timed out")
            return False, ""
    except Exception as e:
        _LOG.warning("pre-existing check: error running tests on origin/main: %s", e)
        return False, ""
    finally:
        # Best-effort cleanup — a leaked tmp dir is harmless disk clutter.
        try:
            subprocess.run(
                ["git", "-C", workspace, "worktree", "remove", "--force", tmp],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass

    # Compare: every failing test on the merged tree must also fail on main.
    main_failing = set(_parse_failing_tests(main_output))
    merged_failing = set(test_ids)
    is_pre_existing = merged_failing <= main_failing  # subset → all were already red
    if not is_pre_existing:
        new_failures = merged_failing - main_failing
        _LOG.info(
            "pre-existing check: %d failure(s) are NEW (not on origin/main): %s",
            len(new_failures), ", ".join(sorted(new_failures)),
        )
    return is_pre_existing, main_output


def run_full_suite(workspace: str) -> SuiteOutcome:
    """Run the WHOLE ``python -m pytest`` suite on the merged tree (system Python
    3.12, NOT the incomplete .venv) AND the frontend typecheck gate (``tsc -b``,
    #183 / #210). Returns a ``SuiteOutcome`` — ``passed`` is True only when BOTH
    gates are green.

    Distinct from ``trident_agent_pipeline.run_pytest_in_worktree``, which is
    SCOPED to a run's changed files — Eli runs EVERYTHING because a merge can break
    a module no single PR touched. A non-zero exit (or a timeout) is a RED suite.

    **Pre-existing red check (Option A, 2026-06-27):** when pytest fails, Eli
    runs the SAME failing tests against ``origin/main`` (via a temp detached
    worktree).  If every failure is byte-identical to the failure on main, the
    red was already there — the PR didn't introduce it — so Eli warns and
    proceeds (the gate is green w.r.t. the PR's own changes).  A new or different
    failure still halts; any error in the check itself is treated as NOT
    pre-existing (fail-closed).

    The frontend check runs AFTER pytest (pytest is the heavier gate; the fast
    typecheck runs last so a pytest failure surfaces first). When no ``web/`` files
    changed the frontend check is skipped (fast path). The pipeline emits a named
    ``tsc`` stage from the frontend fields so the land feed shows its own pass/fail."""
    # 1) pytest (existing gate).
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", "-q"],
            cwd=workspace, capture_output=True, text=True, timeout=SUITE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()
        return SuiteOutcome(
            passed=False,
            summary=f"full suite timed out after {SUITE_TIMEOUT_S:.0f}s",
            output=out,
        )
    pytest_output = (proc.stdout or "") + (proc.stderr or "")
    pytest_passed = proc.returncode == 0
    pytest_summary = ""
    for line in reversed(pytest_output.splitlines()):
        if line.strip():
            pytest_summary = line.strip()
            break

    # 1b) Pre-existing-red check (Option A): when pytest is red, run the failing
    #     tests against origin/main.  If they ALL fail identically there, the red
    #     was already on main → warn and treat as green (the PR didn't introduce it).
    pre_existing_note = ""
    if not pytest_passed:
        failing = _parse_failing_tests(pytest_output)
        if failing:
            is_pre, main_out = _check_pre_existing(workspace, failing)
            if is_pre:
                _LOG.warning(
                    "pre-existing red on main: %d test(s) already failing on "
                    "origin/main — the PR did not introduce them; proceeding",
                    len(failing),
                )
                pre_existing_note = (
                    f" (pre-existing red on main: {len(failing)} test(s) — "
                    f"the PR did not introduce them)"
                )
                pytest_passed = True
                pytest_summary += pre_existing_note

    # 2) Frontend typecheck (#183 / #210). Runs even when pytest failed so the
    #    land feed shows both gates.
    fc_passed, fc_summary, fc_output, fc_checked = run_frontend_check(workspace)

    # 3) Combine — passed only when BOTH are green.
    overall_passed = pytest_passed and fc_passed
    if fc_checked:
        combined_summary = (
            f"pytest: {pytest_summary or ('passed' if pytest_passed else 'failed')}"
            f" | frontend: {fc_summary}"
        )
    else:
        combined_summary = pytest_summary or ("passed" if pytest_passed else "failed")

    # The combined output labels each section so the land feed shows both as named
    # sub-sections even when the pipeline emits a single "test" stage.
    combined_output = (
        f"=== PYTEST ===\n{pytest_output.strip()}\n\n"
        f"=== FRONTEND CHECK (tsc -b) ===\n{fc_output or '(skipped — no web/ files changed)'}"
    ).strip()

    return SuiteOutcome(
        passed=overall_passed,
        summary=combined_summary,
        output=combined_output.strip(),
        frontend_checked=fc_checked,
        frontend_passed=fc_passed,
        frontend_summary=fc_summary,
        frontend_output=fc_output,
    )


def _is_non_fast_forward(text: str) -> bool:
    """True when a failed ``git push`` was rejected because ``main`` advanced
    underneath us (a non-fast-forward), as opposed to auth/network/etc. Git phrases
    this as ``! [rejected]`` + ``(fetch first)`` / ``non-fast-forward``."""
    t = (text or "").lower()
    return ("fetch first" in t) or ("non-fast-forward" in t) or ("[rejected]" in t)


def _upstream_only_changed_files(workspace: str) -> list[str]:
    """Repo-root-relative paths the racing upstream commits changed but the local
    land did not — ``git diff --name-only HEAD...origin/main``. The three-dot form
    diffs ``origin/main`` against the merge-base, so it names exactly what landed on
    ``origin/main`` after this branch was cut (the commits that beat us to the push).
    Forward-slash paths. Requires a prior ``git fetch`` so ``origin/main`` is current."""
    cp = _run_git(
        workspace, ["diff", "--name-only", "HEAD...origin/main"], timeout_s=PUSH_TIMEOUT_S
    )
    if cp.returncode != 0:
        return []
    return [ln.strip().replace("\\", "/") for ln in (cp.stdout or "").splitlines() if ln.strip()]


def push_main(workspace: str) -> str:
    """``git -C <workspace> push origin main`` — the ONE push-main-allowed seam.
    Returns the full push output (stdout+stderr) for the land feed; raises
    RuntimeError on a non-zero exit. Only ever called by Eli on a green full suite
    (the orchestrator gates this).

    Push-race recovery (2026-06-26, [[footguns]]): a green land can still LOSE the
    push to a commit that hit ``origin/main`` during the ~6-min full suite — the
    daily wiki-lint cron is the usual culprit (it pushes ``docs/claude/**`` to main
    while Eli is testing). The old code halted every such race for a human even
    though it is mechanical. Now, on a non-fast-forward rejection we fetch and
    inspect WHAT raced in:

      * Only the LLM wiki (``docs/claude/**``) raced in ⇒ those commits cannot
        affect the suite that just passed, so we integrate them (``merge_branch_onto_main``
        union-resolves any additive wiki clash, exactly like the merge seam) and
        retry the push ONCE.
      * ANYTHING outside ``docs/claude/**`` raced in ⇒ the combined tree is
        UNTESTED. Pushing it would bypass the green-gate, so we raise and let Eli
        halt for a human — same as before, with a precise reason. (Same safety line
        as the merge-conflict triage's ``_AUTORESOLVE_PREFIX``.)

    Only ONE retry: if the re-push still loses (another racer), we raise rather
    than loop against a busy ``main``."""
    proc = _run_git(workspace, ["push", "origin", "main"], timeout_s=PUSH_TIMEOUT_S)
    if proc.returncode == 0:
        return _combine(proc)

    rejection = (proc.stderr or "") + (proc.stdout or "")
    if not _is_non_fast_forward(rejection):
        # Not a race (auth, network, protected-branch, …) — behave exactly as before.
        raise RuntimeError(
            f"git push origin main failed (exit {proc.returncode}): {rejection.strip()}"
        )

    # Non-fast-forward: main advanced under the land. Fetch and see if it's safe.
    _LOG.info("push to main rejected (non-fast-forward) — main advanced during the land; checking what raced in")
    fetch = _run_git(workspace, ["fetch", "origin", "main"], timeout_s=PUSH_TIMEOUT_S)
    if fetch.returncode != 0:
        raise RuntimeError(
            f"git push origin main failed (non-fast-forward) and the recovery fetch "
            f"also failed: {(fetch.stderr or fetch.stdout).strip()}"
        )

    raced_in = _upstream_only_changed_files(workspace)
    safe, unsafe = _classify_conflicts(raced_in)
    if unsafe:
        # Code/config/test raced onto main ⇒ the merged tree was never tested.
        raise RuntimeError(
            "git push origin main rejected (main advanced under the land) and the "
            "racing commit(s) touch code/config/test, so the combined tree is "
            "UNTESTED and was NOT pushed — needs a human: " + ", ".join(unsafe)
        )

    # Only wiki docs raced in — they can't change the suite that just passed.
    # Integrate them (union-resolving additive clashes), then retry the push once.
    _LOG.info("only wiki docs raced onto main (%s) — integrating and re-pushing", ", ".join(safe))
    merge = merge_branch_onto_main(workspace, branch="origin/main")
    if not merge.merged:
        raise RuntimeError(
            "git push origin main rejected; auto-integrating the racing wiki "
            f"commit(s) failed: {merge.conflict}"
        )

    retry = _run_git(workspace, ["push", "origin", "main"], timeout_s=PUSH_TIMEOUT_S)
    if retry.returncode != 0:
        raise RuntimeError(
            "git push origin main failed again after integrating the racing wiki "
            f"commit(s) (exit {retry.returncode}): {(retry.stderr or retry.stdout).strip()}"
        )
    _LOG.info("pushed main after integrating racing wiki commit(s): %s", ", ".join(safe))
    return (
        f"{(merge.output or '').strip()}\n{_combine(retry)}\n"
        f"[push-race recovered: integrated wiki commit(s) {', '.join(safe)} and re-pushed]"
    ).strip()
