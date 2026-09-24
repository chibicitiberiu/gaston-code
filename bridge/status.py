"""Status vocabularies shared between the bridge and the guest.

Each is a small closed set of ASCII tokens that travels as a frame header's
literal value. Before this they were bare string literals at the point of
use -- `server.py`'s `isError` set was a second, independently maintained
literal tuple six lines from the status switch it has to agree with, so
adding a status meant remembering to edit both. docs/audit/02-bridge-python.md
02.14.

`SyncStatus` (02.38) is the sync vocabulary's turn: one enum for all four
SYNC RPCs (SYNCEND/SYNCPUTACK/SYNCMKDIRACK/SYNCDATEND), because they share
one header name (`status=`) and the Python-side reader (files/syncrun.py)
already treated it that way -- one status, whichever RPC produced it --
before this type existed. It is bigger than `ExecStatus` (sixteen tokens
against six) and, unlike `ExecStatus`, does NOT fall back to OK on an
unrecognised value -- see `SyncStatus.parse`'s docstring for why that
matters here specifically. `test_guest_agreement.py`'s
`test_sync_status_vocabulary_matches_the_guest` diffs this enum's values
against `win/sync/syncrpc.cpp`'s literal strings, the same file this type's
docstring points at, so the two cannot drift silently the way `ExecStatus`'s
old duplicated `isError` tuple once did.

`PromptEndReason` (02.38) types `PERMEND`'s `reason=` header -- three
literals (`_finish_prompt`'s "timeout"/"cancelled", `_perm_reply`'s
"answered") that were bare strings at the point of use. It has no matching
guest-side agreement test: `win/app/bar.cpp`'s PERMEND handler never reads
this header at all, it just takes the bar down for the matching id, so
there is no C vocabulary to diff against -- only a Python-side one worth
closing so the reason is not an open string at three call sites. Because it
IS sent on the wire (unlike `SyncStatus`, which is only ever received),
every call site that reaches `permend()` uses `.value` explicitly: a bare
`(str, Enum)` member's `str()` is `"PromptEndReason.TIMEOUT"`, not
`"timeout"` (confirmed against this interpreter before relying on it), and
`Frame.encode()` calls `str()` on every header value.
"""

from __future__ import annotations

from enum import Enum, auto


class GuestState(Enum):
    """Derived guest session state — a projection over orthogonal runtime axes.

    This is a COMPUTED PROPERTY, not a stored field. It is derived from
    ``turn_lock``, ``synccoord.halt_id``, ``perms.pending``,
    ``exec_pending`` and ``writer``/``lost_at``.

    Priority: DISCONNECTED > HALTED > RUNNING_TURN > IDLE.

    The axes are orthogonal — turn-running, halt-raised, permission-
    pending and exec-in-flight can all be true at the same time. This
    enum is a projection, not a replacement.
    """
    IDLE = auto()
    RUNNING_TURN = auto()
    HALTED = auto()
    DISCONNECTED = auto()


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
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "ExecStatus":
        """The guest's own vocabulary, or UNKNOWN for anything this bridge does
        not recognise. Falling back to OK would read an unrecognised status as
        success, which is the wrong direction to guess wrong in."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

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
    ExecStatus.UNKNOWN: (
        "The guest reported an unrecognised command status."),
}


class SyncStatus(str, Enum):
    """What SYNCEND/SYNCPUTACK/SYNCMKDIRACK/SYNCDATEND's `status=` header is
    allowed to say -- the union of all four SYNC RPCs' vocabularies.

    The producer is win/sync/syncrpc.cpp's four do_* handlers -- read, never
    edited, from this package. Grouped by the RPC that can send each one
    (a value can be sent by more than one; `too_long`, `bad_path` and
    `no_parent` each appear under two):

      * scan (SYNCEND):        OK, NO_ROOT, TOO_MANY
      * put (SYNCPUTACK):      OK, BUSY, BAD_PATH, READONLY, NO_PARENT,
                                NO_SPACE, LOCKED, TOO_LONG, CHANGED,
                                CRC_MISMATCH
      * mkdir (SYNCMKDIRACK):  OK, BAD_PATH, EXISTS, IS_FILE, NO_PARENT,
                                TOO_LONG, NO_SPACE
      * get (SYNCDATEND):      OK, BAD_PATH, LOCKED, IS_DIRECTORY, MISSING,
                                TOO_LONG
    """

    OK = "ok"
    NO_ROOT = "no_root"
    TOO_MANY = "too_many"
    BUSY = "busy"
    BAD_PATH = "bad_path"
    READONLY = "readonly"
    NO_PARENT = "no_parent"
    NO_SPACE = "no_space"
    LOCKED = "locked"
    #: 03.41: means something different from `ExecStatus.TOO_LONG` -- a
    #: PATH too long for the guest's filesystem, not a COMMAND LINE too long
    #: for its shell. Same word, two unrelated limits; see
    #: `Pass._too_long_detail`, which exists because of exactly this.
    TOO_LONG = "too_long"
    CHANGED = "changed"
    CRC_MISMATCH = "crc_mismatch"
    EXISTS = "exists"
    IS_FILE = "is_file"
    IS_DIRECTORY = "is_directory"
    MISSING = "missing"
    #: NOT one of the guest's own tokens -- what `parse` returns for a value
    #: that is not. See `parse`'s docstring for why this exists instead of
    #: falling back to OK the way `ExecStatus.parse` does.
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "SyncStatus":
        """Unlike `ExecStatus.parse`, this does NOT fall back to OK.

        Every sync call site tests for OK by inequality (`if status != "ok"`)
        or for a specific member (`status == "no_parent"`), which is how the
        pre-type code already read an unrecognised value: as "not this
        particular good or special case", i.e. a refusal. Falling back to OK
        here would flip that -- an unrecognised status would read as SUCCESS
        instead, which is the wrong direction to guess wrong in for a sync
        write. UNKNOWN preserves the old behaviour (not equal to OK, not
        equal to any named case) while still being a member of the enum
        rather than a bare string that slipped through.
        """
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class PromptEndReason(str, Enum):
    """PERMEND's `reason=` header.

    Purely informational on the wire: win/app/bar.cpp's PERMEND handler
    takes the bar down for the matching id and never reads this header, so
    there is no guest-side vocabulary to diff against the way `SyncStatus`
    has one. Typed anyway so the three call sites (`_finish_prompt`'s
    TIMEOUT/CANCELLED, `_perm_reply`'s ANSWERED) are not open strings, and so
    a fourth one added later has a set to join instead of a precedent to
    half-remember.

    Every call site that reaches `permend()` must pass `.value`, not the
    member itself -- `Frame.encode()` calls `str()` on every header value,
    and `str()` on a bare `(str, Enum)` member is `"PromptEndReason.X"`, not
    `"x"`.
    """

    ANSWERED = "answered"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
