"""Process-wide bridge configuration and directory containment validation.

Carries immutable or process-wide settings: host, port, secret, cwd, model,
effort, link_bps, sync directories, timeouts, and containment rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PORT = 7890


@dataclass
class BridgeConfig:
    secret: str
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    cwd: str | None = None
    model: str | None = None
    effort: str | None = None
    link_bps: float | None = None
    sync_dir: str | None = None
    sync_state: str | None = None
    sync_sweep_s: float = 120.0
    idle_timeout_s: float = 90.0
    #: How long a guest that has gone stays in the registry before it is
    #: forgotten, with its SDK session closed. Nothing ever forgot one while
    #: there was a single slot -- the session was kept deliberately so a
    #: reconnect reattached it. Keyed by machine id (stage 3c) that is one
    #: retained session per id ever seen, and ids churn: machineid.cpp
    #: re-mints over a malformed machine.id. An hour is far longer than any
    #: reconnect and far shorter than a week of them.
    evict_after_s: float = 3600.0
    idle_check_s: float = 15.0
    perm_grace_s: float = 60.0
    hello_timeout_s: float = 10.0

    def validate(self) -> None:
        """Validate directory containment and configuration constraints.

        The baseline must not live inside the tree it describes, or a full
        second copy of every file is mirrored to the guest, then scanned,
        then copied again. And it must not live inside the working directory,
        which is a different requirement that happened to hold by default.
        off_limits refuses paths outside cwd; it does not cover Bash, and
        nothing else guards the baseline. A state directory under cwd is
        therefore one `sed -i` away from a baseline that still parses and is
        wrong.
        """
        if not self.sync_dir:
            return

        self.sync_dir = os.path.abspath(self.sync_dir)
        if not self.sync_state:
            self.sync_state = os.path.abspath(
                os.path.join(self.sync_dir, os.pardir, ".gaston-state")
            )
        else:
            self.sync_state = os.path.abspath(self.sync_state)

        if os.path.commonpath([self.sync_state, self.sync_dir]) == self.sync_dir:
            raise ValueError(
                "--sync-state must be OUTSIDE --sync-dir: a baseline inside "
                "the tree it describes gets mirrored to the guest and recurses"
            )

        if self.cwd:
            wd = os.path.realpath(self.cwd)
            try:
                under_cwd = (
                    os.path.commonpath([os.path.realpath(self.sync_state), wd])
                    == wd
                )
            except ValueError:
                under_cwd = False
            if under_cwd:
                raise ValueError(
                    "--sync-state must be OUTSIDE --cwd: the agent can reach "
                    "anything under the working directory with Bash, and a "
                    "baseline that has been edited still parses"
                )
