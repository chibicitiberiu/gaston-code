"""What the client declares about how it wants to be served: the PREFS frame.

PER CLIENT, NOT PER BRIDGE (docs/OPTIONS-PLAN.md). Every setting the Options
dialog changes lives in the client's own INI and arrives here, once right
after HELLO and again whenever the person presses OK. Nothing is stored on
this side: a reconnect brings the client's current answer, so there is no
second copy to disagree with.

THE BODY, NOT HEADERS. A frame other than HELLO carries at most 16 headers of
255 bytes (wire/frame.py), and one protected-path list is longer than that.
Each line is `key=value`, in the client's ANSI codepage, split on the FIRST
`=` -- an ignore pattern may contain one.

REFUSED, NEVER GUESSED, like the HELLO caps: an unknown key or value is the
client and this bridge disagreeing about what a setting means, and the safe
answer to that is not the nearest one. The client bumps `prefs=N` in HELLO
when it adds a key, so a new client against an old bridge is refused at the
handshake with a reason instead of half-applied.

NO OS FACTS ARE MADE UP HERE. The protected places, dangerous and trusted
programs are the client's; they arrive as opaque text and are matched as
such (categories.py, paths.glob_match).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .agent import categories as K
from .agent import policy

#: Bounds, all of them refusals rather than truncations: a list cut short
#: in silence is a protected place quietly unprotected.
MAX_BODY = 16 * 1024
MAX_LINES = 512
MAX_LIST = 64
MAX_MODES = 8
MAX_LABEL = 31

PAGE_SIZES = ("small", "normal", "large")
#: Multipliers on the memory-scaled page limits (view/pages.py limits_for).
PAGE_SCALE = {"small": 0.5, "normal": 1.0, "large": 2.0}

#: A user mode's id: random hex from the client, so deleting gaston.ini and
#: making a new mode can never inherit an old chat's choice of a different one.
_MODE_ID = re.compile(r"^u-[0-9a-f]{8}$")
_PROGRAM = re.compile(r"^[^\s=\\/:*?\"<>|]{1,64}$")
_EFFORTS = ("", "low", "medium", "high", "xhigh", "max")


class PrefsError(Exception):
    """A line the bridge cannot honour. `key` names it, for PREFSACK."""

    def __init__(self, key: str, why: str) -> None:
        super().__init__(f"{key}: {why}")
        self.key = key
        self.why = why


@dataclass(frozen=True)
class Prefs:
    #: For NEW conversations; an open chat keeps what it has.
    general_model: str = ""
    general_effort: str = ""
    general_mode: str = ""
    page: str = "normal"
    #: Seconds between the bridge's own sync sweeps; 0 = the bridge's
    #: default, -1 = only when the machine reports a change.
    sync_sweep_s: int = 0
    #: Largest file the sync layer carries, KB; 0 = the bridge's default.
    sync_max_kb: int = 0
    sync_ignore: tuple = ()
    #: Seconds a permission bar waits before it is denied; 0 = default.
    perm_deadline_s: int = 0
    lists: K.Lists = field(default_factory=K.Lists)
    modes: dict = field(default_factory=dict)
    #: True when the client sent PREFS at all.
    declared: bool = False


DEFAULT = Prefs()


def _int(key: str, raw: str, lo: int, hi: int) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise PrefsError(key, f"{raw!r} is not a number") from None
    if not lo <= n <= hi:
        raise PrefsError(key, f"{n} is outside {lo}..{hi}")
    return n


def parse(body: bytes, acp: str = "cp1252") -> Prefs:
    """The client's PREFS body as Prefs. Raises PrefsError."""
    if len(body) > MAX_BODY:
        raise PrefsError("", f"{len(body)} bytes, over {MAX_BODY}")
    try:
        text = body.decode(acp or "cp1252")
    except (LookupError, UnicodeDecodeError) as exc:
        raise PrefsError("", f"not {acp}: {exc}") from None
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) > MAX_LINES:
        raise PrefsError("", f"{len(lines)} lines, over {MAX_LINES}")

    out: dict = {}
    protect: list = []
    danger: dict = {}
    trust: list = []
    ignore: list = []
    modes: dict = {}

    def grow(key: str, lst, item) -> None:
        if len(lst) >= MAX_LIST:
            raise PrefsError(key, f"more than {MAX_LIST} entries")
        lst.append(item)

    for line in lines:
        if "=" not in line:
            raise PrefsError(line[:40], "not key=value")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "general.model":
            out["general_model"] = value[:64]
        elif key == "general.effort":
            if value not in _EFFORTS:
                raise PrefsError(key, f"unknown effort {value!r}")
            out["general_effort"] = value
        elif key == "general.mode":
            out["general_mode"] = value
        elif key == "view.page":
            if value not in PAGE_SIZES:
                raise PrefsError(key, f"unknown size {value!r}")
            out["page"] = value
        elif key == "sync.sweep_s":
            out["sync_sweep_s"] = _int(key, value, -1, 86400)
        elif key == "sync.max_kb":
            out["sync_max_kb"] = _int(key, value, 0, 1024 * 1024)
        elif key == "sync.ignore":
            if value:
                grow(key, ignore, value[:200])
        elif key == "perm.deadline_s":
            out["perm_deadline_s"] = _int(key, value, 0, 86400)
        elif key == "protect":
            if value:
                grow(key, protect, value[:260])
        elif key == "trust":
            if not _PROGRAM.match(value):
                raise PrefsError(key, f"{value!r} is not a program name")
            grow(key, trust, value.lower())
        elif key.startswith("danger."):
            prog = key[len("danger."):]
            if not _PROGRAM.match(prog):
                raise PrefsError(key, f"{prog!r} is not a program name")
            if len(danger) >= MAX_LIST:
                raise PrefsError(key, f"more than {MAX_LIST} entries")
            danger[prog.lower()] = value[:120] or prog
        elif key.startswith("mode."):
            _mode_line(key, value, modes)
        else:
            raise PrefsError(key, "not a setting this bridge knows")

    built = {}
    for mid, raw in modes.items():
        if not raw.get("label"):
            raise PrefsError(f"mode.{mid}.label", "missing")
        values = dict(raw.get("values", {}))
        for cat in K.CATS:
            values.setdefault(cat, policy.ASK)
        built[mid] = policy.Mode(mid, raw["label"], raw.get("sdk", "default"),
                                 values, builtin=False)

    return Prefs(
        general_model=out.get("general_model", ""),
        general_effort=out.get("general_effort", ""),
        general_mode=out.get("general_mode", ""),
        page=out.get("page", "normal"),
        sync_sweep_s=out.get("sync_sweep_s", 0),
        sync_max_kb=out.get("sync_max_kb", 0),
        sync_ignore=tuple(ignore),
        perm_deadline_s=out.get("perm_deadline_s", 0),
        lists=K.Lists(protect=tuple(protect), danger=danger,
                      trust=frozenset(trust)),
        modes=built,
        declared=True,
    )


