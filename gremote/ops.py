"""What the machine says, as types.

The guest transforms nothing (CLAUDE.md): it makes syscalls and moves bytes,
and records leave it as flat lines. Turning them into something with named
fields happens here, on the side that has a real language to do it in.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from bridge.agent import steps as _steps
from bridge.wire import uirec, uisel
from bridge.wire.frame import Frame


@dataclass(frozen=True)
class Process:
    """One row of `PS`.

    `pid` is unsigned and often enormous: on 9x a pid is an obfuscated
    pointer with the high bit set, which is why it travels as hex and why
    nothing should print it as a signed number.
    """

    pid: int
    ppid: int
    threads: int
    exe: str


@dataclass(frozen=True)
class Entry:
    """One row of `LS`. `type` is 'f', 'd', or 'u' for something the
    machine can see but not address -- which is a different answer from
    'there is nothing here', and the distinction matters: a caller told
    'unreadable' about a file that simply is not there retries forever."""

    name: str
    type: str
    size: int
    mtime: int
    attrs: int

    @property
    def is_dir(self) -> bool:
        return self.type == "d"


@dataclass(frozen=True)
class Stat:
    type: str
    size: int
    mtime: int
    attrs: int

    @property
    def is_dir(self) -> bool:
        return self.type == "d"


@dataclass(frozen=True)
class Drive:
    letter: str
    type: int
    #: True (probed, present), False (probed, nothing in it) or None (a
    #: removable or CD-ROM `DRIVES` did not probe -- BACKLOG 20). None is
    #: NOT the same as False: a mounted CD has no bytes free by definition,
    #: and so does a drive nobody asked about, and conflating the two
    #: reported every disc as an empty drive on the first guest run.
    has_media: bool | None
    serial: int
    total_kb: int
    free_kb: int
    label: str


@dataclass(frozen=True)
class FileBytes:
    """What a GET brought back.

    `crc` is of what was SENT, taken on the same pass that sent it, so it
    verifies the transfer rather than the file: for a truncated read it is
    the CRC of the prefix, which is the only thing there is to check.
    """

    data: bytes
    crc: int
    truncated: bool

    def verify(self) -> bool:
        return zlib.crc32(self.data) & 0xFFFFFFFF == self.crc


@dataclass(frozen=True)
class ExecResult:
    """What a command did.

    `exit` is only meaningful when `via_shell` is false. Measured on the VM:
    COMMAND.COM /c on Windows 98 exits 0 whatever happens, even for a program
    that does not exist, while a program spawned directly returns its real
    code. So this reports how it was started and lets the caller decide what
    the number is worth, rather than pretending.
    """

    output: bytes
    exit: int
    bytes_produced: int
    truncated: bool
    timed_out: bool
    via_shell: bool

    def text(self, encoding: str) -> str:
        """The output decoded with the machine's OEM codepage, which is what
        a console child writes -- not the ANSI one its filenames use."""
        return self.output.decode(encoding, "replace")


@dataclass(frozen=True)
class SysInfo:
    mem_kb: int
    mem_free_kb: int
    #: GetTickCount, so it wraps after 49.7 days. Raw, as the guest sent it.
    uptime_ms: int


def codepage(number: str, fallback: str = "latin-1") -> str:
    """A Python codec name for one of HELLO's codepage numbers.

    Falls back rather than raising: a machine reporting a codepage this
    Python has no codec for is still a machine worth talking to, and
    mangling a filename beats refusing to list a directory.
    """
    name = f"cp{number}"
    try:
        "".encode(name)
    except LookupError:
        return fallback
    return name


def parse_processes(chunks: list[Frame], encoding: str) -> list[Process]:
    """`pid ppid threads exe`, one per line, hex pids.

    The exe path is in the machine's ANSI codepage, not the OEM one that exec
    output uses -- filenames and window text are ANSI, console bytes are OEM,
    and HELLO reports both so nothing here has to guess.
    """
    out: list[Process] = []
    for chunk in chunks:
        for line in chunk.body.decode(encoding, "replace").splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 3)
            if len(parts) != 4:
                continue
            pid, ppid, threads, exe = parts
            out.append(Process(int(pid, 16), int(ppid, 16), int(threads),
                               exe))
    return out


def parse_sysinfo(frame: Frame) -> SysInfo:
    h = frame.headers
    return SysInfo(
        mem_kb=int(h.get("mem_kb", "0")),
        mem_free_kb=int(h.get("mem_free_kb", "0")),
        uptime_ms=int(h.get("uptime_ms", "0")),
    )


def parse_exec(end: Frame, chunks: list[Frame]) -> ExecResult:
    h = end.headers
    return ExecResult(
        output=b"".join(c.body for c in chunks),
        exit=int(h.get("exit", "0")),
        bytes_produced=int(h.get("bytes", "0")),
        truncated=h.get("truncated") == "1",
        timed_out=h.get("timed_out") == "1",
        via_shell=h.get("via_shell") == "1",
    )


def parse_entries(chunks: list[Frame], encoding: str) -> list[Entry]:
    """`type size mtime attrs name`, one per line.

    The name is last because it is the only field that can contain a space.
    A Windows path cannot contain a newline, which is what makes a line
    format safe here at all.
    """
    out: list[Entry] = []
    for chunk in chunks:
        for line in chunk.body.decode(encoding, "replace").splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 4)
            if len(parts) != 5:
                continue
            kind, size, mtime, attrs, name = parts
            out.append(Entry(name, kind, int(size), int(mtime),
                             int(attrs, 16)))
    return out


def parse_stat(frame: Frame) -> Stat:
    h = frame.headers
    return Stat(h.get("type", "u"), int(h.get("size", "0")),
                int(h.get("mtime", "0")), int(h.get("attrs", "0"), 16))


def parse_drives(chunks: list[Frame], encoding: str) -> list[Drive]:
    """`letter type media serial total_kb free_kb label`, label last.

    `media` is "1" (present), "0" (probed, absent) or "?" (never probed --
    a removable or a CD-ROM `DRIVES` was not asked about, BACKLOG 20).
    """
    out: list[Drive] = []
    for chunk in chunks:
        for line in chunk.body.decode(encoding, "replace").splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 6)
            if len(parts) < 6:
                continue
            letter, kind, media, serial, total, free = parts[:6]
            label = parts[6] if len(parts) > 6 else ""
            has_media = {"1": True, "0": False}.get(media)
            out.append(Drive(letter, int(kind), has_media,
                             int(serial, 16), int(total), int(free), label))
    return out


def parse_get(end: Frame, chunks: list[Frame]) -> FileBytes:
    """Chunks in the order the guest sent them, which is the order it read
    them: ChunkSource pumps contiguously and each frame says where it goes,
    so a gap would be a bug rather than something to reassemble around."""
    body = b"".join(c.body for c in chunks)
    return FileBytes(body, int(end.headers.get("crc", "0"), 16),
                     end.headers.get("truncated") == "1")


@dataclass(frozen=True)
class UiTree:
    """What `TREE` saw.

    `nodes` are the guest's own records, parsed by the same
    `bridge/wire/uirec.py` the client's tree goes through -- which is the
    point of sharing the encoder rather than writing a second one.

    `selectors` is one addressable name per node, in the SAME ORDER. That
    order is load-bearing: `:nth(k)` counts matches in the guest's
    depth-first walk order, so the ordinals are only right because nothing
    sorted or filtered the list first (bridge/wire/uisel.py).
    """

    nodes: list
    selectors: list[str]
    walk: str
    dropped: int
    too_deep: int
    self_skipped: int

    def find(self, substring: str) -> list[str]:
        """Selectors whose node text or name contains `substring`, folded
        for case. A convenience for a script, not a selector engine -- the
        guest has one of those and it is the thing that resolves."""
        want = substring.lower()
        out = []
        # strict: one selector per node is the invariant, and a mismatch
        # means the selector builder and the record list disagree -- which
        # would silently address the wrong control.
        for node, sel in zip(self.nodes, self.selectors, strict=True):
            text = ((getattr(node, "text", "") or "")
                    + " " + (getattr(node, "name", "") or ""))
            if want in text.lower():
                out.append(sel)
        return out


@dataclass(frozen=True)
class StepResult:
    """One step that ran."""

    index: int
    status: str
    ms: int
    hit: str
    detail: str
    part: int


@dataclass(frozen=True)
class StepRun:
    """What `RUN` did, whether or not it finished.

    NOT an exception on a non-ok status, deliberately, and the bridge took
    the same decision for the same reason: a list that failed at step 5 still
    ran four steps the caller has to hear about. `ok` is the thing to branch
    on; `steps` is what actually happened.
    """

    status: str
    done: int
    failed_at: int
    detail: str
    released: bool
    steps: list[StepResult]

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_run(end: Frame, chunks: list[Frame], encoding: str) -> StepRun:
    """The terminator's headers plus the per-step body.

    The body goes through the bridge's own `steps.parse_reply`, because the
    escaping is the guest's and there should be one reader of it -- a second
    one is how the two spellings drift.
    """
    rows, released = _steps.parse_reply(b"".join(c.body for c in chunks))
    steps = [
        StepResult(
            index=int(r.get("i", b"-1")),
            status=r.get("status", b"").decode("ascii", "replace"),
            ms=int(r.get("ms", b"0")),
            hit=r.get("hit", b"").decode(encoding, "replace"),
            detail=r.get("detail", b"").decode(encoding, "replace"),
            part=int(r.get("part", b"-1")),
        )
        for r in rows
    ]
    h = end.headers
    return StepRun(
        status="ok" if end.type == "OK" else h.get("code", "failed"),
        done=int(h.get("done", "0")),
        failed_at=int(h.get("step", "-1")),
        detail=h.get("detail", ""),
        released=released,
        steps=steps,
    )


def parse_tree(end: Frame, chunks: list[Frame], encoding: str) -> UiTree:
    nodes = uirec.parse_records(b"".join(c.body for c in chunks), encoding)
    # In place, on `nodes`, and the order it is given them is the order the
    # guest walked -- `:nth(k)` counts matches in that order, so nothing may
    # sort or filter the list before this.
    uisel.build_selectors(nodes)
    h = end.headers
    return UiTree(
        nodes=nodes,
        selectors=[n.sel for n in nodes],
        walk=h.get("walk", ""),
        dropped=int(h.get("dropped", "0")),
        too_deep=int(h.get("too_deep", "0")),
        self_skipped=int(h.get("self", "0")),
    )
