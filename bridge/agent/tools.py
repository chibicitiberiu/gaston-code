"""The in-process MCP server that reaches the connected machine.

One tool so far: `exec`. Claude sees it as `mcp__machine__exec`, and the prefix
comes from the KEY in `mcp_servers`, not from the `name=` argument here -- the
CLI keys its config by the dict key (`client.py:160-162`), so the two must
match or the tool is addressed by a name nothing answers to.

Nothing here is called `win98_*`. Only the guest binary is OS-specific; the
tool surface should survive being pointed at MS-DOS or NT 4, and where the OS
genuinely matters it is a runtime branch on the guest, not a name.

Every call routes through the permission bar. MCP permission rules cannot carry
specifiers -- the CLI rejects "patterns in parentheses" -- so the only rule
expressible for this tool is the WHOLE TOOL, which is why the guest never
offers "Always" for it. One press would be unattended command execution on
someone's computer for the rest of the session.
"""

from __future__ import annotations

import logging
from typing import Annotated

from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger("bridge.tools")

# The guest clamps this too; a tool argument is a suggestion, not a promise.
DEFAULT_TIMEOUT_MS = 20_000
MAX_TIMEOUT_MS = 120_000


def _text(body: str, *, error: bool = False) -> dict:
    out: dict = {"content": [{"type": "text", "text": body}]}
    if error:
        out["isError"] = True
    return out


def build(bridge) -> dict:
    """Create the `machine` server bound to one bridge.

    Returns the value for `mcp_servers`, so the caller writes
    `mcp_servers=tools.build(self)` and the dict key is fixed here rather than
    being a second place to get the prefix wrong.
    """

    @tool(
        "exec",
        # This description is the model's only guide to a machine it cannot
        # see, so it states the era rather than assuming it.
        "Run a command on the connected machine (the old computer the user is "
        "sitting at), and return its output. By default the command runs "
        "through the system shell, so shell built-ins, pipes and redirection "
        "work -- on Windows 9x that is COMMAND.COM, which is 16-bit. Output is "
        "captured as the console codepage and capped at 32KB. The user must "
        "approve every call on their own screen, so prefer one command that "
        "answers the question over several that explore.\n\n"
        "IMPORTANT: the command must be non-interactive. Anything that stops "
        "to ask a question -- `del *.*` and `format` ask 'Are you sure "
        "(Y/N)?' -- will HANG until the timeout kills it, because on Windows "
        "9x those prompts are read from the console device and cannot be fed "
        "from here. Measured, not assumed. Pipe an answer in instead "
        "(`echo y | del c:\\tmp\\*.*`), or use a form that does not ask.",
        {
            "command": Annotated[
                str,
                "The command line to run, written for the target machine's "
                "shell (DOS conventions on Windows 9x: backslashes, 8.3 names "
                "where long ones are not supported).",
            ],
            "timeout_ms": Annotated[
                int,
                f"How long to allow, in milliseconds. Default "
                f"{DEFAULT_TIMEOUT_MS}, clamped to {MAX_TIMEOUT_MS}. The "
                f"machine is slow; a compile may genuinely need minutes.",
            ],
            "shell": Annotated[
                bool,
                "Run through the shell (default true). Set false to spawn an "
                "executable directly, which gives cleaner quoting but loses "
                "built-ins like dir, vol, copy and type, and loses pipes and "
                "redirection.",
            ],
            "cwd": Annotated[
                str,
                "Working directory on the connected machine, e.g. C:\\WORK. "
                "Optional.",
            ],
        },
    )
    async def machine_exec(args: dict) -> dict:
        command = (args or {}).get("command") or ""
        if not command.strip():
            return _text("No command was given.", error=True)

        try:
            timeout_ms = int(args.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        except (TypeError, ValueError):
            timeout_ms = DEFAULT_TIMEOUT_MS
        timeout_ms = max(1000, min(timeout_ms, MAX_TIMEOUT_MS))

        shell = args.get("shell")
        shell = True if shell is None else bool(shell)
        cwd = args.get("cwd") or ""

        return await bridge.run_on_guest(command, timeout_ms=timeout_ms,
                                         shell=shell, cwd=cwd)

    @tool(
        "sync_status",
        "Report the state of file synchronisation with the connected machine: "
        "which of your files could not be copied there and why, which are in "
        "conflict, and which are currently refusing your writes.\n\n"
        "Read this when a file you wrote does not seem to have arrived, before "
        "concluding that it did. Writing a file always succeeds here; whether "
        "it reached the user's machine is a separate question and this is the "
        "only way to answer it. Read-only and free to call.",
        {},
    )
    async def machine_sync_status(args: dict) -> dict:
        return _text(bridge.sync_status_text())

    @tool(
        "sync_resolve",
        "Resolve one halted synchronisation decision, after the user has "
        "confirmed they are finished with the file.\n\n"
        "When you and the user both change the same file, their copy wins and "
        "yours is moved aside -- and the path then REFUSES your writes, "
        "mechanically, until this is called. That is deliberate: you cannot "
        "usefully react to a conflict during the turn in which it happened, "
        "so the bridge stops you rather than asking you to stop.\n\n"
        "Do not call this to make an error go away. Call it once you have read "
        "the preserved copy, merged what you need, and the user has said they "
        "have finished with the file. The user approves every call on their "
        "own screen.",
        {
            "path": Annotated[
                str,
                "The path, relative to the mirrored folder, exactly as "
                "sync_status reported it.",
            ],
            "action": Annotated[
                str,
                "release (default) -- lift the write refusal on this path, "
                "having merged; forget -- stop tracking a path that is gone "
                "from both sides and should not come back.",
            ],
        },
    )
    async def machine_sync_resolve(args: dict) -> dict:
        path = ((args or {}).get("path") or "").strip()
        if not path:
            return _text("No path was given.", error=True)
        action = ((args or {}).get("action") or "release").strip().lower()
        if action not in ("release", "forget"):
            return _text(f"Unknown action {action!r}; use release or forget.",
                         error=True)
        return _text(await bridge.sync_resolve(path, action))

    server = create_sdk_mcp_server(name="machine", version="0.1.0",
                                   tools=[machine_exec, machine_sync_status,
                                          machine_sync_resolve])
    # THE OUTERMOST OF THREE TIMEOUTS, and it has to be the largest.
    #
    # Without it the CLI uses MCP_TOOL_TIMEOUT, which defaults to 30s -- below
    # both the guest's 120s ceiling and the bridge's own budget. The CLI would
    # abandon the call, and an SDK server's tool is NOT cancelled when that
    # happens (types.py:653-662): the Python body runs on, the guest keeps
    # running the child, exec_lock stays held blocking the next call, and the
    # result eventually resolves a future nobody is reading.
    #
    # Ordering: guest clamp (<=120s) < bridge budget (clamp + 30s) < this.
    return {"machine": {**server, "timeout": 300_000}}
