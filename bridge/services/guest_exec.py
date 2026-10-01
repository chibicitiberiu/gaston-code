"""GuestExecService: command execution on the connected guest machine.

Manages running commands via EXEC frames, streaming output via EXECOUT frames,
resolving execution results via EXECRESULT, killing commands on timeout/cancel,
and failing pending commands on link drops or session switches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..agent import prompts, toolrules
from ..guest import ExecCall, GuestSession
from ..status import EXEC_STATUS_PROSE, ExecStatus
from ..wire import fold
from ..wire.protocol import MAX_HEADER_VALUE, Frame, _quote, exec_cmd, exec_kill
from . import execwrap
from .guest_fs import FsError

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
        fs: Any = None,
    ) -> None:
        self.connection = connection
        self.spawn = spawn or asyncio.create_task
        self.pre_exec = pre_exec
        #: The fs lane, for writing the exit-code wrapper. Optional: without it
        #: everything still runs and the exit code is simply not recovered,
        #: which is the behaviour this service had until 2026-10-02.
        self.fs = fs

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
                "is_error": True,
                "isError": True,
            }

        prefix = ""
        no_shell = False
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
                        "is_error": True,
                "isError": True,
                    }
            elif usable:
                prefix = (
                    toolrules.shell_command(guest.tools, usable[0].name) or ""
                )
            else:
                no_shell = True

        # THE EXIT-CODE WRAPPER. Outside the lock, because it is a separate
        # fs-lane round trip and holding the exec lock across it would make
        # every command wait for another command's wrapper write.
        #
        # Everything about it is optional and each absence costs only the exit
        # code: no shell (the code already survives a direct spawn), no fs
        # lane, no `temp` header from a client too old to send one, a builtin
        # whose code does not exist on this shell, or a write that failed.
        wrapped = False
        run_command = command
        shell_name = ""
        if prefix:
            shell_name = want or (toolrules.usable_shells(guest.tools)[0].name
                                  if toolrules.usable_shells(guest.tools)
                                  else "")
        temp_dir = (guest.hello or {}).get("temp", "")
        if (shell_name and temp_dir and self.fs is not None
                and execwrap.applies(shell_name, command)):
            dest = execwrap.path(shell_name, temp_dir)
            try:
                await self.fs.put(guest, dest,
                                  execwrap.body(shell_name, command))
                run_command = dest
                wrapped = True
            except (FsError, TimeoutError, OSError) as exc:
                # Not fatal and not silent. The command still runs, exactly as
                # it did before this existed; what is lost is the ability to
                # say whether it worked.
                log.info("no exit code for this command: could not write %s "
                         "(%s)", dest, exc)

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
                    "is_error": True,
                "isError": True,
                }
            guest.exec_seq += 1
            exec_id = f"x{guest.exec_seq}"
            fut = asyncio.get_running_loop().create_future()
            guest.exec_pending[exec_id] = ExecCall(future=fut,
                                                   no_shell=no_shell,
                                                   wrapped=wrapped)

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
                    "is_error": True,
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
                        "is_error": True,
                "isError": True,
                    }

            await self.connection.send(
                guest,
                exec_cmd(
                    exec_id,
                    run_command,
                    timeout_ms=timeout_ms,
                    shell=prefix,
                    cwd=cwd,
                ),
            )
            # BOTH, when they differ. The user approved `command` and the guard
            # classified it; `run_command` is the wrapper holding that same text
            # byte for byte. A log that showed only one of them would make the
            # other impossible to account for afterwards.
            if wrapped:
                log.info("exec %s: %s (via %s)", exec_id, command[:160],
                         run_command)
            else:
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
                    "is_error": True,
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

        # THE WRAPPER'S VERDICT, pulled off before anything reads the output.
        # Empty means the line never arrived -- the command killed the shell,
        # or the output cap bit before it -- and then nothing is said about the
        # exit code, which is the honest answer rather than a guess.
        verdict = ""
        if entry.wrapped:
            text, verdict = execwrap.split(text)

        parts = []
        prose = EXEC_STATUS_PROSE.get(status)
        if prose:
            parts.append(prose.format(max_cmd=self.EXEC_MAX_CMD))
        if entry.no_shell and status == ExecStatus.SPAWN_FAILED:
            parts.append(prompts.text("tools.exec.notes.no_shell").strip())
        # Through COMMAND.COM /c on Windows 98 `code` is ALWAYS 0, even for a
        # program that does not exist (measured: win/checks/execprobe.cpp,
        # docs/measurements/execprobe-vm-gui-20260926.txt); a program spawned
        # with no shell reports its real code. So a zero here proves nothing
        # on that shell, which is why only a non-zero code is ever said.
        # docs/BACKLOG.md 14.
        #
        # THE WRAPPER CLOSES THAT, for a shell that loses the code and a
        # command that is not a builtin: `if errorlevel` inside a batch file
        # does see a program's real code, so a "fail" here is the program's own
        # answer and an "ok" is too. Zero versus non-zero only -- the exact
        # value was measurable and was dropped by decision, see
        # bridge/services/execwrap.py. It takes precedence over `code`, which
        # on this shell is the 0 that proves nothing.
        if verdict == "fail" and status == ExecStatus.OK:
            parts.append(prompts.text("tools.exec.notes.failed").strip())
        elif verdict == "ok" and status == ExecStatus.OK:
            pass            # success needs no sentence; the output is the answer
        elif code not in ("", "0") and status == ExecStatus.OK:
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
                "is_error": status.is_error,
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

