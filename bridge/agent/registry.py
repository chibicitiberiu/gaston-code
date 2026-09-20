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


class Settings:
    """Model, effort and permission mode, per conversation.

    THE CONVERSATION'S, not the machine's. These used to live on the guest
    and nowhere else, which meant two things that both read as bugs: opening
    a chat you had set to Plan and Opus gave you whatever the last chat was
    using, and a bridge restart put every conversation back on the process
    defaults. Neither is a choice anyone made.

    Written whenever one changes and read when a conversation is opened. A
    setting chosen BEFORE the first message has nowhere to be written yet --
    there is no conversation id until the client exists -- so it is held on
    the guest and saved at the moment the id appears. That is why `set` takes
    the whole trio rather than one field: by then all three may have changed.

    Same shape as `Folders` beside it, and for the same reasons: a small JSON
    file, atomic replace, a corrupt one logged and ignored rather than fatal.
    Losing it costs three menu selections, not work.
    """

    #: What is remembered. Named here so a typo is a KeyError in a test
    #: rather than a field that silently never persists.
    FIELDS = ("model", "effort", "mode")

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else (default_path().parent
                                             / "settings.json")
        self._map: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._map = {str(k): v for k, v in data.items()
                             if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.exception("could not read %s", self.path)

    def get(self, session_id: str | None) -> dict:
        """What this conversation was last set to, as far as it is known.

        Absent keys are absent rather than empty: "" is a real value for
        model and effort (it means the bridge's own default), so a caller has
        to be able to tell "never set" from "set to the default".
        """
        v = self._map.get(session_id or "")
        if not isinstance(v, dict):
            return {}
        return {k: v[k] for k in self.FIELDS if k in v}

    def set(self, session_id: str | None, **values) -> None:
        bad = [k for k in values if k not in self.FIELDS]
        if bad:
            raise KeyError(f"not a remembered setting: {bad!r}")
        if not session_id:
            return
        entry = dict(self._map.get(session_id) or {})
        entry.update({k: v for k, v in values.items() if v is not None})
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

    def __len__(self) -> int:
        return len(self._map)


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