def _mode_line(key: str, value: str, modes: dict) -> None:
    parts = key.split(".")
    if len(parts) < 3 or not _MODE_ID.match(parts[1]):
        raise PrefsError(key, "a mode id is u- and eight hex digits")
    mid = parts[1]
    if mid not in modes and len(modes) >= MAX_MODES:
        raise PrefsError(key, f"more than {MAX_MODES} modes")
    m = modes.setdefault(mid, {"values": {}})
    field_ = parts[2]
    if field_ == "label" and len(parts) == 3:
        if not value or len(value) > MAX_LABEL:
            raise PrefsError(key, f"a label is 1..{MAX_LABEL} characters")
        m["label"] = value
    elif field_ == "sdk" and len(parts) == 3:
        if value not in ("default", "plan"):
            raise PrefsError(key, f"unknown sdk mode {value!r}")
        m["sdk"] = value
    elif field_ == "c" and len(parts) == 4:
        if parts[3] not in K.CATS:
            raise PrefsError(key, f"unknown category {parts[3]!r}")
        if value not in policy.VALUES:
            raise PrefsError(key, f"unknown value {value!r}")
        m["values"][parts[3]] = value
    else:
        raise PrefsError(key, "not a mode field this bridge knows")


def floor(old: Prefs, new: Prefs) -> tuple[Prefs, list]:
    """`new`, but never protecting LESS than `old` did within a session.

    The protected list is what keeps a system file in the system tier, and
    `gaston.ini` -- which holds it -- is a file the agent can be allowed to
    write. A list that could shrink mid-session is one the agent could
    shrink. Additions apply at once; removals at the next connect, and the
    second value is what was held back, so the person can be told.
    """
    kept = [p for p in old.lists.protect if p not in new.lists.protect]
    if not kept:
        return new, []
    lists = K.Lists(protect=new.lists.protect + tuple(kept),
                    danger=new.lists.danger, trust=new.lists.trust)
    return Prefs(**{**new.__dict__, "lists": lists}), kept


def scaled(p: Prefs, page_bytes: int, page_blocks: int,
           mem_kb: int = 0) -> tuple[int, int]:
    """Page caps after the client's page size. Small halves what its memory
    allows and Large doubles it, never past the ceiling every machine
    shares (view/pages.py: four times the 32MB page) and never below a
    page that can hold one long answer."""
    from .view import pages
    f = PAGE_SCALE.get(p.page, 1.0)
    if f == 1.0:
        return page_bytes, page_blocks
    return (max(4 * 1024, min(int(page_bytes * f), pages.MAX_BYTES * 4)),
            max(20, min(int(page_blocks * f), pages.MAX_BLOCKS * 4)))
