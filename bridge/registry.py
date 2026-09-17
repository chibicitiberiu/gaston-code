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
