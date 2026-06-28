"""
trident_loop_gate.py — the durable architecture-FREEZE gate (Slice 3 of PRD #58,
issue #62).

What this is
------------
When Eli's architectural call (``trident_eli_architect.decide_replan``) returns a
:class:`~trident_eli_architect.Freeze` — i.e. the remaining work has drifted past
the parent PRD's product scope — the whole autonomous loop must STOP claiming new
work until the PM resolves it in a session. This module owns the small DURABLE,
injectable seam that records and reads that freeze state.

Two HARD design constraints (both from #62):

  1. **Decoupled from the dashboard watch-toggle.** "Eli froze on scope drift" is
     a DISTINCT state from "Nathan paused the loop". The watch-toggle persists to
     ``watch_gate.json``; this freeze persists to a SEPARATE sentinel
     (``loop_freeze.json``). Neither reads the other — a PM un-pausing the watch
     toggle must NOT silently clear an architecture freeze, and vice-versa. The
     live serial loop (Slice #64) will check BOTH gates independently.

  2. **NOT wired into the live loop here.** This slice only makes the freeze
     WRITABLE + READABLE and fully fake-tested. Gordon-respects-the-freeze and the
     arm-switch master are Slice #64's job — do not import this into the selector.

Durability + idiom
------------------
A single gitignored JSON sentinel under the runtime workspace, written with the
same atomic temp-then-``os.replace`` idiom as ``trident_agent_run_store`` (and the
watch-gate). Gitignored so a clone can NEVER inherit "frozen" through a git pull —
the host owns its own freeze state (exactly the reasoning ``watch_gate.json`` uses
in .gitignore). The sentinel's PRESENCE is the freeze; its absence is "clear".

The seam
--------
``LoopGateLike`` is the injected protocol the pipeline depends on:

    freeze(*, reason, issue) -> None     # write the freeze (idempotent overwrite)
    is_frozen() -> bool                  # True while the sentinel exists
    frozen_reason() -> Optional[str]     # the recorded reason, or None when clear

Production backs it with :class:`JsonLoopGate`; tests pass a fake or a real
``JsonLoopGate(tmp_path)``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Protocol

_LOG = logging.getLogger(__name__)

# The sentinel filename, sibling to watch_gate.json under the runtime workspace.
# DELIBERATELY distinct from watch_gate.json so the architecture freeze and the
# manual watch pause are independent states (constraint #1). Gitignored — added
# to .gitignore alongside watch_gate.json so a clone never inherits "frozen".
DEFAULT_FILENAME = "loop_freeze.json"


# ═══════════════════════════════════════════════════════════════════════════════
# The injected seam protocol
# ═══════════════════════════════════════════════════════════════════════════════

class LoopGateLike(Protocol):
    """The freeze-gate seam the architect pipeline depends on. Production wires
    :class:`JsonLoopGate`; tests pass a fake."""

    def freeze(self, *, reason: str, issue: int) -> None: ...
    def is_frozen(self) -> bool: ...
    def frozen_reason(self) -> Optional[str]: ...


# ═══════════════════════════════════════════════════════════════════════════════
# The durable JSON-sentinel implementation
# ═══════════════════════════════════════════════════════════════════════════════

class JsonLoopGate:
    """A durable freeze gate backed by a single gitignored JSON sentinel.

    The sentinel's PRESENCE means frozen; its ABSENCE means clear. Writing is
    atomic (temp + ``os.replace``) so a crash mid-write can't leave a torn file.
    Reads tolerate a missing or corrupt sentinel (treated as a freeze that can't
    be parsed — see ``is_frozen`` / ``frozen_reason``), never raising at the
    call site.

    Schema (one object)::

        {
          "frozen":     true,             # always true while the file exists
          "reason":     "drifted past …", # the architectural reason (PM-facing)
          "issue":      62,               # the issue/PRD that triggered the freeze
          "frozen_at":  1718900000.0      # epoch seconds the freeze was written
        }
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)

    # ── write ──────────────────────────────────────────────────────────────────

    def freeze(self, *, reason: str, issue: int) -> None:
        """Write the freeze sentinel (idempotent overwrite). Atomic.

        Re-freezing while already frozen simply overwrites with the latest reason
        — the gate is a latch, not a counter. The directory is created if absent
        (the runtime workspace may not yet have it)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "frozen": True,
            "reason": (reason or "").strip(),
            "issue": int(issue),
            "frozen_at": time.time(),
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self._path)
        _LOG.warning("loop FROZEN on architecture drift (issue #%d): %s",
                     issue, payload["reason"])

    def clear(self) -> bool:
        """Remove the freeze sentinel (the PM resolved it). Returns True if a
        freeze existed and was cleared, False if it was already clear. NOT wired
        to any live path in this slice — provided so the PM-resolution flow (a
        later slice / a dashboard control) has a primitive to call."""
        try:
            self._path.unlink()
            _LOG.info("loop freeze cleared")
            return True
        except FileNotFoundError:
            return False

    # ── read ───────────────────────────────────────────────────────────────────

    def is_frozen(self) -> bool:
        """True iff the freeze sentinel exists. A corrupt-but-present sentinel
        still counts as FROZEN — fail safe: we never auto-unfreeze on a parse
        error, that requires the PM to clear it explicitly."""
        return self._path.is_file()

    def frozen_reason(self) -> Optional[str]:
        """The recorded freeze reason, or None when the gate is clear.

        A present-but-corrupt sentinel returns a generic frozen reason rather
        than None (so a reader still sees "frozen") — again, never silently
        treat a corrupt freeze as clear."""
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "frozen (freeze sentinel present but unreadable)"
        reason = data.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        return "frozen (no reason recorded)"


def default_loop_gate(workspace: str | os.PathLike) -> JsonLoopGate:
    """The loop-freeze gate rooted at ``<workspace>/loop_freeze.json`` (gitignored
    runtime state, sibling to watch_gate.json)."""
    return JsonLoopGate(Path(workspace) / DEFAULT_FILENAME)
