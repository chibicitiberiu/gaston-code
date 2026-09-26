"""Parse the guest's control records.

The guest writes these in ``win/ui/uirec.cpp`` and this reads them; the two
are an agreement, pinned by ``tests/python/guest/test_uirec_agreement.py``
against a fixture the guest binary itself produces.

Length-prefixed, not separated, because a window's text may contain
anything at all -- an Edit control's contents come back through
``WM_GETTEXT``, newlines and tabs included. A line-oriented format works
right up until the first multi-line text box and then silently yields extra
records rather than failing.

One record:

    <src> <path> <parent> <ctrl> <child> <l> <t> <r> <b> <flags> \
        <n0>..<n6> <bytes...>\\n

with the seven byte-counts covering, in order: class, text, role, name,
value, state, action.

**There is no handle in here**, and that is deliberate rather than an
oversight -- see ``docs/UIAUTOMATION.md``. A control is addressed by a
selector this module's caller computes from these fields.
"""

from __future__ import annotations

import dataclasses
import logging

from . import fold

log = logging.getLogger("bridge.uirec")

#: Matches RC_NF_* in win/ui/uirec.h.
NF_VISIBLE = 0x01
NF_ENABLED = 0x02
NF_FOCUSED = 0x04
NF_TIMEDOUT = 0x08

#: The head is a fixed count of space-separated tokens. Splitting on a
#: bounded count rather than scanning is what keeps a value that happens to
#: contain a space from shifting every field after it -- the tail is found by
#: offset, never by searching.
_HEAD_TOKENS = 17

_STR_FIELDS = ("cls", "text", "role", "name", "value", "state", "action")


@dataclasses.dataclass(slots=True)
class UiNode:
    """One control, as the bridge sees it."""

    src: str            # "w" a window, "a" an accessible element, "g" a text run
    path: str           # child-index path from the root of this walk
    parent: str         # the path with its last component removed, "" for a root
    ctrl: int           # GetDlgCtrlID; 0 when there is none
    child: int          # MSAA child id, for src="a"
    rect: tuple[int, int, int, int]
    flags: int
    cls: str = ""
    text: str = ""
    role: str = ""
    name: str = ""
    value: str = ""
    state: str = ""
    action: str = ""
    #: Filled in by the selector builder, not by the guest.
    sel: str = ""

    @property
    def visible(self) -> bool:
        return bool(self.flags & NF_VISIBLE)

    @property
    def enabled(self) -> bool:
        return bool(self.flags & NF_ENABLED)

    @property
    def focused(self) -> bool:
        return bool(self.flags & NF_FOCUSED)

    @property
    def text_timed_out(self) -> bool:
        """The window did not answer WM_GETTEXT inside its budget.

        Not the same as empty text, and worth keeping apart: one means the
        control has nothing to say and the other means we could not ask.
        """
        return bool(self.flags & NF_TIMEDOUT)

    @property
    def label(self) -> str:
        """Whatever this control is most likely known by to a person."""
        return self.text or self.name or ""


def parse_records(body: bytes, acp: str = "") -> list[UiNode]:
    """Parse a UIENT body. Bad records are logged and skipped.

    Skipped rather than raised, for the reason the manifest parser gives: one
    unparseable record must not cost the whole tree, and the caller checks the
    count against ``END nodes=`` anyway, which is what turns a dropped record
    into a refused answer rather than a silently short one.
    """
    out: list[UiNode] = []
    pos = 0
    n = len(body)

    while pos < n:
        try:
            head_end = _head_end(body, pos)
            if head_end < 0:
                log.warning("uirec: truncated head at offset %d, %d records in",
                            pos, len(out))
                break
            tok = body[pos:head_end].split(b" ")
            if len(tok) != _HEAD_TOKENS:
                raise ValueError(f"head has {len(tok)} tokens, want {_HEAD_TOKENS}")

            lens = [int(t) for t in tok[10:17]]
            start = head_end + 1
            need = sum(lens)
            # +1 for the trailing newline. A record that claims more bytes
            # than are here is a truncation, and reading on would parse the
            # NEXT record's fields as this one's.
            if start + need + 1 > n:
                raise ValueError(
                    f"declared {need} bytes of fields but only {n - start} remain")

            node = UiNode(
                src=tok[0].decode("ascii", "replace"),
                path=tok[1].decode("ascii", "replace"),
                parent="" if tok[2] == b"-" else tok[2].decode("ascii", "replace"),
                ctrl=int(tok[3]),
                child=int(tok[4]),
                rect=(int(tok[5]), int(tok[6]), int(tok[7]), int(tok[8])),
                flags=int(tok[9]),
            )
            at = start
            for field, ln in zip(_STR_FIELDS, lens):
                setattr(node, field, fold.decode(body[at:at + ln], acp))
                at += ln
            if body[at:at + 1] != b"\n":
                raise ValueError("no newline where the record should end")
            out.append(node)
            pos = at + 1
        except (ValueError, IndexError) as exc:
            log.warning("uirec: skipping a record at offset %d: %s", pos, exc)
            # Resynchronise on the next newline. Imperfect by nature -- the
            # text may contain one -- but the alternative is abandoning every
            # record after the first bad one.
            nl = body.find(b"\n", pos)
            if nl < 0:
                break
            pos = nl + 1

    return out


def _head_end(body: bytes, pos: int) -> int:
    """Offset of the space that ends the head, or -1."""
    seen = 0
    i = pos
    n = len(body)
    while i < n:
        if body[i] == 0x0A:
            return -1           # a newline inside the head: not a record
        if body[i] == 0x20:
            seen += 1
            if seen == _HEAD_TOKENS:
                return i
        i += 1
    return -1
