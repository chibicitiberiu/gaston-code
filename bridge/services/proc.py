"""Processes on the connected machine: list them, end one.

The guest answers PSLIST and PSKILL on its reader thread (libexec's
`Processes`, `win/rpc/rpc.cpp`), because a hung program is found and ended
while an EXEC may still be stuck in the job slot. This side is a table of
futures keyed by `p`-prefixed ids, the same shape as the ui service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from ..guest import GuestSession
from ..wire import fold
from ..wire.protocol import ps_kill, ps_launch, ps_list

log = logging.getLogger("bridge.proc")

#: The guest answers in milliseconds; a kill waits at most 250ms there.
BUDGET_S = 15.0


@dataclass
class Proc:
    pid: str
    ppid: str
    threads: int
    exe: str


class ProcError(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class ProcService:
    def __init__(self, connection) -> None:
        self.connection = connection
        #: The memory figures the last listing carried, per guest.
        self._last_memory: dict[int, dict[str, int]] = {}

    async def _call(self, guest: GuestSession, frame) -> tuple[dict, bytes]:
        guest.proc_seq += 1
        ps_id = f"p{guest.proc_seq}"
        frame.headers["id"] = ps_id
        fut = asyncio.get_running_loop().create_future()
        guest.proc_pending[ps_id] = fut
        try:
            await self.connection.send(guest, frame)
            return await asyncio.wait_for(fut, timeout=BUDGET_S)
        except TimeoutError as exc:
            raise ProcError("timeout") from exc
        finally:
            guest.proc_pending.pop(ps_id, None)

    async def list(self, guest: GuestSession) -> tuple[list[Proc], bool]:
        """(processes, truncated). Raises ProcError on a refusal."""
        headers, body = await self._call(guest, ps_list(""))
        status = headers.get("status", "")
        if status != "ok":
            raise ProcError(status or "no_status")
        mem = {}
        for k in ("mem_kb", "mem_free_kb", "swap_kb", "swap_free_kb",
                  "mem_load"):
            with contextlib.suppress(KeyError, ValueError):
                mem[k] = int(headers[k])
        self._last_memory[id(guest)] = mem
        text = fold.decode(body, _acp(guest))
        out: list[Proc] = []
        for line in text.splitlines():
            parts = line.split(" ", 3)
            if len(parts) < 4:
                continue
            try:
                threads = int(parts[2])
            except ValueError:
                threads = 0
            out.append(Proc(parts[0], parts[1], threads, parts[3]))
        return out, headers.get("truncated") == "1"

    async def memory(self, guest: GuestSession) -> dict[str, int]:
        """Live memory, from the PSRESULT a listing just carried: mem_kb,
        mem_free_kb, swap_kb, swap_free_kb, mem_load. Empty for a client too
        old to send it."""
        return dict(self._last_memory.get(id(guest), {}))

    async def launch(self, guest: GuestSession, command: str, *,
                     cwd: str = "", show: str = "") -> dict:
        """Start a program, do not wait. The guest's headers: status (ok,
        not_found, bad_cwd, too_long, failed, bad_command), pid, error."""
        headers, _ = await self._call(guest, ps_launch("", command, cwd=cwd,
                                                       show=show))
        return headers

    async def kill(self, guest: GuestSession, pid: str, *,
                   force: bool = False) -> str:
        """The guest's status word: ok, pending, denied, no_such_pid,
        failed, no_toolhelp."""
        headers, _ = await self._call(guest, ps_kill("", pid, force=force))
        return headers.get("status", "") or "no_status"

    async def proc_frame(self, guest: GuestSession, f) -> None:
        """PSRESULT / PSKILLRESULT."""
        fut = guest.proc_pending.get(f.headers.get("id", ""))
        if fut is None:
            log.debug("%s for %s, which nobody is waiting for", f.type,
                      f.headers.get("id", ""))
            return
        if not fut.done():
            fut.set_result((dict(f.headers), bytes(f.body or b"")))

    def fail_calls(self, guest: GuestSession | None) -> None:
        if guest is None:
            return
        for ps_id, fut in list(guest.proc_pending.items()):
            if not fut.done():
                fut.set_exception(ProcError("disconnected"))
            guest.proc_pending.pop(ps_id, None)


def _acp(guest: GuestSession) -> str:
    """Exe paths are ANSI, like window text."""
    caps = getattr(guest, "caps", None)
    return getattr(caps, "acp", "") or ""
