"""What the machine says, as types.

The guest transforms nothing (CLAUDE.md): it makes syscalls and moves bytes,
and records leave it as flat lines. Turning them into something with named
fields happens here, on the side that has a real language to do it in.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

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
    #: False for a floppy or CD bay with nothing in it. NOT derivable from
    #: free bytes: a mounted CD has none by definition, and inferring
    #: emptiness that way reported every disc as an empty drive.
    has_media: bool
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
    """`letter type media serial total_kb free_kb label`, label last."""
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
            out.append(Drive(letter, int(kind), media == "1",
                             int(serial, 16), int(total), int(free), label))
    return out


def parse_get(end: Frame, chunks: list[Frame]) -> FileBytes:
    """Chunks in the order the guest sent them, which is the order it read
    them: ChunkSource pumps contiguously and each frame says where it goes,
    so a gap would be a bug rather than something to reassemble around."""
    body = b"".join(c.body for c in chunks)
    return FileBytes(body, int(end.headers.get("crc", "0"), 16),
                     end.headers.get("truncated") == "1")
