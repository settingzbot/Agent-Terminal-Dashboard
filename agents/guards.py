"""
trident_agent_guards.py — the PURE shell-command guard predicate (issue #34).

Why this module exists
----------------------
Agent runs (issue #24 / #30) execute unattended at 3am with LOOSENED
tool-permissions so they don't hang on approval prompts. Containment is supposed
to come from worktree isolation (#35) — but two failure modes leak *through* an
isolated worktree, so they need an explicit guard:

    (a) **Push / merge to ``main``.** A run's output must be a Pull Request,
        never a direct write to the protected branch. Worktree isolation doesn't
        stop ``git push origin main`` — the remote is shared.
    (b) **Reading the OS keyring / secrets.** The live Lighter API private key
        lives in the OS keyring (see trident_secrets). A run that prints it into
        its transcript has leaked a key that signs real trades. Worktree
        isolation doesn't stop ``keyring get ...`` either.

This module is the deep, exhaustively-tested CORE: a pure function that, given a
shell-command string, returns a verdict. It has NO side effects, imports nothing
from the run engine, and never touches the keyring — so it is trivially testable
with hundreds of positive/negative cases (tests/test_agent_guards.py).

The enforcement *seam* (a launch-command wrapper + a git pre-push hook installer)
lives in trident_agent_run.py and calls this predicate. Keeping the policy here
and the plumbing there means #35's worktree step can reuse the exact same policy.

ENFORCEMENT SCOPE — what this predicate actually sees (#121, L8)
---------------------------------------------------------------
The two axes are NOT enforced symmetrically, and the secret-read axis is the
weaker of the two:

  * **Push / merge to main** has TWO enforcement points: the launch-command check
    AND the git pre-push hook. The hook fires on every push the agent issues
    *inside* the run, against the REAL resolved refspec — so even an obfuscated
    in-session ``git push`` to main is caught at push time. This axis is defended
    in-session.

  * **Secret read** has ONLY the launch-command check. ``check_command`` /
    ``_check_secret_read`` inspect the single command a run is LAUNCHED with — they
    do NOT see the commands the agent types *inside* the loosened-permission
    session. There is no keyring/secret equivalent of the pre-push hook, so a
    ``keyring get`` or ``get_lighter_key()`` the agent issues mid-session is NOT
    blocked by anything here. The launch command of a coding run is just
    ``claude ...``, which carries no secret read, so in practice this axis catches
    almost nothing at runtime — it is a guard on the *launch string*, not on the
    session's behaviour. Do NOT treat a clean verdict as proof a run never read a
    secret. (A real in-session secret-scan — e.g. a transcript scanner — is
    explicitly OUT of #121; promote it to a feature if that containment is wanted.)

Design notes
------------
* We err toward FALSE POSITIVES being expensive (a benign command wrongly blocked
  stalls an unattended run) and FALSE NEGATIVES being catastrophic (a leaked key
  or a clobbered ``main``). So the patterns are tuned to catch representative
  *forms* exhaustively while explicitly allowing the common benign shapes
  (feature-branch pushes, ``gh pr create``, branch names that merely contain the
  substring "main", reading non-secret env vars, etc.).
* Compound commands (``a && b``, ``a; b``, ``a || b``, pipelines) are split and
  each segment is checked independently — a forbidden segment anywhere blocks the
  whole command.
* The predicate is regex/string based, not a real shell parser. That is a
  deliberate tradeoff: it can't be defeated by a clever obfuscation a determined
  *adversary* writes, but the threat model here is an LLM run going off-script,
  not a hostile human. The git pre-push hook is the backstop for the push axis
  (it sees the actual resolved refspec, not the typed string), so even an
  obfuscated ``git push`` to main is caught at push time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── verdict categories ──────────────────────────────────────────────────────

CATEGORY_PUSH_MAIN = "push_or_merge_to_main"
CATEGORY_SECRET_READ = "keyring_or_secret_read"
CATEGORY_BUILT_ARTIFACT = "built_artifact_in_diff"

# Protected branch names a run may never push to / merge into.
PROTECTED_BRANCHES = ("main", "master")

# Built-bundle directories an agent run must never commit / push (issue #51).
# ``dashboard_v2/`` is the compiled output of ``web/`` (``npm run deploy:dashboard``).
# Every PR that rebuilt it produced its own ``dashboard-<hash>.js``, and the
# pairwise bundle conflicts made the #18–#21 batch unmergeable. Agent PRs are
# SOURCE-ONLY: the bundle is rebuilt once at human-merge / integration time. The
# directory stays TRACKED in the repo (the VPS and PC get the built bundle by
# ``git pull``, not by running the build) — the fix is "agents don't touch it",
# enforced at the pre-push hook, NOT a global ``.gitignore``. Paths are matched
# against these prefixes (posix, trailing slash) after backslash normalisation.
FORBIDDEN_ARTIFACT_PREFIXES = ("dashboard_v2/",)


@dataclass(frozen=True)
class Verdict:
    """Result of guarding one command.

    ``blocked`` is the only thing callers must branch on. ``reason`` is a short,
    human-readable explanation (surfaced to the run transcript / dashboard).
    ``category`` is one of the ``CATEGORY_*`` constants when blocked, else None.
    """

    blocked: bool
    reason: str = ""
    category: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        if not self.blocked:
            return "Verdict(allow)"
        return f"Verdict(BLOCK {self.category}: {self.reason})"


_ALLOW = Verdict(blocked=False)


# ── command splitting ───────────────────────────────────────────────────────

# Split a compound command into its segments at shell operators so a forbidden
# segment anywhere trips the guard. We treat &&, ||, |, ;, and newlines as
# separators. This is intentionally liberal — over-splitting can only create
# MORE segments to inspect, never hide one.
_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||\||;|\n)\s*")


def _segments(command: str) -> list[str]:
    return [seg for seg in _SPLIT_RE.split(command) if seg.strip()]


# ── push / merge to main detection ──────────────────────────────────────────

# A protected branch as a bare token or as the target side of a refspec
# (src:dst, +src:dst, HEAD:refs/heads/main, etc.). We look for the protected
# name appearing as a *whole branch token*, not as a substring of another name
# (so "main-menu-fix", "domain", "remains" do NOT match).
#
#   token boundary:  start | whitespace | ':' | '+' | 'refs/heads/'
#   then the name
#   then a boundary:  end | whitespace
# We build this for each protected name.
def _build_branch_target_re(names: tuple[str, ...]) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in names)
    # The protected name must be preceded by one of: start, ws, ':', '+', or be
    # the tail of a refs/heads/ path; and followed by ws or end-of-string.
    return re.compile(
        rf"(?:^|[\s:+]|refs/heads/)(?:{alt})(?:\s|$)"
    )


_BRANCH_TARGET_RE = _build_branch_target_re(PROTECTED_BRANCHES)

_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")
_GIT_MERGE_RE = re.compile(r"\bgit\s+merge\b")
_GH_PR_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b")
_FORCE_FLAG_RE = re.compile(r"(?:^|\s)(?:-f|--force(?:-with-lease)?)\b")


def _has_explicit_branch_arg(push_cmd: str) -> bool:
    """True if the push command names ANY explicit branch/refspec target after
    'git push' (i.e. there is a non-flag positional beyond the remote). Used to
    decide whether a bare ``git push -f`` (no ref) is ambiguous → block."""
    # tokens after 'git push'
    after = push_cmd.split("push", 1)[1] if "push" in push_cmd else ""
    toks = [t for t in after.split() if t and not t.startswith("-")]
    # toks[0] would be the remote; a 2nd positional is the refspec/branch.
    return len(toks) >= 2


def _check_push_main(segment: str) -> Verdict | None:
    seg = segment.strip()

    # gh pr merge — merging a PR lands it on the base (main) branch.
    if _GH_PR_MERGE_RE.search(seg):
        return Verdict(
            blocked=True,
            reason="`gh pr merge` lands the PR on the protected branch; a run's "
            "output must stay a PR for a human to merge.",
            category=CATEGORY_PUSH_MAIN,
        )

    # git merge ... main/master  — merging the protected branch INTO the current
    # branch, OR (with a branch arg) bringing protected history in. We only block
    # when a protected name appears as a target token AND it's not a read-only
    # 'git rebase main' (rebase doesn't move main). git merge of a protected
    # branch name is blocked conservatively.
    if _GIT_MERGE_RE.search(seg) and _BRANCH_TARGET_RE.search(seg):
        return Verdict(
            blocked=True,
            reason="`git merge` touching the protected branch is not allowed in "
            "an unattended run.",
            category=CATEGORY_PUSH_MAIN,
        )

    # git push ...
    if _GIT_PUSH_RE.search(seg):
        targets_main = bool(_BRANCH_TARGET_RE.search(seg))
        forced = bool(_FORCE_FLAG_RE.search(seg))
        if targets_main:
            return Verdict(
                blocked=True,
                reason="push targets the protected branch (main/master); a run "
                "must open a PR from a feature branch instead.",
                category=CATEGORY_PUSH_MAIN,
            )
        # A forced push with NO explicit refspec pushes the current branch — which
        # could be main — with force. Too dangerous to allow blind in an
        # unattended run; block the ambiguous bare-force form.
        if forced and not _has_explicit_branch_arg(seg):
            return Verdict(
                blocked=True,
                reason="forced `git push` without an explicit feature-branch "
                "refspec could clobber the protected branch; name a feature "
                "branch explicitly.",
                category=CATEGORY_PUSH_MAIN,
            )

    return None


# ── keyring / secret read detection ─────────────────────────────────────────

# The trident_secrets public getters that return a real credential VALUE.
# (status/key_status/_status return only last-4 metadata, so they are NOT
# secret reads and must stay allowed.)
_SECRET_GETTERS = (
    "get_lighter_key",
    "get_x_credentials",
    "get_bluesky_credentials",
    "get_deepseek_key",
    "get_watchdog_gmail",
    "get_waitlist_stats_key",
)
_SECRET_GETTER_RE = re.compile(r"\b(?:" + "|".join(_SECRET_GETTERS) + r")\b")

# `keyring get ...` CLI (with or without a `python -m` prefix).
_KEYRING_GET_RE = re.compile(r"\bkeyring\s+get\b")
_KEYRING_MODULE_GET_RE = re.compile(r"\b(?:python\d?|py)\b.*\bkeyring\b.*\bget\b")

# keyring.get_password(...) in a python one-liner.
_KEYRING_GET_PASSWORD_RE = re.compile(r"\bget_password\b")

# The env var that holds the live private key, in any expansion form:
#   $LIGHTER_PRIVATE_KEY  ${LIGHTER_PRIVATE_KEY}  %LIGHTER_PRIVATE_KEY%
#   $env:LIGHTER_PRIVATE_KEY  Env:LIGHTER_PRIVATE_KEY  'LIGHTER_PRIVATE_KEY'
# We block any command that references the name (the only legitimate consumer is
# the bot itself via trident_secrets, never an agent run).
_LIGHTER_ENV_RE = re.compile(r"LIGHTER_PRIVATE_KEY")


def _check_secret_read(segment: str) -> Verdict | None:
    # LAUNCH-COMMAND ONLY (#121, L8): this sees the string a run is launched with,
    # never the commands the agent issues inside the session. Unlike the push axis
    # (backed by the in-session pre-push hook), there is no in-session secret-read
    # backstop — a mid-session `keyring get` is not caught here.
    seg = segment

    if _SECRET_GETTER_RE.search(seg):
        return Verdict(
            blocked=True,
            reason="calls a trident_secrets credential getter — a run must never "
            "read a live secret into its transcript.",
            category=CATEGORY_SECRET_READ,
        )

    if _KEYRING_GET_RE.search(seg) or _KEYRING_MODULE_GET_RE.search(seg):
        return Verdict(
            blocked=True,
            reason="`keyring get` reads a stored credential value.",
            category=CATEGORY_SECRET_READ,
        )

    if _KEYRING_GET_PASSWORD_RE.search(seg):
        return Verdict(
            blocked=True,
            reason="`keyring.get_password(...)` reads a stored credential value.",
            category=CATEGORY_SECRET_READ,
        )

    if _LIGHTER_ENV_RE.search(seg):
        return Verdict(
            blocked=True,
            reason="references LIGHTER_PRIVATE_KEY — the live key must never be "
            "read into a run transcript.",
            category=CATEGORY_SECRET_READ,
        )

    return None


# ── public predicate ────────────────────────────────────────────────────────

def check_command(command: object) -> Verdict:
    """Guard one shell command. PURE — no side effects, never raises.

    Returns a :class:`Verdict`. ``blocked`` is True for any command that pushes /
    merges to a protected branch or reads the keyring / a secret; False
    otherwise. Non-string / empty input is treated as not-blocked (defensive: the
    caller passes strings, but the predicate must never crash on bad input).

    Compound commands are split at shell operators and each segment is checked;
    a forbidden segment anywhere blocks the whole command.
    """
    if not isinstance(command, str) or not command.strip():
        return _ALLOW

    for segment in _segments(command):
        verdict = _check_push_main(segment)
        if verdict is not None:
            return verdict
        verdict = _check_secret_read(segment)
        if verdict is not None:
            return verdict

    return _ALLOW


def is_forbidden(command: object) -> bool:
    """Convenience boolean wrapper around :func:`check_command`."""
    return check_command(command).blocked


def blocked_categories(command: object) -> set[str]:
    """Every blocked ``CATEGORY_*`` across ALL segments of ``command``. PURE.

    Unlike :func:`check_command`, which returns the FIRST blocked verdict and
    stops, this scans every segment and runs BOTH the push-main and secret-read
    checks on each, collecting every offending category into a set. That matters
    for a *category-filtered* enforcer (Eli, who drops only the push-main axis):
    a compound like ``git push origin main && keyring get x`` must surface the
    secret-read category even though push-main is detected first — otherwise the
    first-verdict short-circuit would hide the secret read and let it through.

    Non-string / empty input ⇒ empty set (defensive, mirrors ``check_command`` —
    never raises)."""
    cats: set[str] = set()
    if not isinstance(command, str) or not command.strip():
        return cats
    for segment in _segments(command):
        for check in (_check_push_main, _check_secret_read):
            verdict = check(segment)
            if verdict is not None and verdict.category is not None:
                cats.add(verdict.category)
    return cats


# ── built-artifact diff detection (issue #51) ───────────────────────────────

def _normalise_path(raw: object) -> str:
    """Repo-relative posix path for ``raw`` (backslashes → '/', strip a leading
    ``./``). Non-string / empty ⇒ "" (the caller skips it)."""
    if not isinstance(raw, str):
        return ""
    norm = raw.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def check_pushed_paths(paths: object) -> Verdict:
    """Guard the set of file paths a push would carry. PURE — never raises.

    Blocks if ANY changed path falls under a built-artifact directory
    (:data:`FORBIDDEN_ARTIFACT_PREFIXES`, currently ``dashboard_v2/``). Agent PRs
    are source-only; the bundle is rebuilt once at human-merge time, so a run that
    commits/pushes a rebuilt ``dashboard_v2/`` reintroduces exactly the pairwise
    conflicts issue #51 exists to kill. The offending path is named in the
    verdict.

    The pre-push hook resolves the REAL diff a push carries (not the typed
    command) and runs the paths through here — so this is the diff-axis sibling of
    :func:`check_command`'s protected-branch axis. Non-iterable / empty input is
    treated as not-blocked (defensive: the predicate must never crash on bad
    input, and an empty diff is trivially allowed).
    """
    if not paths:
        return _ALLOW
    try:
        items = list(paths)
    except TypeError:
        return _ALLOW

    for raw in items:
        norm = _normalise_path(raw)
        if not norm:
            continue
        for prefix in FORBIDDEN_ARTIFACT_PREFIXES:
            bare = prefix.rstrip("/")
            if norm == bare or norm.startswith(prefix):
                return Verdict(
                    blocked=True,
                    reason=(
                        f"diff touches the built bundle (`{norm}`) — agent PRs are "
                        f"source-only; `{bare}` is rebuilt once at human-merge "
                        "time, never committed by a run."
                    ),
                    category=CATEGORY_BUILT_ARTIFACT,
                )
    return _ALLOW
