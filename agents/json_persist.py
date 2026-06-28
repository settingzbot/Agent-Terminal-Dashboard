"""
trident_agent_json_persist.py — tiny JSON-file persistence primitives (issue #140).

What this is
------------
Two deep modules that each own ONE pattern the agent-manager kept open-coding:
"persist a small value to a JSON file at the workspace root, survive a restart,
and never blow up on a missing or hand-corrupted file."

* :class:`JsonFlag` — a single boolean (the dashboard ARM / supervised toggles).
  On-disk shape ``{"enabled": <bool>}`` — unchanged from the manager's old
  watch_gate.json / supervised.json so a live host's existing file still loads.
* :class:`JsonSet` — a set of strings (the completed-ack / problems-ack markers).
  On-disk shape is a sorted JSON list — unchanged from the manager's old
  completed_ack.json / problems_ack.json.

Both share the same three guarantees:

* **default-on-missing** — a file that does not exist reads as the configured
  default (a flag's ``default`` boolean, a set's empty set). No write happens on
  a read, so a fresh workspace never fails OPEN by accident.
* **corrupt-file tolerance** — an unreadable / non-JSON / wrong-shape file reads
  as the same default rather than raising, so a hand-edited bad file can't break
  the manager. Corruption is logged at WARNING.
* **atomic write** — every save goes to a temp sibling then ``os.replace``s over
  the target, so a crash mid-write can never leave a half-written file that a
  later read would choke on (it would just fall back to the default).

The manager keeps owning the file PATHS and its ``_lock``; these classes are pure
persistence primitives it composes (one per persisted value). They hold no
manager state and never reach back into it.

Run:  python -m pytest tests/test_agent_json_persist.py -q
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically (temp sibling + ``os.replace``).

    The parent dir is created if missing so a first-ever save on a fresh
    workspace succeeds. The temp file sits beside the target (same dir → same
    filesystem, so ``os.replace`` is a true atomic rename, not a cross-device
    copy)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


class JsonFlag:
    """A single boolean persisted to a JSON file as ``{"enabled": <bool>}``.

    Constructed with the file path (the manager owns it) and an explicit
    ``default`` returned whenever the file is missing or corrupt — the toggle
    never turns itself ON by accident, so the default is the SAFE state (the
    watch-gate disarms, supervised stays OFF).
    """

    def __init__(self, path: str | os.PathLike, *, default: bool) -> None:
        self._path = Path(path)
        self._default = bool(default)

    def load(self) -> bool:
        """Read the persisted flag. Returns ``default`` on a missing / unreadable
        / non-JSON / wrong-shape file (corruption is logged, never raised)."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return self._default
        except (OSError, ValueError, TypeError):
            _LOG.warning(
                "%s unreadable — treating as default (%s)",
                self._path.name, self._default)
            return self._default
        if not isinstance(data, dict):
            _LOG.warning(
                "%s is not a JSON object — treating as default (%s)",
                self._path.name, self._default)
            return self._default
        return bool(data.get("enabled", self._default))

    def save(self, enabled: bool) -> None:
        """Persist the flag atomically as ``{"enabled": <bool>}``."""
        _atomic_write(self._path, json.dumps({"enabled": bool(enabled)}))


class JsonSet:
    """A set of strings persisted to a JSON file as a sorted list.

    Constructed with the file path (the manager owns it). A missing or corrupt
    file reads as the empty set — the marker is purely additive (ack folds ids
    in), so "nothing acknowledged yet" is the correct default for a fresh or
    damaged file.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)

    def load(self) -> set[str]:
        """Read the persisted set. Returns an empty set on a missing / unreadable
        / non-JSON / non-list file (so corruption degrades to "nothing seen",
        never raises). Elements are coerced to ``str``."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError):
            _LOG.warning("%s unreadable — treating as empty set", self._path.name)
            return set()
        if not isinstance(data, list):
            _LOG.warning(
                "%s is not a JSON list — treating as empty set", self._path.name)
            return set()
        return {str(x) for x in data}

    def save(self, values: set[str]) -> None:
        """Persist the set atomically as a sorted JSON list (stable on-disk order
        makes a hand-diff readable and a re-save idempotent byte-for-byte)."""
        _atomic_write(self._path, json.dumps(sorted(values)))
