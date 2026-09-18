"""Status vocabularies shared between the bridge and the guest.

Each is a small closed set of ASCII tokens that travels as a frame header's
literal value. Before this they were bare string literals at the point of
use -- `server.py`'s `isError` set was a second, independently maintained
literal tuple six lines from the status switch it has to agree with, so
adding a status meant remembering to edit both. docs/audit/02-bridge-python.md
02.14.

`SyncStatus` is deliberately NOT here yet: the sync vocabulary is bigger (a
dozen distinct tokens across SCAN/PUT/GET/MKDIR, some -- `too_long` -- reused
with a different meaning than the exec vocabulary's own `too_long`) and its
bare-literal comparisons in `files/syncrun.py` are read-only membership tests
rather than a dispatch table with a matching duplicated-list bug. Left as
outstanding work; see the finding.
"""

from __future__ import annotations

from enum import Enum


class ExecStatus(str, Enum):
    """What `EXECRESULT`'s `status=` header is allowed to say.

    The producer is win/rpc/rpc.cpp's switch over `RC_EXEC_*` -- read, never
    edited, from this package.
    """

    OK = "ok"
    TIMEOUT = "timeout"
    SPAWN_FAILED = "spawn_failed"
    CANCELLED = "cancelled"
    TOO_LONG = "too_long"
    BUSY = "busy"

    @classmethod
    def parse(cls, value: str) -> "ExecStatus":
        """The guest's own vocabulary, plus OK for anything this bridge does
        not recognise -- matching case-of-doubt behaviour before this type
        existed: an unlisted status fell through every `elif` with no prose
        and no `isError` tag."""
        try:
            return cls(value)
        except ValueError:
            return cls.OK

    @property
    def is_error(self) -> bool:
        return self is not ExecStatus.OK


#: The prose shown to the model above the command's own output. `TOO_LONG`'s
#: `{max_cmd}` is filled in by the caller, which is the one place that knows
#: `Bridge.EXEC_MAX_CMD`.
EXEC_STATUS_PROSE: dict[ExecStatus, str] = {
    ExecStatus.SPAWN_FAILED: (
        "The command could not be started on the connected machine."),
    ExecStatus.TOO_LONG: (
        # Was reported as spawn_failed, which reads as an environment problem
        # and invites a retry. It is a deterministic size rejection, so
        # retrying the same command never succeeds -- and the model has no
        # way to guess the limit unless it is told.
        "The command line was too long for the connected machine and was "
        "refused before it ran. The limit is about {max_cmd} characters "
        "including the shell prefix; shorten it, or write the long part to "
        "a file and run that."),
    ExecStatus.TIMEOUT: (
        "Timed out and was killed after the limit; partial output follows."),
    ExecStatus.BUSY: (
        "The connected machine is already running a command."),
    ExecStatus.CANCELLED: (
        "Cancelled -- the machine is shutting down."),
}
