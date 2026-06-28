"""Process-spawn helpers shared across the daemon clients.

Keeps the "don't flash a console window" knowledge in one place so the
pty-manager and agent-manager spawns stay consistent.
"""
from __future__ import annotations

import os
import sys


def windowless_python() -> str:
    """Return the Python interpreter to spawn background daemons with.

    On Windows, prefer ``pythonw.exe`` — the GUI-subsystem interpreter, which
    never allocates a console window. ``python.exe`` is a console-subsystem
    binary, and ``DETACHED_PROCESS`` alone does not reliably keep its console
    from flashing into view when the spawner itself owns a console (the launcher
    window). ``pythonw.exe`` is windowless by construction, so the lazily-spawned
    managers stay invisible regardless of how the dashboard was started.

    Falls back to ``sys.executable`` off-Windows or if ``pythonw.exe`` is missing.
    """
    exe = sys.executable
    if sys.platform != "win32":
        return exe
    if os.path.basename(exe).lower().startswith("pythonw"):
        return exe
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else exe
