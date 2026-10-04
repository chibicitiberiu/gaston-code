"""What this bridge knows about each conversation, in one row per conversation.

Three things are remembered, and all three are the same shape of fact -- a
conversation id and something this bridge decided about it:

  * **whose it is.** The chat list used to show everything
    `list_sessions()` returned, which on a working machine is mostly
    conversations from a desktop with a different set of tools, a different
    screen and a different idea of what a path looks like. Continuing one of
    those *from Windows 98* rarely makes sense. There is no settable tag in
    ClaudeAgentOptions, so the bridge remembers the ids it started -- and,
    since 2026-10-01, WHICH MACHINE it started them for. "A different set of
    tools, a different screen, a different idea of what a path looks like"
    was always as true of a second guest as of the desktop, and `mine` alone
    cannot tell them apart: it is written on the bridge host and says only
    that THIS BRIDGE was involved. The machine says who it is in `HELLO`
    (`machine`, minted once and kept beside the exe -- `win/app/machineid.h`)
    and the id is filed beside `mine`.
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


#: The three files this replaced, oldest first. READ FOREVER, NEVER WRITTEN --
#: not a one-shot migration, because an old bridge may still be running
#: alongside this one and still appending to `sessions.json`. Reading them on
#: every load means the old files stay authoritative for anything the new one
#: hasn't been told, and rolling back is just running the previous binary.
LEGACY = ("sessions.json", "folders.json", "settings.json")


class Conversations:
    """One JSON object, `{conversation_id: {...}}`, atomically replaced.

    A row carries whichever of these it has been told:

        mine    true if this bridge started the conversation
        machine the guest it was started FROM, as HELLO's `machine` header.
                ABSENT MEANS ANY, and that is not a stopgap: every row
                written before 2026-10-01 has no machine (89 of them on the
                developer's box the day this landed), and a row that meant
                "no machine" rather than "any machine" would have emptied
                every chat list the moment this shipped.
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
    #: `mode` is the LEGACY key -- the CLI's permission-mode name -- still
    #: written so a rolled-back bridge finds something it understands;
    #: `mode_id` is the execution mode this bridge reads first.
    SETTINGS = ("model", "effort", "mode", "mode_id")

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

    def add(self, session_id: str | None, machine: str = "") -> None:
        """Record that this bridge started the conversation, on `machine`.

        An empty `machine` writes NO machine field rather than an empty one,
        so a client that does not identify itself leaves rows that every
        machine can see -- the same reading legacy rows get, arrived at the
        same way rather than by a second rule.
        """
        fields: dict = {"mine": True}
        if machine:
            fields["machine"] = machine
        self._put(session_id, **fields)

    def __contains__(self, session_id: str | None) -> bool:
        # `or ""`, the same normalisation every other lookup here uses:
        # `r.get("id")` in `filter` below can genuinely be None (a row with
        # no "id" key), and this has always tolerated that at runtime --
        # only the annotation was narrower than the behaviour.
        return bool((self._map.get(session_id or "") or {}).get("mine"))

    def machine(self, session_id: str | None) -> str:
        """Which guest this conversation was started from, "" if unrecorded."""
        return (self._map.get(session_id or "") or {}).get("machine", "")

    def belongs(self, session_id: str | None, machine: str = "") -> bool:
        """Is this a conversation `machine` should be offered?

        Two questions, and both have to be yes. `mine` is this BRIDGE's
        claim and `machine` is the GUEST's, and they fail differently: a
        conversation from another bridge has no row at all, where one from
        another guest has a row saying so.

        ABSENT ON EITHER SIDE MEANS ANY. A row with no machine predates the
        field and belongs to whoever is asking; a caller with no machine is
        a client that did not identify itself, and narrowing its list to
        nothing would be worse than the over-broad list it has always had.
        """
        row = self._map.get(session_id or "") or {}
        if not row.get("mine"):
            return False
        theirs = row.get("machine", "")
        return not machine or not theirs or theirs == machine

    def filter(self, rows: list[dict], machine: str = "") -> list[dict]:
        """Narrow a listing to conversations this bridge started, for this
        guest.

        `mine`, not row presence: a row can exist because a conversation was
        given a folder or a model while it was open, which is not the same
        claim as this bridge having started it.
        """
        return [r for r in rows if self.belongs(r.get("id"), machine)]

    def __len__(self) -> int:
        """Rows, not conversations started -- `mine` is one field among seven."""
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
