"""GuestExecService: command execution on the connected guest machine.

Manages running commands via EXEC frames, streaming output via EXECOUT frames,
resolving execution results via EXECRESULT, killing commands on timeout/cancel,
and failing pending commands on link drops or session switches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..agent import toolrules
from ..guest import ExecCall, GuestSession
from ..status import EXEC_STATUS_PROSE, ExecStatus
from ..wire import fold
from ..wire.protocol import MAX_HEADER_VALUE, Frame, _quote, exec_cmd, exec_kill

log = logging.getLogger("bridge.guest_exec")

EXEC_OUT_MAX = 64 * 1024
EXEC_MAX_CMD = 1024


class GuestExecService:
    """Manages command execution lifecycle on the guest machine."""

    EXEC_OUT_MAX = EXEC_OUT_MAX
    EXEC_MAX_CMD = EXEC_MAX_CMD

    def __init__(
        self,
        connection: Any,
        *,
        spawn: Any = None,
        pre_exec: Any = None,
    ) -> None:
        self.connection = connection
        self.spawn = spawn or asyncio.create_task
        self.pre_exec = pre_exec

    async def run_on_guest(
        self,
        command: str,
        *,
        timeout_ms: int,
        shell: str,
        cwd: str,
        guest: GuestSession | None = None,
    ) -> dict:
        """Body of mcp__machine__exec. Returns an MCP tool result."""
        if guest is None:
            guest = getattr(self.connection, "guest", None) or getattr(
                self.connection, "_guest", None
            )

        if guest is not None and self.pre_exec is not None:
            await self.pre_exec(guest, command)

        if guest is None or not guest.link.connected:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "The connected machine is not reachable right now -- it is "
                            "not connected to this proxy. Nothing was run."
                        ),
                    }
                ],
                "isError": True,
            }

        prefix = ""
        want = (shell or "").strip()
        if want.lower() != "none":
            usable = toolrules.usable_shells(guest.tools)
            if want:
                prefix = toolrules.shell_command(guest.tools, want) or ""
                if not prefix:
                    have = ", ".join(f.name for f in usable)
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"There is no shell called {want!r} that this bridge "
                                    f"can run commands through on that machine. "
                                    + (
                                        f"It has: {have}."
                                        if have
                                        else (
                                            "It reported no shell this bridge knows how to "
                                            "drive."
                                        )
                                    )
                                    + " Nothing was run."
                                ),
                            }
                        ],
                        "isError": True,
                    }
            elif usable:
                prefix = (
                    toolrules.shell_command(guest.tools, usable[0].name) or ""
                )

        async with guest.exec_lock:
            if not guest.link.connected:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "The connected machine went away while this command "
                                "was waiting to run. Nothing was run."
                            ),
                        }
                    ],
                    "isError": True,
                }
            guest.exec_seq += 1
            exec_id = f"x{guest.exec_seq}"
            fut = asyncio.get_running_loop().create_future()
            guest.exec_pending[exec_id] = ExecCall(future=fut)

            for label, value in (("command", command), ("cwd", cwd)):
                bad = fold.unrepresentable(value)
                if bad is None:
                    continue
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"That {label} contains {bad!r} (U+{ord(bad):04X}), which "
                                f"the machine's codepage cannot represent. Nothing was "
                                f"run. Rewrite it using plain ASCII -- do not assume a "
                                f"lookalike character will be understood, because the "
                                f"machine will read whatever byte it is given."
                            ),
                        }
                    ],
                    "isError": True,
                }

            if cwd:
                wire_len = len(_quote(cwd))
                if wire_len > MAX_HEADER_VALUE:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"That working directory is too long to send to "
                                    f"the machine ({wire_len} bytes on the wire; the limit "
                                    f"is {MAX_HEADER_VALUE}). Nothing was run. Use a "
                                    f"shorter path, or cd to it inside the command."
                                ),
                            }
                        ],
                        "isError": True,
                    }

            await self.connection.send(
                guest,
                exec_cmd(
                    exec_id,
                    command,
                    timeout_ms=timeout_ms,
                    shell=prefix,
                    cwd=cwd,
                ),
            )
            log.info("exec %s: %s", exec_id, command[:160])

            budget = (timeout_ms / 1000.0) + 30.0
            try:
                return await asyncio.wait_for(fut, timeout=budget)
            except TimeoutError:
                self.kill_exec(guest, exec_id)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"No reply from the connected machine after "
                                f"{budget:.0f}s; it has been asked to stop the "
                                f"command."
                            ),
                        }
                    ],
                    "isError": True,
                }
            except asyncio.CancelledError:
                self.kill_exec(guest, exec_id)
                raise
            finally:
                guest.exec_pending.pop(exec_id, None)

    def kill_exec(self, guest: GuestSession, exec_id: str) -> None:
        """Ask the guest to stop a command we are no longer waiting for."""
        entry = guest.exec_pending.get(exec_id)
        if entry is not None:
            if entry.killed:
                return
            entry.killed = True
        log.info("asking the guest to kill exec %s", exec_id)
        self.spawn(self.connection.send(guest, exec_kill(exec_id)))

    async def exec_out(self, guest: GuestSession, f: Frame) -> None:
        """Process EXECOUT frame chunk."""
        entry = guest.exec_pending.get(f.headers.get("id", ""))
        if entry is None:
            return
        room = self.EXEC_OUT_MAX - len(entry.out)
        if room <= 0:
            entry.capped = True
            return
        if len(f.body) > room:
            entry.out.extend(f.body[:room])
            entry.capped = True
            return
        entry.out.extend(f.body)

    async def exec_result(self, guest: GuestSession, f: Frame) -> None:
        """Process EXECRESULT frame and resolve outstanding future."""
        exec_id = f.headers.get("id", "")
        entry = guest.exec_pending.get(exec_id)
        if entry is None:
            log.debug(
                "EXECRESULT for %s, which nobody is waiting for "
                "(usually a kill that has already been handled)",
                exec_id,
            )
            return
        fut = entry.future
        if fut.done():
            return

        status = ExecStatus.parse(f.headers.get("status", "ok"))
        code = f.headers.get("code", "")
        truncated = f.headers.get("truncated", "0") == "1" or entry.capped

        text = fold.decode_oem(bytes(entry.out))

        parts = []
        prose = EXEC_STATUS_PROSE.get(status)
        if prose:
            parts.append(prose.format(max_cmd=self.EXEC_MAX_CMD))
        if code not in ("", "0") and status == ExecStatus.OK:
            parts.append(f"Exit code {code}.")
        if truncated:
            if entry.capped:
                parts.append(
                    f"Output was capped at "
                    f"{self.EXEC_OUT_MAX // 1024}KB by this bridge "
                    f"and truncated."
                )
            else:
                parts.append(
                    "Output was capped at 32KB by the connected "
                    "machine and truncated."
                )

        body = text if text.strip() else "(no output)"
        head = (" ".join(parts) + "\n\n") if parts else ""
        fut.set_result(
            {
                "content": [{"type": "text", "text": head + body}],
                "isError": status.is_error,
            }
        )
        log.info(
            "exec %s finished: status=%s code=%s bytes=%d",
            exec_id,
            status.value,
            code,
            len(entry.out),
        )

    def fail_execs(self, guest: GuestSession | None, why: str) -> None:
        """Resolve every in-flight EXEC, synchronously."""
        if guest is None:
            return
        for _exec_id, entry in list(guest.exec_pending.items()):
            fut = entry.future
            if not fut.done():
                fut.set_result(
                    {"content": [{"type": "text", "text": why}], "isError": True}
                )
        guest.exec_pending.clear()

