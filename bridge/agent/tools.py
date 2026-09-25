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

from . import prompts

log = logging.getLogger("bridge.tools")

# The guest clamps this too; a tool argument is a suggestion, not a promise.
DEFAULT_TIMEOUT_MS = 20_000
MAX_TIMEOUT_MS = 120_000


def _text(body: str, *, error: bool = False) -> dict:
    out: dict = {"content": [{"type": "text", "text": body}]}
    if error:
        out["isError"] = True
    return out


def build(host, guest=None) -> dict:
    """Create the `machine` server bound to one HOST and ONE GUEST.

    `host` supplies the three services the tools act through -- `guest_exec`,
    `sync` and `workspace`. It used to be the Bridge itself, which worked only
    because the Bridge forwarded everything; when the services stopped being
    reached through it, this closure was left calling methods the object it
    had been handed did not have, and the only symptom was the model telling
    the user it could not reach their machine.

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
        prompts.text("tools.exec.description"),
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
                        prompts.text("tools.exec.args.command"),
                },
                "timeout_ms": {
                    "type": "integer",
                    "description":
                        prompts.text("tools.exec.args.timeout_ms",
                              default_timeout_ms=DEFAULT_TIMEOUT_MS,
                              max_timeout_ms=MAX_TIMEOUT_MS),
                },
                "shell": {
                    # A NAME, not a boolean, because a machine has as many
                    # shells as it has: `command.com` and `4dos` on a DOS
                    # box, `sh`/`bash`/`zsh`/`csh` on a Unix one. A boolean
                    # could only mean "the one somebody else picked", and
                    # the somebody was the guest, deciding from
                    # `GetVersion()` and telling nobody -- so a second
                    # installed shell was unreachable and the command guard
                    # on this side was judging by a grammar the other side
                    # might not have been running.
                    #
                    # The options are not listed HERE: this description
                    # ships to every machine, and which shells exist is a
                    # fact about one. The system prompt names them from what
                    # that machine reported.
                    "type": "string",
                    "description":
                        prompts.text("tools.exec.args.shell"),
                },
                "cwd": {
                    "type": "string",
                    "description":
                        prompts.text("tools.exec.args.cwd"),
                },
            },
            "required": ["command"],
        },
    )
    async def machine_exec(args: dict) -> dict:

        # LOGGED HERE, because this is the outermost frame on our side of
        # the MCP boundary. Above it the SDK turns an exception into a tool
        # result the MODEL reads, and nothing reaches the bridge log -- so a
        # bug in this path is visible only as the model reporting "a
        # connection problem" on a 640x480 screen. Same shape as 02.13.
        try:
            command = (args or {}).get("command") or ""
            if not command.strip():
                return _text("No command was given.", error=True)

            try:
                timeout_ms = int(args.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
            except (TypeError, ValueError):
                timeout_ms = DEFAULT_TIMEOUT_MS
            timeout_ms = max(1000, min(timeout_ms, MAX_TIMEOUT_MS))

            # PASSED ON AS WRITTEN, resolved in run_on_guest. A name this
            # machine does not have has to come back as an error naming what it
            # does have, and that list lives with the guest, not here.
            #
            # A bool is still understood, because the schema said boolean for
            # long enough that a model may have learned it: True means "the
            # machine's first shell" and False means "none", which is what those
            # values meant when they were the whole vocabulary.
            shell = args.get("shell")
            if isinstance(shell, bool):
                shell = "" if shell else "none"
            shell = str(shell or "").strip()
            # Defaulted to THIS conversation's folder on the user's machine, not
            # left empty. An empty cwd means the guest passes NULL to
            # CreateProcess and the child inherits the GUI process's directory --
            # a value the bridge has never been told and cannot derive, which is
            # also why bridge/agent/cmdguard.py has to answer ASK for every
            # relative path when it is unset.
            cwd = (args or {}).get("cwd") or host.workspace.guest_work_root(guest)

            # LOGGED HERE, because this is the outermost frame on our side of
            # the MCP boundary. Above it the SDK turns an exception into a tool
            # result the MODEL reads, and nothing reaches the bridge's own log --
            # so a bug in this path is visible only as the model reporting "a
            # connection problem" on a 640x480 screen. Same shape as 02.13.
            return await host.guest_exec.run_on_guest(
                command, timeout_ms=timeout_ms, shell=shell, cwd=cwd,
                guest=guest)

        except Exception:
            log.exception("mcp__machine__exec failed: %r",
                          str((args or {}).get("command"))[:80])
            raise

    @tool(
        "sync_status",
        prompts.text("tools.sync_status.description"),
        {},
    )
    async def machine_sync_status(args: dict) -> dict:
        return _text(host.sync.sync_status_text(guest))

    @tool(
        "sync_resolve",
        prompts.text("tools.sync_resolve.description"),
        {
            "path": Annotated[
                str,
                "The path, relative to the mirrored folder, exactly as "
                "sync_status reported it.",
            ],
            "action": Annotated[
                str,
                prompts.text("tools.sync_resolve.args.action"),
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
        return _text(await host.sync.sync_resolve(path, action, guest))

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
