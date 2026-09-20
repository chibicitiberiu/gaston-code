"""Which sessions this bridge created.

The chat list used to show everything `list_sessions()` returned, which on a
working machine is mostly conversations from a desktop with a completely
different set of tools, a different screen and a different idea of what a path
looks like. Continuing one of those *from Windows 98* rarely makes sense.

There is no settable tag in ClaudeAgentOptions, so the bridge remembers the ids
it started. Crude, but honest, and it survives a restart -- which a set held in
memory would not.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("bridge.registry")

def default_path() -> Path:
    """Resolved per call, not at import: tests point GASTON_STATE at a
    temporary file, and a module-level constant would have been read before
    they got the chance."""
    env = os.environ.get("GASTON_STATE")
    return Path(env) if env else Path.home() / ".gaston-code" / "sessions.json"


class Registry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self._ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, list):
                self._ids = {str(x) for x in data}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # A corrupt registry must not stop the bridge starting: the worst
            # case is an empty chat list, which is recoverable by using it.
            log.exception("could not read %s", self.path)

    def add(self, session_id: str | None) -> None:
        if not session_id or session_id in self._ids:
            return
        self._ids.add(session_id)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self._ids)))
            tmp.replace(self.path)     # atomic, so a crash cannot truncate it
        except OSError:
            log.exception("could not write %s", self.path)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._ids

    def filter(self, rows: list[dict]) -> list[dict]:
        return [r for r in rows if r.get("id") in self._ids]

    def __len__(self) -> int:
        return len(self._ids)


class Folders:
    """Which folder each conversation works in.

    REMEMBERED HERE rather than read back from the session, and that is not
    duplication. A conversation's folder is named after the conversation, and
    the CLI names the conversation from its FIRST MESSAGE -- by which point
    the session has already started and recorded the directory it started in,
    which is the root. Asking the SDK afterwards therefore gives the wrong
    answer, and reopening a chat showed "<unset>" for a conversation that had
    a folder sitting right there on disk.

    Same shape as `Registry` beside it: a small JSON file, atomic replace, a
    corrupt one logged and ignored rather than fatal. Losing it costs the
    folder NAME, not the folder -- the files are still there and the next
    message names a fresh one.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else (default_path().parent
                                             / "folders.json")
        self._map: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._map = {str(k): v for k, v in data.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.exception("could not read %s", self.path)

    def get(self, session_id: str | None) -> str:
        """The folder name on THIS side."""
        return self._entry(session_id).get("local", "")

    def guest(self, session_id: str | None) -> str:
        """The folder as the GUEST spells it -- an absolute DOS path.

        Stored rather than derived, because the user can point a
        conversation at any folder on their machine and nothing about
        `C:\\Somewhere\\Else` can be worked out from a name on this side.
        """
        return self._entry(session_id).get("guest", "")

    def _entry(self, session_id: str | None) -> dict:
        v = self._map.get(session_id or "")
        if isinstance(v, str):
            # The first format: just the local name. Read, never written.
            return {"local": v, "guest": ""}
        return v if isinstance(v, dict) else {}

    def set(self, session_id: str | None, folder: str,
            guest_path: str = "") -> None:
        if not session_id or not folder:
            return
        entry = dict(self._entry(session_id))
        entry["local"] = folder
        if guest_path:
            entry["guest"] = guest_path
        if self._map.get(session_id) == entry:
            return
        self._map[session_id] = entry
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._map, indent=1, sort_keys=True))
            tmp.replace(self.path)
        except OSError:
            log.exception("could not write %s", self.path)

    def taken(self) -> set[str]:
        """Every folder name already spoken for, for the uniqueness check."""
        return {self._entry(k).get("local", "") for k in self._map}
