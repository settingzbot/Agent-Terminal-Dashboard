#!/usr/bin/env python3
"""Re-parenting launcher — spawn a target process fully detached, then exit.

Why this exists (harness harden Part 2, 2026-06-24, [[footguns#192]]):
the pty-manager and agent-manager are spawned by the dashboard, and on Windows
``DETACHED_PROCESS`` does NOT reparent — the spawner stays the parent in the
process tree. So a ``taskkill /T`` anywhere up that chain (e.g. station_watchdog
killing the dashboard, or the old [[footguns#191]] agent-manager ``/T``)
cascades down the parent-PID chain and takes the pty-manager — and every live
terminal — with it.

The fix: don't let the long-lived daemon be a child of the dashboard at all.
This launcher is a throwaway middle process. The dashboard spawns IT; it spawns
the real target (still ``DETACHED_PROCESS``) and then exits IMMEDIATELY. Once
this launcher is gone, the target is ORPHANED — its parent PID points at this
dead launcher, which is not the dashboard and not any live descendant of it.
``taskkill /T <dashboard>`` walks the live parent-PID tree, never reaches the
orphan, and the pty-manager survives. (Windows does not re-parent orphans to a
root the way Unix re-parents to init, so the link stays severed.)

The target keeps its own lifecycle: it writes its own PID file and is managed by
PID (graceful shutdown RPC / verified PID-kill), so orphaning changes nothing
about how it's stopped — only that it can't be killed *by accident* as
collateral of a dashboard tree-kill.

Usage:
    python spawn_detached.py --stderr <path> -- <exe> [args...]

``--stderr`` is the file the target's stderr is appended to (so a native
ConPTY/pywinpty crash still lands somewhere — [[footguns#188]]). Everything
after ``--`` is the target command, run verbatim. Exit codes: 0 = launched,
2 = bad arguments, 3 = spawn failed.
"""
from __future__ import annotations

import os
import subprocess
import sys

# CREATE_NO_WINDOW: the target runs with a HIDDEN console — windowless itself, and
# its own console-app children inherit that hidden console instead of flashing a
# new window (DETACHED_PROCESS gives *no* console, which makes children pop their
# own). CREATE_NEW_PROCESS_GROUP: the target leads its own Ctrl-C/Break group.
# Neither reparents — the orphaning (this launcher exiting) is what severs the
# tree; these just keep it quiet.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _parse_args(argv: list[str]) -> tuple[str | None, list[str]]:
    """Return (stderr_path, command). Minimal hand-parse — argparse would choke
    on the target's own flags after ``--``."""
    stderr_path: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--stderr" and i + 1 < len(argv):
            stderr_path = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--":
            return stderr_path, argv[i + 1:]
        # First bare token with no preceding "--" is a usage error — we require
        # the explicit separator so the target's flags are never misread as ours.
        break
    return stderr_path, []


def main() -> int:
    stderr_path, command = _parse_args(sys.argv[1:])
    if not command:
        sys.stderr.write(
            "spawn_detached: usage: spawn_detached.py --stderr <path> -- <exe> [args...]\n")
        return 2

    creationflags = 0
    if sys.platform == "win32":
        creationflags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP

    # Open the target's stderr sink. The target inherits its own dup of the
    # handle; this launcher's copy is closed when it exits moments from now.
    stderr_f = None
    if stderr_path:
        try:
            os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
            stderr_f = open(stderr_path, "ab")
        except OSError:
            stderr_f = None  # diagnostics are best-effort; never block the spawn

    try:
        subprocess.Popen(
            command,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f or subprocess.DEVNULL,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001 — report and signal failure, don't raise
        sys.stderr.write(f"spawn_detached: failed to launch {command!r}: {e}\n")
        return 3
    finally:
        if stderr_f is not None:
            stderr_f.close()

    # Return immediately. This process dying is the whole point — it orphans the
    # target out of the spawner's kill tree.
    return 0


if __name__ == "__main__":
    sys.exit(main())
