"""What the machine says, as types.

The guest transforms nothing (CLAUDE.md): it makes syscalls and moves bytes,
and records leave it as flat lines. Turning them into something with named
fields happens here, on the side that has a real language to do it in.
"""

from __future__ import annotations

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
