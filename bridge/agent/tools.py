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


def build(bridge, guest=None) -> dict:
    """Create the `machine` server bound to one bridge AND ONE GUEST.

    The guest matters. These closures used to capture only the Bridge and let
    each tool resolve `bridge._guest` when it ran -- so the tool addressed
    "whichever machine is attached right now" rather than the machine whose
    conversation this is. With one client those are the same thing. With two
    they are not, and the failure is the worst shape available: the command
    runs on the wrong machine, and the permission prompt asking for it appears
    on the right person's screen, so it looks approved because it WAS approved
    -- for something else.

    Passing it now, while there is one client, is what makes that impossible
    later rather than something to remember during the multi-client work.

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
        "through the system shell, whichever that machine has. Output is "
        "captured as the console codepage and capped at 32KB. The user must "
        "approve every call on their own screen, so prefer one command that "
        "answers the question over several that explore.\n\n"
        # NO SHELL SYNTAX HERE, deliberately, and it was here for a while.
        #
        # A tool description is unconditional: every machine gets it. The
        # COMMAND.COM rules that used to sit in this paragraph are wrong on
        # a 4DOS box, where `&&` works, and wrong on the NT 4 and Windows
        # 3.1 targets CLAUDE.md's first standing rule says this surface has
        # to survive. They moved to `toolnotes.py` and are now selected by
        # what the machine reported having -- which is the cheapest
        # available proof the design is portable rather than a Win98 special
        # case, since pointing this at NT 4 changes the text with no bridge
        # change at all.
        #
        # One command per call is NOT a shell fact and stays:
        "ONE COMMAND PER CALL. Whether commands can be chained, and with "
        "what, depends on the shell that machine has -- the system prompt "
        "says which one it is and what it accepts. To run several steps "
        "regardless of shell, write a script file into the mirrored folder "
        "and run that; it is also one approval instead of several.\n\n"
        # The provenance names the machine it was measured on; the RULE is
        # stated generally, because "a program that prompts reads the
        # console directly and cannot be fed from here" is a fact about the
        # transport, not about any one shell.
        "IMPORTANT: the command must be non-interactive. Anything that stops "
        "to ask a question -- a delete or a format asking 'Are you sure "
        "(Y/N)?' -- will HANG until the timeout kills it: that prompt is "
        "read from the machine's own console and there is no way to answer "
        "it from here -- NOT by piping one in either, which was measured and "
        "does not work. Use a form that does not ask: a switch that says "
        "yes, or a more specific target.",
        # A FULL JSON SCHEMA, not the `{name: Annotated[...]}` shorthand.
        #
        # The shorthand marks EVERY property required -- `"required":
        # list(properties.keys())` in the SDK's schema builder -- so three
        # arguments documented as optional were not. Measured on the guest:
        # the model called this with a command and no `cwd`, got back
        # `Input validation error: 'cwd' is a required property`, and had to
        # try again. Every exec cost two round trips and the user watched a
        # red error go by for a tool call that was correct.
        #
        # The SDK uses a schema verbatim when it already looks like one (a
        # `type` and a `properties`), which is the escape hatch this takes.
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description":
                        # No OS named here, for the same reason the main
                        # description carries no shell syntax: this schema
                        # ships to every machine. What that machine's paths
                        # and shell look like is detected and stated in the
                        # system prompt.
                        "The command line to run, written for the target "
                        "machine's own shell and path conventions -- the "
                        "system prompt says what those are.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description":
                        f"How long to allow, in milliseconds. Default "
                        f"{DEFAULT_TIMEOUT_MS}, clamped to {MAX_TIMEOUT_MS}. "
                        f"The machine is slow; a compile may genuinely need "
                        f"minutes.",
                },
                "shell": {
                    "type": "boolean",
                    "description":
                        "Run through the shell (default true). Set false to "
                        "spawn an executable directly, which gives cleaner "
                        "quoting but loses built-ins like dir, vol, copy and "
                        "type, and loses pipes and redirection.",
                },
                "cwd": {
                    "type": "string",
                    "description":
                        "Working directory on the connected machine, e.g. "
                        "C:\\WORK. Optional -- leave it out and the command "
                        "runs in this conversation's own folder on that "
                        "machine, which is where your files arrive.",
                },
            },
            "required": ["command"],
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
        # Defaulted to THIS conversation's folder on the user's machine, not
        # left empty. An empty cwd means the guest passes NULL to
        # CreateProcess and the child inherits the GUI process's directory --
        # a value the bridge has never been told and cannot derive, which is
        # also why bridge/agent/cmdguard.py has to answer ASK for every
        # relative path when it is unset.
        cwd = (args or {}).get("cwd") or bridge.guest_work_root(guest)

        return await bridge.run_on_guest(command, timeout_ms=timeout_ms,
                                         shell=shell, cwd=cwd, guest=guest)

    @tool(
        "sync_status",
        "Report the state of file synchronisation with the connected machine: "
        "which of your files could not be copied there and why, which are in "
        "conflict, and which are currently refusing your writes.\n\n"
        "Read this when a file you wrote does not seem to have arrived, before "
        "concluding that it did. Writing a file always succeeds here; whether "
        "it reached the user's machine is a separate question and this is the "
        "only way to answer it. Read-only and cheap to call.",
        {},
    )
    async def machine_sync_status(args: dict) -> dict:
        return _text(bridge.sync_status_text(guest))

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
                "having merged; accept-revert -- the user really did undo "
                "your change, so take their version and discard yours; "
                "accept-tree -- answers a whole-pass refusal, takes no "
                "path; forget -- stop tracking a path that is gone from both "
                "sides and should not come back.",
            ],
        },
    )
    async def machine_sync_resolve(args: dict) -> dict:
        path = ((args or {}).get("path") or "").strip()
        action = ((args or {}).get("action") or "release").strip().lower()
        if action not in ("release", "accept-revert", "accept-tree", "forget"):
            return _text(f"Unknown action {action!r}; use release, "
                         f"accept-revert, accept-tree or forget.", error=True)
        # Checked AFTER the action, because one of them is tree-level and has
        # no path to give. Checking first made "no path" the answer to a call
        # that was correctly formed.
        if not path and action != "accept-tree":
            return _text("No path was given.", error=True)
        return _text(await bridge.sync_resolve(path, action, guest))

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
