"""What this bridge knows about each conversation, in one row per conversation.

Three things are remembered, and all three are the same shape of fact -- a
conversation id and something this bridge decided about it:

  * **whether it is ours.** The chat list used to show everything
    `list_sessions()` returned, which on a working machine is mostly
    conversations from a desktop with a different set of tools, a different
    screen and a different idea of what a path looks like. Continuing one of
    those *from Windows 98* rarely makes sense. There is no settable tag in
    ClaudeAgentOptions, so the bridge remembers the ids it started.
  * **its model, effort and permission mode.** THE CONVERSATION'S, not the
    machine's. These used to live on the guest and nowhere else, which meant
    opening a chat you had set to Plan and Opus gave you whatever the last
    chat was using, and a bridge restart put every conversation back on the
    process defaults. Neither is a choice anyone made.
  * **its folder**, on both sides. Remembered rather than read back from the
    session: a conversation's folder is named after the conversation, and the
    CLI names the conversation from its FIRST MESSAGE -- by which point the
    session has already recorded the directory it STARTED in, which is the
    root. Asking the SDK afterwards gives the wrong answer, and reopening a
    chat showed "<unset>" for a conversation whose folder was on disk.

WHY ONE FILE. These were three classes over three files -- `sessions.json`,
`folders.json`, `settings.json` -- each written because the one beside it
already existed, with three copies of the loader and three copies of the
atomic write. Splitting them bought nothing and cost a bug: three writers,
three moments, and one of them keyed on a value that was empty at the moment
it ran, so `settings.json` was never created at all while `folders.json`
beside it filled up normally. One row written from one place is harder to get
that wrong.

The cost, stated rather than discovered later: a corrupt file now loses the
chat list, the folders AND the settings together, where it used to lose one.
All three are logged-and-ignored rather than fatal and all recover by using
the app -- the chat list refills, a folder is renamed on the next message,
settings cost three menu selections -- so the trade is deliberate.

Losing the file is recoverable. Refusing to start is not: see `Baseline` for
where the opposite posture belongs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("bridge.conversations")


def default_path() -> Path:
    """Resolved per call, not at import: tests point GASTON_STATE at a
    temporary file, and a module-level constant would have been read before
    they got the chance."""
    env = os.environ.get("GASTON_STATE")
    return Path(env) if env else (Path.home() / ".gaston-code"
                                  / "conversations.json")


#: The three files this replaced, oldest first. READ FOREVER, NEVER WRITTEN.
#:
#: Not a migration step that runs once. A one-shot conversion has a moment
#: where it has half happened, and this machine has a bridge from before this
#: change that may still be running and still appending to `sessions.json` --
#: which would then be a conversation that silently never reaches the chat
#: list again. Reading them on every load instead means the old files stay
#: authoritative for anything the new one has not been told about, an old
#: bridge running alongside loses nothing, and rolling back is running the
#: previous binary rather than restoring anything.
LEGACY = ("sessions.json", "folders.json", "settings.json")


class Conversations:
    """One JSON object, `{conversation_id: {...}}`, atomically replaced.

    A row carries whichever of these it has been told:

        mine    true if this bridge started the conversation
        model   "" is a REAL value meaning the bridge's own default
        effort  likewise
        mode    one of the permission modes
        local   the folder's name under --sync-dir, on this side
        guest   the folder as the GUEST spells it, an absolute DOS path

    Absent and empty are different everywhere: "I have not chosen for this
    chat" and "I chose the default for this chat" are not the same answer,
    and only the second one is written down.
    """

    #: What `remember` will store. Named here so a typo is a KeyError in a
    #: test rather than a field that silently never persists.
    SETTINGS = ("model", "effort", "mode")

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self._map: dict[str, dict] = {}
        self._load()

    # --- loading ------------------------------------------------------------

    def _load(self) -> None:
        for name in LEGACY:
            self._read_legacy(self.path.parent / name)
        self._read(self.path)

    def _read(self, path: Path) -> None:
        """Merge one file of our own shape over what is already held.

        FIELD BY FIELD, not row by row. A row this file has nothing to say
        about must keep what a legacy file said, and a row it does mention
        must not drop a field it happens not to carry -- an id added to
        `sessions.json` by an older bridge after this file was last written
        is exactly that case.
        """
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("could not read %s", path)
            return
        if not isinstance(data, dict):
            return
        for key, row in data.items():
            if isinstance(row, dict):
                self._map.setdefault(str(key), {}).update(row)

    def _read_legacy(self, path: Path) -> None:
        """One of the three files this replaced, in its own shape."""
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("could not read %s", path)
            return

        if isinstance(data, list):                      # sessions.json
            for key in data:
                self._map.setdefault(str(key), {})["mine"] = True
            return
        if not isinstance(data, dict):
            return
        for key, row in data.items():
            entry = self._map.setdefault(str(key), {})
            if isinstance(row, str):
                # folders.json's FIRST format: just the local name. It was
                # read and never written by the code this replaces either.
                entry["local"] = row
            elif isinstance(row, dict):
                entry.update(row)

    # --- writing ------------------------------------------------------------

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._map, indent=1, sort_keys=True))
            tmp.replace(self.path)     # atomic, so a crash cannot truncate it
        except OSError:
            log.exception("could not write %s", self.path)

    def _put(self, session_id: str | None, **fields) -> None:
        """Merge `fields` into one row and save, unless nothing changed."""
        if not session_id:
            return
        entry = dict(self._map.get(session_id) or {})
        entry.update(fields)
        if self._map.get(session_id) == entry:
            return
        self._map[session_id] = entry
        self._write()

    # --- whose conversation it is ------------------------------------------

    def add(self, session_id: str | None) -> None:
        """Record that this bridge started the conversation."""
        self._put(session_id, mine=True)

    def __contains__(self, session_id: str) -> bool:
        return bool((self._map.get(session_id) or {}).get("mine"))

    def filter(self, rows: list[dict]) -> list[dict]:
        """Narrow a listing to conversations this bridge started.

        `mine`, not row presence: a row can exist because a conversation was
        given a folder or a model while it was open, which is not the same
        claim as this bridge having started it.
        """
        return [r for r in rows if r.get("id") in self]

    def __len__(self) -> int:
        """Rows, not conversations started -- `mine` is one field among six."""
        return len(self._map)

    # --- model, effort, permission mode ------------------------------------

    def settings(self, session_id: str | None) -> dict:
        """What this conversation was last set to, as far as it is known.

        Absent keys are absent rather than empty: "" is a real value for
        model and effort, so a caller has to be able to tell "never set" from
        "set to the default".
        """
        row = self._map.get(session_id or "")
        if not isinstance(row, dict):
            return {}
        return {k: row[k] for k in self.SETTINGS if k in row}

    def remember(self, session_id: str | None, **values) -> None:
        """Write model, effort and mode. Takes the whole trio, because a
        setting chosen before the first message has nowhere to be written
        until the conversation has an id -- and by then all three may have
        changed."""
        bad = [k for k in values if k not in self.SETTINGS]
        if bad:
            raise KeyError(f"not a remembered setting: {bad!r}")
        self._put(session_id,
                  **{k: v for k, v in values.items() if v is not None})

    # --- the folder it works in --------------------------------------------

    def folder(self, session_id: str | None) -> str:
        """The folder name on THIS side."""
        return (self._map.get(session_id or "") or {}).get("local", "")

    def guest_folder(self, session_id: str | None) -> str:
        """The folder as the GUEST spells it -- an absolute DOS path.

        Stored rather than derived, because the user can point a conversation
        at any folder on their machine and nothing about `C:\\Somewhere\\Else`
        can be worked out from a name on this side.
        """
        return (self._map.get(session_id or "") or {}).get("guest", "")

    def set_folder(self, session_id: str | None, folder: str,
                   guest_path: str = "") -> None:
        if not folder:
            return
        fields = {"local": folder}
        if guest_path:
            fields["guest"] = guest_path
        self._put(session_id, **fields)

    def taken(self) -> set:
        """Every local folder name already spoken for, so a second
        conversation about the same thing does not land in the first one's
        directory."""
        return {row["local"] for row in self._map.values()
                if isinstance(row, dict) and row.get("local")}
