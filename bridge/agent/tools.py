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

import asyncio
import base64
import hashlib
import logging
import mimetypes
import ntpath
import os
import pathlib
import re

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..files import caps as C
from ..files import paths as P
from ..services.guest_fs import FsError
from ..services.proc import ProcError
from ..services.ui import UiError
from ..wire import fold
from ..wire.uiview import render_tree, to_png
from . import prompts
from . import toolnotes as notes
from .selectors import render_candidates, split_steps
from .steps import STEP_TIMEOUT_DEFAULT, StepError, compile_steps, parse_reply

log = logging.getLogger("bridge.tools")

#: How much text `get_file` will hand back INLINE. Above it, and for anything
#: that is not text, the bytes go to a file on this proxy and the reply says
#: only where -- `Read` and `Grep` are better at the rest than a second
#: pagination surface inside this tool would be, and the agent already has
#: them.
#:
#: 16KB is about four thousand tokens: large enough for any config file, a
#: batch file, a source file or a short log, and small enough that a careless
#: `get_file` of something big cannot cost a turn. The guest's own 4MB ceiling
#: still applies to what travels, and is fine now that the big cases land on
#: disk rather than in context.
GET_INLINE_MAX = 16 * 1024

# The guest clamps this too; a tool argument is a suggestion, not a promise.
DEFAULT_TIMEOUT_MS = 20_000
MAX_TIMEOUT_MS = 120_000


def _selector_part(target: str, step: str) -> str:
    """The `step`-th step of a selector, or "".

    THE SPLIT IS HERE AND NOT ON THE GUEST, which sends only the index. The
    guest would be re-rendering a string it was handed, and getting the
    spacing subtly different from what the caller wrote -- so it names the
    position and this names the text. Steps end at `>` and at whitespace,
    as the guest counts them (selectors.split_steps).
    """
    try:
        want = int(step)
    except (TypeError, ValueError):
        return ""
    parts = split_steps(target)
    return parts[want] if 0 <= want < len(parts) else ""


#: `[Ctrl+A]` as select-all, in any of the spellings the parser accepts.
_CTRL_A = re.compile(r"[\[{]\s*(?:ctrl|control)\s*\+\s*a\s*[\]}]", re.I)


def _ctrl_a_note(text: str) -> str:
    """Said on the RESULT, not refused, and not left to the argument text.

    Ctrl+A is not select-all on this machine -- measured twice on 2026-10-02,
    on a custom Win32 edit control and on Notepad, where `[Ctrl+A]goodbye`
    against a box holding "hello" produced "hellogoodbye". It does nothing and
    nothing reports that it did nothing, which is exactly the shape of failure
    the tool notes exist for.

    THE ARGUMENT DESCRIPTION ALREADY SAID SO AND THE AGENT DID IT ANYWAY, in
    the very session this was added for. An argument description is the part of
    a schema a model attends to least; a line in the result arrives after the
    act, when it is the only thing between the agent and a wrong conclusion
    about what is in the box.

    A NOTE AND NOT A REFUSAL. Two apps are not every app, and an application
    that implements the accelerator itself would be blocked by a refusal for no
    reason. This costs a sentence and leaves the capability alone.
    """
    return prompts.text("tools.ui.view.ctrl_a") + "\n" if _CTRL_A.search(text) else ""


def _ui_explain(exc: UiError, target: str = "") -> str:
    """A guest status, as something the model can act on.

    Every one of these says what to do next, which is why they live in
    tools.toml with the rest of the prose the agent reads."""
    fields = dict(exc.headers)
    fields.setdefault("status", exc.status)
    #: A guest older than the on_top header still has to get the sentence,
    #: not the "unknown status" fallback that a missing placeholder would
    #: otherwise produce.
    fields.setdefault("on_top", "another window")
    fields.setdefault("key", "that")
    status = exc.status
    #: WHICH PART of a compound selector was missing. `A > B` failing says
    #: nothing about whether A was there, and an agent told only "nothing
    #: matches ... the window may not be open" believed the window had gone --
    #: while it was on screen and only the button did not exist, because the
    #: program had no Close button at all (live session, 2026-10-02). The guest
    #: sends the index; the component text is recovered here.
    if status == "not_found":
        part = _selector_part(target, fields.get("step", ""))
        if part:
            fields["part"] = part
            status = "not_found_part"
    #: WHO collided, not just how many: the guest names up to three matches
    #: with their parent and top-level window, and each gets a selector that
    #: narrows by its window. A count alone sent agents to `:nth`, which picks
    #: by position and silently changes meaning when a window opens.
    if status == "ambiguous":
        listed = render_candidates(exc.headers, target)
        if listed:
            base = prompts.text("tools.ui.refused.ambiguous_listed",
                                count=exc.detail or "several controls match",
                                candidates=listed)
            return base
    try:
        base = prompts.text(f"tools.ui.refused.{status}", **fields)
    except KeyError:
        base = prompts.text("tools.ui.refused.unknown", status=exc.status)
    return f"{base} ({exc.detail})" if exc.detail else base


def _render_run(headers: dict, body: bytes, compiled, steps: list
                ) -> tuple[str, bool]:
    """A step list's outcome, as the agent needs it: how far it got, what
    each step that ran did, and -- for the one that failed -- the same
    sentence `click` or `ui` would have given, naming the step as it was
    written. Returns (text, failed)."""
    rows, released = parse_reply(body)
    status = headers.get("status", "")
    lines: list[str] = []
    for row in rows:
        try:
            wire = int(row.get("i", b"-1"))
        except ValueError:
            continue
        idx = compiled.origin[wire] if 0 <= wire < len(compiled.origin) else 0
        phrase = compiled.phrases[idx - 1] if 0 < idx <= len(
            compiled.phrases) else "?"
        st = row.get("status", b"").decode("ascii", "replace")
        hit = row.get("hit", b"").decode("cp1252", "replace")
        detail = row.get("detail", b"").decode("cp1252", "replace")
        line = f"{idx}. {phrase}: {st}"
        if hit and st == "ok":
            line += f" ({hit})"
        if detail and st == "ok":
            line += f" -- {detail}"
        lines.append(line)
    # Several wire lines can belong to one step (a string with chords); the
    # agent counted steps, so the list is folded to one line per step.
    seen: dict[str, str] = {}
    for line in lines:
        key = line.split(".", 1)[0]
        seen[key] = line if key not in seen or not line.endswith(": ok") \
            else seen[key]
    out = list(seen.values())
    if released:
        out.append(prompts.text("tools.input.view.released"))

    if status == "ok":
        return (prompts.text("tools.input.view.done", n=len(steps))
                + "\n" + "\n".join(out)), False

    wire = int(headers.get("step", "0") or 0)
    idx = compiled.origin[wire] if 0 <= wire < len(compiled.origin) else 0
    step = steps[idx - 1] if 0 < idx <= len(steps) else ""
    target = ""
    if isinstance(step, dict):
        for k in ("click", "focus", "close", "move", "wait_for", "wait_gone",
                  "wait_text", "where"):
            if isinstance(step.get(k), str):
                target = step[k]
                break
    fields = dict(headers)
    part_row = next((r for r in rows if r.get("i") == str(wire).encode()),
                    {})
    if part_row.get("part"):
        fields["step"] = part_row["part"].decode("ascii", "replace")
    # The guest says `timeout` for a step that ran out of time, which is not
    # the machine failing to answer -- that sentence would send the agent
    # looking for a dead link.
    if status == "timeout":
        status = "step_timeout"
    exc = UiError(status, headers.get("detail", ""), fields)
    why = _ui_explain(exc, target)
    head = prompts.text("tools.input.view.failed", step=idx,
                        n=len(steps), done=headers.get("done", "0"))
    return head + "\n" + "\n".join(out) + "\n" + why, True


def _text(body: str, *, error: bool = False) -> dict:
    """A tool result, and BOTH SPELLINGS of the error flag.

    `claude_agent_sdk` reads `result.get("is_error", False)` when it builds the
    `CallToolResult` (`__init__.py`, `run_tool`), while the MCP wire field it
    produces is `isError`. This wrote only `isError`, so the key never matched
    and EVERY refusal from EVERY tool here reached the model as a SUCCESS
    carrying refusal text -- which is most of the way there, since the words
    arrive, and wrong in the one way that matters: a failed call the model is
    told succeeded is a call it has no reason to retry or route around.

    Both keys, not just the one the SDK reads today: the dict also goes
    straight to test harnesses and anything else that speaks MCP's own
    spelling, and a result that is an error under one name and a success under
    the other is the bug this comment exists because of.
    """
    out: dict = {"content": [{"type": "text", "text": body}]}
    if error:
        out["is_error"] = True
        out["isError"] = True
    return out


def build(guest_exec, sync, workspace, guest=None, ui=None,
          proc=None, fs=None, workdir: str = "", mode=None) -> dict:
    """Create the `machine` server bound to THREE SERVICES and ONE GUEST.

    Named one by one rather than reached through something that has them. It
    was the Bridge first, which worked only because the Bridge forwarded
    everything; when the services stopped being reached through it, this
    closure was left calling methods the object it had been handed did not
    have, and the only symptom was the model telling the user it could not
    reach their machine. Passing a `host` that owns all three was the same
    mistake one step smaller -- a dependency on three things, declared as a
    dependency on whatever holds them.

    The guest matters. These closures used to capture only the Bridge and let
    each tool resolve `bridge.connection.guest` when it ran -- so the tool addressed
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

    # WHETHER THE FIVE SCREEN TOOLS EXIST FOR THIS SESSION. Read off the
    # guest's own declaration -- nothing new is threaded in, because `guest`
    # has carried `caps` since the handshake parsed it (bridge/handshake.py
    # -> ConnectionService._attach), which is before any session is built.
    #
    # A guest of None is a harness, not a machine: no screen, so no tools.
    # Doing it this way means a fake client in a test gets the same answer by
    # the same route as a real one.
    _caps = getattr(guest, "caps", None) if guest is not None else None
    automation = (getattr(_caps, "automation", C.AUTOMATION_NONE)
                  != C.AUTOMATION_NONE) if _caps is not None else False

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

        # The except below logs, because this is the outermost frame on our
        # side of the MCP boundary. Above it the SDK turns an exception into
        # a tool result the MODEL reads, and nothing reaches the bridge log
        # -- so a bug in this path is visible only as the model reporting "a
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
            asked = str((args or {}).get("cwd") or "").strip()
            cwd = _abs(asked) if asked else workspace.guest_work_root(guest)
            if cwd is None:
                return _text(_unresolved(asked), error=True)

            return await guest_exec.run_on_guest(
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
        return _text(sync.sync_status_text(guest))

    @tool(
        "sync_resolve",
        prompts.text("tools.sync_resolve.description"),
        # A full schema, not the Annotated shorthand: the shorthand marks
        # every property required, and `accept-tree` has no path to give.
        # The schema said one thing and the code below another -- the same
        # trap `exec`'s comment describes.
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path, relative to the mirrored "
                                   "folder, exactly as sync_status reported "
                                   "it. Not needed for accept-tree.",
                },
                "action": {
                    "type": "string",
                    "description": prompts.text(
                        "tools.sync_resolve.args.action"),
                },
            },
            "required": [],
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
        return _text(await sync.sync_resolve(path, action, guest))

    # --- driving the screen -------------------------------------------
    #
    # Four tools and not eight. The waits are PARAMETERS on `ui` and
    # `click` rather than a tool of their own: the loop an agent runs is
    # "do a thing, then look", and folding the wait into both ends of it
    # turns three round trips into one. Behind a socket to a 200MHz machine
    # the round trip is the expensive part. See docs/UIAUTOMATION.md.

    def _ui_ready() -> str:
        if ui is None or guest is None:
            return prompts.text("tools.ui.view.cannot_drive")
        # THE STALE CONVERSATION. A GuestSession outlives a reconnect, so a
        # session built while the setting was on still holds these five after
        # the client comes back declaring none of them -- not registering them
        # only shapes a session at the moment it is built. Read from `caps`
        # on every call rather than from the `automation` captured above, so
        # the live declaration wins and the refusal does not wake the machine's
        # desktop worker to be told the same thing.
        live = getattr(guest, "caps", None)
        if live is not None and getattr(
                live, "automation", C.AUTOMATION_NONE) == C.AUTOMATION_NONE:
            return prompts.text("tools.ui.refused.automation_off")
        return ""

    @tool(
        "ui",
        prompts.text("tools.ui.description"),
        {
            "type": "object",
            "properties": {
                "scope": {"type": "string",
                          "description": prompts.text("tools.ui.args.scope")},
                "pid": {"type": "integer",
                        "description": prompts.text("tools.ui.args.pid")},
                "max_nodes": {
                    "type": "integer",
                    "description": prompts.text("tools.ui.args.max_nodes")},
                "msaa": {"type": "boolean",
                         "description": prompts.text("tools.ui.args.msaa")},
                "hidden": {"type": "boolean",
                           "description": prompts.text("tools.ui.args.hidden")},
                "wait_for": {
                    "type": "string",
                    "description": prompts.text("tools.ui.args.wait_for")},
                "wait_ms": {
                    "type": "integer",
                    "description": prompts.text("tools.ui.args.wait_ms")},
                "settle_ms": {
                    "type": "integer",
                    "description": prompts.text("tools.ui.args.settle_ms")},
                "inproc": {
                    "type": "boolean",
                    "description": prompts.text("tools.ui.args.inproc")},
                "target": {
                    "type": "string",
                    "description": prompts.text("tools.ui.args.target")},
                "items_of": {
                    "type": "string",
                    "description": prompts.text("tools.ui.args.items_of")},
            },
            "required": [],
        },
    )
    async def machine_ui(args: dict) -> dict:
        try:
            why = _ui_ready()
            if why:
                return _text(why, error=True)
            a = args or {}
            scope = str(a.get("scope") or "desktop").strip().lower()
            if scope not in ("desktop", "foreground", "pid"):
                return _text(prompts.text("tools.ui.view.bad_scope",
                                          scope=repr(scope)), error=True)
            # `pid` AND `scope="pid"` TRAVEL TOGETHER OR NEITHER DOES.
            # `int(a.get("pid") or 0)` makes absent and 0 the same value, and
            # the guest reads 0 as "no filter" -- so `scope: "pid"` with no pid
            # quietly returned the WHOLE DESKTOP. On 2026-10-01 an agent asked
            # for the windows of one program, got a 25-control desktop dump,
            # could not find what it was looking for in it, and had no reason
            # to suspect the scope it asked for was not the scope it got.
            pid = int(a.get("pid") or 0)
            if scope == "pid" and pid <= 0:
                return _text(prompts.text("tools.ui.view.pid_needed"),
                             error=True)
            if pid > 0 and scope != "pid":
                return _text(prompts.text("tools.ui.view.pid_unused",
                                          scope=scope, pid=pid), error=True)
            # ONE CONTROL'S ITEMS: list rows, tree nodes, toolbar buttons,
            # desktop icons -- the names `input`'s `item` takes.
            items_of = str(a.get("items_of") or "").strip()
            if items_of:
                nodes, headers = await ui.tree(
                    guest, target=items_of, acc=True,
                    max_nodes=max(1, min(int(a.get("max_nodes") or 400), 400)))
                return _text(prompts.text("tools.ui.view.items_of",
                                          sel=items_of)
                             + "\n" + render_tree(nodes, headers))
            # `target` WITHOUT `inproc` WAS DROPPED ON THE WAY OUT
            # (protocol.ui_tree only sends it on the inproc branch), so the
            # agent got the whole scope back and no word that the control it
            # named had played no part. Said, rather than sent and ignored.
            if a.get("target") and not a.get("inproc"):
                return _text(prompts.text("tools.ui.view.target_unused"),
                             error=True)
            nodes, headers = await ui.tree(
                guest,
                scope=scope,
                pid=pid,
                max_nodes=max(1, min(int(a.get("max_nodes") or 400), 4000)),
                msaa=bool(a.get("msaa")),
                hidden=bool(a.get("hidden")),
                wait_for=str(a.get("wait_for") or ""),
                wait_ms=max(100, min(int(a.get("wait_ms") or 5000), 60000)),
                settle_ms=max(0, min(int(a.get("settle_ms") or 0), 5000)),
                inproc=bool(a.get("inproc")),
                target=str(a.get("target") or ""),
            )
            return _text(render_tree(nodes, headers))
        except UiError as exc:
            # The selector the guest was matching: a wait's, when there was
            # no target -- which is the only way `ui` reaches `ambiguous`.
            return _text(_ui_explain(exc, str(a.get("target")
                                              or a.get("items_of")
                                              or a.get("wait_for") or "")),
                         error=True)
        except Exception:
            log.exception("mcp__machine__ui failed: %r", args)
            raise

    @tool(
        "input",
        prompts.text("tools.input.description"),
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": prompts.text("tools.input.args.steps"),
                    "items": {"type": ["string", "object"]},
                },
                "timeout": {
                    "type": "integer",
                    "description": prompts.text("tools.input.args.timeout")},
                "settle": {
                    "type": "integer",
                    "description": prompts.text("tools.input.args.settle")},
            },
            "required": ["steps"],
        },
    )
    async def machine_input(args: dict) -> dict:
        a = args or {}
        try:
            why = _ui_ready()
            if why:
                return _text(why, error=True)
            live = getattr(guest, "caps", None)
            acp = getattr(live, "acp", "cp1252") or "cp1252"
            try:
                compiled = compile_steps(
                    a.get("steps"), timeout=a.get("timeout"),
                    settle=a.get("settle"),
                    encode=lambda t: fold.encode_exact(t, acp),
                    keys_version=getattr(live, "keys", 0) or 1)
            except StepError as exc:
                return _text(prompts.text("tools.input.refused",
                                          why=str(exc)), error=True)
            headers, body = await ui.run(
                guest, compiled.body, timeout_ms=compiled.timeout_ms,
                step_ms=STEP_TIMEOUT_DEFAULT, settle=compiled.settle)
            text, failed = _render_run(headers, body, compiled,
                                       a.get("steps") or [])
            typed = " ".join(st for st in a.get("steps") or []
                             if isinstance(st, str))
            return _text(_ctrl_a_note(typed) + text, error=failed)
        except UiError as exc:
            return _text(_ui_explain(exc, ""), error=True)
        except Exception:
            log.exception("mcp__machine__input failed: %r", args)
            raise

    @tool(
        "shot",
        prompts.text("tools.shot.description"),
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer",
                      "description": prompts.text("tools.shot.args.x")},
                "y": {"type": "integer",
                      "description": prompts.text("tools.shot.args.y")},
                "w": {"type": "integer",
                      "description": prompts.text("tools.shot.args.w")},
                "h": {"type": "integer",
                      "description": prompts.text("tools.shot.args.h")},
            },
            "required": [],
        },
    )
    async def machine_shot(args: dict) -> dict:
        try:
            why = _ui_ready()
            if why:
                return _text(why, error=True)
            a = args or {}
            bmp, headers = await ui.shot(
                guest,
                x=max(0, int(a.get("x") or 0)), y=max(0, int(a.get("y") or 0)),
                w=max(0, int(a.get("w") or 0)), h=max(0, int(a.get("h") or 0)))
            png = to_png(bmp)
            if png is None:
                return _text(
                    prompts.text("tools.ui.view.no_image",
                                 w=headers.get("w"), h=headers.get("h"),
                                 bpp=headers.get("bpp"), size=len(bmp)),
                    error=True)
            return {"content": [{
                "type": "image",
                "data": base64.b64encode(png).decode("ascii"),
                "mimeType": "image/png",
            }]}
        except UiError as exc:
            return _text(_ui_explain(exc, str(a.get("target") or "")),
                         error=True)
        except Exception:
            log.exception("mcp__machine__shot failed: %r", args)
            raise

    def _proc_refused(status: str, pid: str = "") -> str:
        try:
            return prompts.text(f"tools.ps.refused.{status}", pid=pid)
        except KeyError:
            return prompts.text("tools.ps.refused.unknown", status=status,
                                pid=pid)

    @tool(
        "ps",
        prompts.text("tools.ps.description"),
        {"type": "object", "properties": {}, "required": []},
    )
    async def machine_ps(args: dict) -> dict:
        try:
            if proc is None or guest is None:
                return _text(prompts.text("tools.ps.refused.unavailable"),
                             error=True)
            try:
                rows, truncated = await proc.list(guest)
            except ProcError as exc:
                return _text(_proc_refused(exc.status), error=True)
            lines = [f"{'pid':<8} {'parent':<8} {'thr':>3}  program"]
            lines += [f"{p.pid:<8} {p.ppid:<8} {p.threads:>3}  {p.exe}"
                      for p in rows]
            body = "\n".join(lines)
            # MEMORY, AS IT IS NOW. On a 32MB machine "why is it slow" is
            # usually the swap line, and the agent deciding what to end
            # needs the figure while it decides, not HELLO's from startup.
            mem = await proc.memory(guest)
            if "mem_kb" in mem and "mem_free_kb" in mem:
                body += "\n\n" + prompts.text(
                    "tools.ps.view.memory", free=mem["mem_free_kb"] // 1024,
                    total=mem["mem_kb"] // 1024,
                    swap_used=max(0, mem.get("swap_kb", 0)
                                  - mem.get("swap_free_kb", 0)) // 1024,
                    load=mem.get("mem_load", 0))
            if truncated:
                body += "\n" + prompts.text("tools.ps.view.truncated")
            return _text(body)
        except Exception:
            log.exception("mcp__machine__ps failed: %r", args)
            raise

    @tool(
        "kill",
        prompts.text("tools.kill.description"),
        {
            "type": "object",
            "properties": {
                "pid": {"type": "string",
                        "description": prompts.text("tools.kill.args.pid")},
                "force": {"type": "boolean",
                          "description": prompts.text("tools.kill.args.force")},
            },
            "required": ["pid"],
        },
    )
    async def machine_kill(args: dict) -> dict:
        try:
            if proc is None or guest is None:
                return _text(prompts.text("tools.ps.refused.unavailable"),
                             error=True)
            a = args or {}
            pid = str(a.get("pid") or "").strip().lower()
            if pid.startswith("0x"):
                pid = pid[2:]
            if not pid or any(c not in "0123456789abcdef" for c in pid):
                return _text(prompts.text("tools.ps.refused.bad_pid", pid=pid),
                             error=True)
            try:
                status = await proc.kill(guest, pid,
                                         force=bool(a.get("force")))
            except ProcError as exc:
                status = exc.status
            if status == "ok":
                return _text(prompts.text("tools.kill.view.ok", pid=pid))
            if status == "pending":
                return _text(prompts.text("tools.kill.view.pending", pid=pid))
            return _text(_proc_refused(status, pid), error=True)
        except Exception:
            log.exception("mcp__machine__kill failed: %r", args)
            raise

    def _abs(path: str) -> str | None:
        """A path the agent wrote, made absolute against this conversation's
        folder on the machine (bridge/files/paths.py on_guest); None if it
        cannot be. Done here, not only in the fs service, so every reply
        names the path that was really touched."""
        resolve = getattr(guest, "on_guest", None)
        if resolve is None or not path:
            return path
        return resolve(path)

    def _unresolved(path: str) -> str:
        """Why `path` could not be made a path on the machine."""
        return _fs_refused("proxy_path" if path.strip().startswith("/")
                           else "relative", path)

    def _fs_refused(status: str, path: str = "") -> str:
        try:
            return prompts.text(f"tools.fs.refused.{status}", path=path)
        except KeyError:
            return prompts.text("tools.fs.refused.unknown", status=status,
                                path=path)

    def _acp() -> str:
        caps = getattr(guest, "caps", None) if guest is not None else None
        return getattr(caps, "acp", "") or ""

    def _fscaps():
        """The client's declared conventions, or the defaults.

        `P.to_guest` reads `caps.eol` from this, which is how a file lands
        with the endings the MACHINE asked for instead of the ones this host
        happens to use.
        """
        return getattr(guest, "caps", None) or P.DEFAULT

    #: Whether a tool call wants the bytes converted. Default ON.
    #:
    #: THE LANE DOES NOT CONVERT, DELIBERATELY: `guest_fs.py` is "byte for
    #: byte, with no baseline and no opinion about line endings", and that is
    #: right for the lane. It was wrong as the only behaviour the agent could
    #: get. The sync layer classifies text and converts; put_file did not; so
    #: the same batch file was runnable through one path and silently broken
    #: through the other, and on 2026-10-01 a live session spent eleven tool
    #: calls finding that out (docs/measurements/
    #: batch-eol-and-dir-b-p200-20261001.txt -- COMMAND.COM reads an LF-only
    #: .bat as ONE command line and exits 0).
    #:
    #: WHAT IT COVERS, both halves, which is why it is not called `eol`:
    #:
    #:   encoding  text on this side is Unicode and that machine's is a
    #:             single-byte codepage. A str has to be encoded either way
    #:             -- there is no "raw" form of a str for a cp1252 machine --
    #:             so this always happens, and a character the codepage
    #:             cannot hold is a NAMED REFUSAL rather than an exception
    #:             out of the tool. It is never transliterated: `fold.encode`
    #:             would turn U+00B7 into `.`, which in a path invents a
    #:             separator, and file contents include paths and commands.
    #:   endings   LF <-> CRLF, by `P.classify`, so a file with a NUL
    #:             anywhere, a lone CR, a \r\r\n or mixed endings is left
    #:             exactly as it is. Those rules are not fussiness: see
    #:             `P.classify` for why normalisation has to be idempotent.
    #:
    #: Off is for the case that has to be byte-exact -- a checksum, an archive
    #: you are staging through, a file whose endings are the thing you are
    #: inspecting.
    def _translating(args: dict) -> bool:
        v = args.get("translation")
        if v is None:
            return True
        if isinstance(v, str):
            return v.strip().lower() not in ("off", "false", "no", "0", "raw")
        return bool(v)

    def _endings(data: bytes, cls: str) -> str:
        """What to say about this file's line endings.

        Five shapes, because `P.classify` distinguishes five and the agent
        needs to know WHICH: "mixed endings" and "lone CR" are things it may
        have caused and can fix, while "binary" is not.

        AND RELATIVE TO WHAT THE MACHINE DECLARED, never to an operating
        system this code has decided it is talking to. `caps.eol` says which
        ending that client writes, so "not the one this machine uses" is the
        fact that matters and stays true on a DOS box, an NT box or a PDP.
        """
        if b"\x00" in data:
            return prompts.text("tools.get_file.endings.binary")
        if cls != P.CLASS_TEXT:
            return prompts.text("tools.get_file.endings.irregular")
        if b"\n" not in data and b"\r" not in data:
            return prompts.text("tools.get_file.endings.none")
        names = ("lf", "crlf", "cr")
        got = "crlf" if b"\r\n" in data else "lf"
        want = getattr(_fscaps(), "eol", C.EOL_CRLF)
        want_name = names[want] if 0 <= want < len(names) else "lf"
        key = "native" if got == want_name else "foreign"
        return prompts.text(
            "tools.get_file.endings." + key,
            eol=prompts.text("tools.get_file.eol." + got),
            want=prompts.text("tools.get_file.eol." + want_name))

    def _encode_text(text: str) -> tuple[bytes, str]:
        """A str -> that machine's bytes, or ("", reason).

        `encode_exact` raises `UnicodeEncodeError`, and until 2026-10-02 this
        call sat under a bare `except Exception: raise`, so one box-drawing
        character in a README reached the agent as a traceback. Measured the
        same day: cp1252 holds the euro sign and does not hold U+2500,
        Cyrillic or any emoji.
        """
        # The codec THAT WAS USED, not the one that was asked for. `_acp()` is
        # "" until a machine declares one and `encode_exact` then falls back to
        # the connection's codepage, so naming `_acp()` in the refusal could
        # report a codepage that had nothing to do with the failure.
        cp = _acp() or fold.codec()
        try:
            return fold.encode_exact(text, cp), ""
        except UnicodeEncodeError as exc:
            bad = text[exc.start:exc.end]
            return b"", prompts.text(
                "tools.put_file.refused.unencodable",
                char=bad, code=("U+%04X" % ord(bad[0])) if bad else "?",
                cp=cp or "the machine's codepage", at=str(exc.start))

    def _fetched_dir() -> str:
        """Where a fetched binary lands.

        A SIBLING of the working directory, never inside it. The working
        directory IS the mirror -- machine.py:71 computes everything relative
        to it and says so -- so a file written there is reported by the next
        scan as new and pushed BACK to the guest. Conflict staging already
        sits one level up for exactly this reason; this follows it.
        """
        base = os.path.dirname(workdir.rstrip(os.sep)) if workdir else "."
        return os.path.join(base, ".gaston", "fetched")

    #: What a downloaded file IS, for the one line the agent gets about it.
    #:
    #: SNIFFED, NOT SHELLED OUT. `file(1)` gives better prose and would make
    #: this tool behave differently depending on what is installed on the
    #: proxy, which is the one thing a tool surface must not do.
    #:
    #: NO OPERATING SYSTEM IS NAMED, and `test_no_os_in_code` enforces it: an
    #: MZ header is an "executable program" here, not a DOS one, because this
    #: table is shown to the agent and the tool surface has to survive being
    #: pointed at a different machine (CLAUDE.md, "Tool names are OS-neutral").
    #: Which operating system it is belongs in the profiles, not in a string
    #: this prints. Anything unrecognised falls back to the extension, and
    #: then to an honest "binary data".
    _MAGIC = (
        (b"MZ",                    "executable program"),
        (b"PK\x03\x04",            "ZIP archive"),
        (b"MSCF",                  "CAB archive"),
        (b"GIF8",                  "GIF image"),
        (b"\x89PNG",               "PNG image"),
        (b"\xff\xd8\xff",          "JPEG image"),
        (b"BM",                    "BMP image"),
        (b"%PDF",                  "PDF document"),
        (b"\xd0\xcf\x11\xe0",      "OLE compound document"),
        (b"\x1f\x8b",              "gzip data"),
        (b"RIFF",                  "RIFF media (WAV or AVI)"),
        (b"\x00\x00\x01\x00",      "icon"),
    )

    def _describe(data: bytes, path: str, textual: bool) -> str:
        if textual:
            return "text"
        for magic, what in _MAGIC:
            if data.startswith(magic):
                return what
        guess, _ = mimetypes.guess_type(path.replace("\\", "/"))
        return guess or "binary data"

    def _size(n: int) -> str:
        """Bytes under 10KB, otherwise the round number a person reads."""
        if n < 10 * 1024:
            return f"{n} bytes"
        if n < 1024 * 1024:
            return f"{n / 1024:.0f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    async def _save_fetched(path: str, data: bytes) -> str:
        """Write the bytes down and return where.

        ONE SUBDIRECTORY PER SOURCE PATH, keyed on a hash of it. The name used
        to be the basename alone, so fetching `C:\\A\\CONFIG.SYS` and then
        `C:\\B\\CONFIG.SYS` silently overwrote the first with the second and
        handed back a path that no longer held what the agent thought. The
        hash is short and the basename is kept beside it, because the agent
        has to read this path back and an opaque filename is one it cannot
        recognise in its own transcript.
        """
        digest = hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:8]
        dest_dir = os.path.join(_fetched_dir(), digest)
        await asyncio.to_thread(os.makedirs, dest_dir, exist_ok=True)
        name = os.path.basename(path.replace("\\", "/").rstrip("/")) or "file"
        dest = os.path.join(dest_dir, name)
        # Off the loop: a few megabytes onto a real disk is not instantaneous,
        # and this runs while a turn is live.
        await asyncio.to_thread(pathlib.Path(dest).write_bytes, data)
        return dest

    @tool(
        "get_file",
        prompts.text("tools.get_file.description"),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": prompts.text("tools.get_file.args.path")},
                "max_bytes": {"type": "integer",
                              "description": prompts.text(
                                  "tools.get_file.args.max_bytes")},
                "translation": {"type": "boolean",
                                "description": prompts.text(
                                    "tools.get_file.args.translation")},
            },
            "required": ["path"],
        },
    )
    async def machine_get_file(args: dict) -> dict:
        try:
            if fs is None or guest is None:
                return _text(prompts.text("tools.fs.refused.unavailable"),
                             error=True)
            path = str(args.get("path", "")).strip()
            if not path:
                return _text(_fs_refused("bad_path"), error=True)
            resolved = _abs(path)
            if resolved is None:
                return _text(_unresolved(path), error=True)
            path = resolved
            try:
                got = await fs.get(guest, path,
                                   max_bytes=int(args.get("max_bytes") or 0))
            except FsError as exc:
                return _text(_fs_refused(exc.status, path), error=True)

            note = (prompts.text("tools.get_file.view.truncated")
                    if got.truncated else "")

            # A NUL anywhere means binary, the same rule the guest's own
            # classifier uses -- not a sniff window, because a file that is
            # text for its first 8KB and binary afterwards is binary.
            cls = P.classify(got.data)
            textual = (b"\x00" not in got.data) and _translating(args)

            # SMALL TEXT COMES BACK INLINE AND NOTHING ELSE DOES.
            #
            # Until 2026-10-02 every text file was returned in full, with the
            # only ceiling the GUEST's 4MB -- about a million tokens of context
            # for one `get_file`, on a machine whose logs reach that size, and
            # the only thing in the way was the agent choosing to pass
            # `max_bytes`. Its description reads like an optimisation, not a
            # safety rail.
            #
            # The binary path already did the right thing: write it down, say
            # where. This is the same rule applied to the case that could
            # actually hurt, with a small-file exception so reading a 2KB .ini
            # does not cost two round trips on a machine where each one is a
            # second.
            if textual and len(got.data) <= GET_INLINE_MAX:
                try:
                    body = fold.decode(P.to_bridge(got.data, cls), _acp())
                except Exception:
                    body = None
                if body is not None:
                    # WHAT THE ENDINGS ARE, said out loud, because the agent
                    # cannot see them. On 2026-10-01 a session read a batch
                    # file back SPECIFICALLY to find out why it would not run,
                    # got text that looked perfect, and learned nothing -- LF
                    # and CRLF render identically.
                    return _text(body + note + "\n" + prompts.text(
                        "tools.get_file.view.endings",
                        kind=_endings(got.data, cls)))

            dest = await _save_fetched(path, got.data)
            return _text(prompts.text("tools.get_file.view.downloaded",
                                      kind=_describe(got.data, path, textual),
                                      size=_size(len(got.data)),
                                      local=dest) + note)
        except Exception:
            log.exception("mcp__machine__get_file failed: %r", args)
            raise

    @tool(
        "put_file",
        prompts.text("tools.put_file.description"),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": prompts.text("tools.put_file.args.path")},
                "content": {"type": "string",
                            "description": prompts.text(
                                "tools.put_file.args.content")},
                "from_path": {"type": "string",
                              "description": prompts.text(
                                  "tools.put_file.args.from_path")},
                "translation": {"type": "boolean",
                                "description": prompts.text(
                                    "tools.put_file.args.translation")},
            },
            "required": ["path"],
        },
    )
    async def machine_put_file(args: dict) -> dict:
        try:
            if fs is None or guest is None:
                return _text(prompts.text("tools.fs.refused.unavailable"),
                             error=True)
            path = str(args.get("path", "")).strip()
            content = args.get("content")
            from_path = str(args.get("from_path", "") or "").strip()
            if not path:
                return _text(_fs_refused("bad_path"), error=True)
            resolved = _abs(path)
            if resolved is None:
                return _text(_unresolved(path), error=True)
            path = resolved
            # Exactly one source. Both is ambiguous and neither is a mistake;
            # guessing at either is how the wrong bytes reach a real file.
            if (content is None) == (not from_path):
                return _text(prompts.text("tools.put_file.refused.one_source"),
                             error=True)

            if from_path:
                # from_path reads THIS machine's disk, which is a different
                # decision from writing anywhere on the guest: that one the
                # person approves per call, this one they cannot see. Confined
                # to the conversation's own folder.
                base = os.path.abspath(workdir or ".")
                target = os.path.abspath(os.path.join(base, from_path))
                try:
                    inside = os.path.commonpath([base, target]) == base
                except ValueError:
                    inside = False
                if not inside or not os.path.isfile(target):
                    return _text(prompts.text(
                        "tools.put_file.refused.from_path", path=from_path),
                        error=True)
                data = await asyncio.to_thread(pathlib.Path(target).read_bytes)
            else:
                data, why = _encode_text(str(content))
                if why:
                    return _text(why, error=True)

            # CONVERT LAST, over the bytes, whichever source they came from,
            # so `content=` and `from_path=` cannot disagree about what lands.
            # `P.classify` is the sync layer's own classifier, so a file put
            # here and the same file mirrored arrive identical -- which is the
            # entire point, and was not true until 2026-10-02.
            converted = ""
            if _translating(args):
                cls = P.classify(data)
                if cls == P.CLASS_TEXT:
                    out = P.to_guest(data, cls, _fscaps())
                    if out != data:
                        converted = prompts.text("tools.put_file.view.converted")
                    data = out

            try:
                n = await fs.put(guest, path, data)
            except FsError as exc:
                # MISSING FOLDERS ARE MADE, like any editor's Save does. The
                # agent wrote `proj\\src\\main.c` into a new folder, was
                # told the folder did not exist, and spent a turn and a
                # permission prompt per level on `mkdir` before writing the
                # same file again (2026-10-03). FSMKDIR makes parents too.
                # A client without it (ops < 1) gets the refusal as before.
                parent = ntpath.dirname(path)
                if (exc.status != "no_parent" or not parent
                        or _ops_ready(1)
                        or await _mirror_refusal(parent, creates=True,
                                                 is_dir=True)):
                    return _text(_fs_refused(exc.status, path), error=True)
                try:
                    await fs.op(guest, "FSMKDIR", parent)
                    n = await fs.put(guest, path, data)
                except FsError as exc2:
                    return _text(_fs_refused(exc2.status, path), error=True)
                converted += prompts.text("tools.put_file.view.made_folder",
                                          folder=parent)
            return _text(prompts.text("tools.put_file.view.wrote",
                                      path=path, bytes=n) + converted)
        except Exception:
            log.exception("mcp__machine__put_file failed: %r", args)
            raise

    def _ops_ready(need: int = 2) -> str:
        """Why the operations added with `input` cannot be offered to this
        client right now, or "" -- read from the LIVE caps, since a session
        outlives a reconnect to an older client. list_dir needs ops 1; the
        tree-aware mutations and launch, 2."""
        if fs is None or guest is None:
            return prompts.text("tools.fs.refused.unavailable")
        live = getattr(guest, "caps", None)
        if getattr(live, "ops", 0) < need:
            return prompts.text("tools.fs.refused.client_too_old")
        return ""

    async def _mirror_refusal(path: str, *, creates: bool = False,
                              is_dir: bool = False) -> str:
        """Why a change at `path` must not be made, if it is inside the
        mirrored folder -- "" otherwise.

        A change there reaches the host on the next sync pass exactly as one
        made by `put_file` does, and that pass updates the baseline; nothing
        is done to the baseline here. What IS done here: a path the sync
        layer is holding in conflict (or anything under one) is refused
        while the halt is up -- the person's copy is what is being protected
        -- and a name sync would later refuse to carry is not created.
        """
        root = getattr(guest, "work_guest", "") if getattr(
            guest, "work_dir", "") else ""
        if not root:
            return ""
        norm = path.replace("/", "\\").rstrip("\\")
        r = root.replace("/", "\\").rstrip("\\")
        if norm.lower() != r.lower() and not norm.lower().startswith(
                r.lower() + "\\"):
            return ""
        rel = norm[len(r):].lstrip("\\").replace("\\", "/")
        if any(part in ("..", ".") for part in rel.split("/")):
            return _fs_refused("bad_path", path)
        if rel.split("/")[0].lower() == ".gaston":
            return prompts.text("tools.fs.refused.sync_own", path=path)
        caps = getattr(guest, "caps", None) or P.DEFAULT
        if creates and rel:
            skip = P.check(rel, guest_root=root, caps=caps, is_dir=is_dir)
            if skip is not None:
                return prompts.text("tools.fs.refused.unsyncable", path=path,
                                    why=str(skip))
        if sync is None:
            return ""
        key = P.canonical(rel, caps=caps) if rel else ""
        async with sync.baseline_lock(guest):
            base = sync.load_baseline(guest)
            held = set(base.latched) | set(base.prev)
        for k in held:
            if not key or k == key or k.startswith(key + "/") \
                    or key.startswith(k + "/"):
                return prompts.text("tools.fs.refused.in_conflict", path=path,
                                    held=k)
        return ""

    async def _fs_change(name: str, op: str, path: str, to: str = "", *,
                         creates_at: str = "", is_dir: bool = False,
                         overwrite: bool = False,
                         permanent: bool = False,
                         raw_args: dict | None = None) -> dict:
        why = _ops_ready()
        if why:
            return _text(why, error=True)
        path = path.strip()
        if not path or (op in ("FSMV", "FSCOPY") and not to.strip()):
            return _text(_fs_refused("bad_path", path), error=True)
        resolved_path = _abs(path)
        resolved_to = _abs(to.strip()) if to.strip() else ""
        if resolved_path is None:
            return _text(_unresolved(path), error=True)
        if resolved_to is None:
            return _text(_unresolved(to.strip()), error=True)
        path, to = resolved_path, resolved_to
        for p_, made in ((path, False), (to.strip(), True)):
            if not p_:
                continue
            refusal = await _mirror_refusal(
                p_, creates=made or bool(creates_at), is_dir=is_dir)
            if refusal:
                return _text(refusal, error=True)
        # THE SECOND GATE (permdesk.gate). A delete, copy or move that came
        # round the permission desk -- Auto lets the CLI approve on its own --
        # is decided here instead: the preview, the reviewer, the bar.
        desk = getattr(guest, "perms", None)
        if name in ("delete", "copy", "move") and desk is not None:
            refused = await desk.gate(f"mcp__machine__{name}", raw_args)
            if refused:
                return _text(refused, error=True)
        try:
            h = await fs.op(guest, op, path, to.strip(), overwrite=overwrite,
                            permanent=permanent)
        except FsError as exc:
            text = _fs_refused(exc.status, path)
            if exc.detail:
                text += " " + prompts.text("tools.fs.view.stopped_at",
                                           where=exc.detail)
            return _text(text, error=True)
        files = int(h.get("files", 0) or 0)
        dirs = int(h.get("dirs", 0) or 0)
        size = int(h.get("bytes", 0) or 0)
        amount = prompts.text("tools.fs.view.amount", files=files, dirs=dirs,
                              size=f"{size / 1048576:.1f} MB" if size >= 1048576
                              else f"{size // 1024} KB")
        view = f"tools.{name}.view.done"
        if name == "delete":
            view += "_recycled" if h.get("how") == "recycled" else "_permanent"
        if name == "move":
            view += "_renamed" if h.get("how") == "renamed" else "_copied"
        return _text(prompts.text(view, path=path, to=to.strip(),
                                  amount=amount))

    @tool(
        "mkdir",
        prompts.text("tools.mkdir.description"),
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": prompts.text(
                                     "tools.mkdir.args.path")}},
         "required": ["path"]},
    )
    async def machine_mkdir(args: dict) -> dict:
        try:
            return await _fs_change("mkdir", "FSMKDIR",
                                    str((args or {}).get("path", "")),
                                    creates_at="self", is_dir=True)
        except Exception:
            log.exception("mcp__machine__mkdir failed: %r", args)
            raise

    @tool(
        "move",
        prompts.text("tools.move.description"),
        {"type": "object",
         "properties": {
             "path": {"type": "string",
                      "description": prompts.text("tools.move.args.path")},
             "to": {"type": "string",
                    "description": prompts.text("tools.move.args.to")},
             "overwrite": {"type": "boolean",
                           "description": prompts.text(
                               "tools.move.args.overwrite")}},
         "required": ["path", "to"]},
    )
    async def machine_move(args: dict) -> dict:
        try:
            a = args or {}
            return await _fs_change("move", "FSMV", str(a.get("path", "")),
                                    str(a.get("to", "")),
                                    overwrite=bool(a.get("overwrite")),
                                    raw_args=a)
        except Exception:
            log.exception("mcp__machine__move failed: %r", args)
            raise

    @tool(
        "copy",
        prompts.text("tools.copy.description"),
        {"type": "object",
         "properties": {
             "path": {"type": "string",
                      "description": prompts.text("tools.copy.args.path")},
             "to": {"type": "string",
                    "description": prompts.text("tools.copy.args.to")},
             "overwrite": {"type": "boolean",
                           "description": prompts.text(
                               "tools.copy.args.overwrite")}},
         "required": ["path", "to"]},
    )
    async def machine_copy(args: dict) -> dict:
        try:
            a = args or {}
            return await _fs_change("copy", "FSCOPY", str(a.get("path", "")),
                                    str(a.get("to", "")),
                                    overwrite=bool(a.get("overwrite")),
                                    raw_args=a)
        except Exception:
            log.exception("mcp__machine__copy failed: %r", args)
            raise

    @tool(
        "delete",
        prompts.text("tools.delete.description"),
        {"type": "object",
         "properties": {
             "path": {"type": "string",
                      "description": prompts.text("tools.delete.args.path")},
             "permanent": {"type": "boolean",
                           "description": prompts.text(
                               "tools.delete.args.permanent")}},
         "required": ["path"]},
    )
    async def machine_delete(args: dict) -> dict:
        try:
            a = args or {}
            return await _fs_change("delete", "FSRM", str(a.get("path", "")),
                                    permanent=bool(a.get("permanent")),
                                    raw_args=a)
        except Exception:
            log.exception("mcp__machine__delete failed: %r", args)
            raise

    @tool(
        "rmdir",
        prompts.text("tools.rmdir.description"),
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": prompts.text(
                                     "tools.rmdir.args.path")}},
         "required": ["path"]},
    )
    async def machine_rmdir(args: dict) -> dict:
        try:
            return await _fs_change("rmdir", "FSRMDIR",
                                    str((args or {}).get("path", "")))
        except Exception:
            log.exception("mcp__machine__rmdir failed: %r", args)
            raise

    @tool(
        "launch",
        prompts.text("tools.launch.description"),
        {"type": "object",
         "properties": {
             "command": {"type": "string",
                         "description": prompts.text(
                             "tools.launch.args.command")},
             "cwd": {"type": "string",
                     "description": prompts.text("tools.launch.args.cwd")},
             "show": {"type": "string",
                      "description": prompts.text("tools.launch.args.show")}},
         "required": ["command"]},
    )
    async def machine_launch(args: dict) -> dict:
        try:
            if proc is None or guest is None:
                return _text(prompts.text("tools.ps.refused.unavailable"),
                             error=True)
            why = _ops_ready()
            if why:
                return _text(why, error=True)
            a = args or {}
            command = str(a.get("command", "")).strip()
            show = str(a.get("show") or "").strip().lower()
            if show not in ("", "normal", "min", "max"):
                return _text(prompts.text("tools.launch.refused.bad_show"),
                             error=True)
            # Like exec: this conversation's folder unless told otherwise,
            # and a relative one joined to it -- left empty, the program
            # started in whatever directory the CLIENT was started from.
            asked = str(a.get("cwd") or "").strip()
            cwd = (_abs(asked) if asked else
                   (workspace.guest_work_root(guest)
                    if guest is not None and workspace is not None else ""))
            if cwd is None:
                return _text(_unresolved(asked), error=True)
            try:
                h = await proc.launch(guest, command,
                                      cwd=cwd,
                                      show="" if show == "normal" else show)
            except ProcError as exc:
                return _text(_proc_refused(exc.status), error=True)
            status = h.get("status", "") or "no_status"
            if status == "ok":
                return _text(prompts.text("tools.launch.view.started",
                                          command=command,
                                          pid=h.get("pid", "?")))
            try:
                return _text(prompts.text(f"tools.launch.refused.{status}",
                                          command=command,
                                          error=h.get("error", "")),
                             error=True)
            except KeyError:
                return _text(prompts.text("tools.launch.refused.failed",
                                          command=command,
                                          error=h.get("error", status)),
                             error=True)
        except Exception:
            log.exception("mcp__machine__launch failed: %r", args)
            raise

    @tool(
        "list_dir",
        prompts.text("tools.list_dir.description"),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": prompts.text(
                             "tools.list_dir.args.path")},
                "max_entries": {"type": "integer",
                                "description": prompts.text(
                                    "tools.list_dir.args.max_entries")},
            },
            "required": ["path"],
        },
    )
    async def machine_list_dir(args: dict) -> dict:
        """WHY THIS EARNS ITS PLACE (CLAUDE.md: "a tool earns its place only if
        `exec` does the job badly").

        Measured on real hardware, twelve files in one directory
        (docs/measurements/batch-eol-and-dir-b-p200-20261001.txt):

            dir /b <path>   131 bytes, names only, no sizes or dates
            dir    <path>   about 1,100 -- volume label, serial number, a
                            directory header, `.` and `..`, a free-space
                            footer, and BOTH the 8.3 and the long name per row

        So `exec` can give the agent names cheaply or details expensively, and
        neither answer is machine-readable, neither can be had without the
        console codepage in the middle, and a path with a space in it has to be
        quoted for a shell that splits on whitespace. A live session ran plain
        `dir` six times to check single files it had just compiled.

        `listDirectory` has been in libfs since step 5a and gremote has used it
        since step 4; this is the client's half of the same primitive, with the
        same wire shape.
        """
        try:
            # GATED on the client saying it answers FSLS: one that predates
            # it routed the frame to its UI thread as "unhandled", and the
            # call waited out four minutes before saying anything.
            why = _ops_ready(1)
            if why:
                return _text(why, error=True)
            path = str(args.get("path", "")).strip()
            if not path:
                return _text(_fs_refused("bad_path"), error=True)
            resolved = _abs(path)
            if resolved is None:
                return _text(_unresolved(path), error=True)
            path = resolved
            try:
                got = await fs.ls(guest, path,
                                  max_entries=int(args.get("max_entries") or 0))
            except FsError as exc:
                return _text(_fs_refused(exc.status, path), error=True)

            if not got.entries:
                return _text(prompts.text("tools.list_dir.view.empty",
                                          path=path))
            # DIRECTORIES FIRST, then by name, both case-insensitively. The
            # guest emits them in the order the filesystem hands them over,
            # which on FAT is creation order -- useful to nobody reading a
            # listing, and unstable between two calls on the same directory.
            rows = sorted(got.entries,
                          key=lambda e: (not e.is_dir, e.name.lower()))
            width = max(len(e.name) for e in rows)
            out = [prompts.text("tools.list_dir.view.header", path=path)]
            for e in rows:
                if e.is_dir:
                    size = prompts.text("tools.list_dir.view.dir")
                elif e.type != "f":
                    size = prompts.text("tools.list_dir.view.unknown")
                else:
                    size = f"{e.size:>10}"
                out.append(f"  {e.name:<{width}}  {size}")
            body = "\n".join(out)
            if got.truncated:
                body += "\n" + prompts.text(
                    "tools.list_dir.view.truncated", count=len(rows))
            return _text(body)
        except Exception:
            log.exception("mcp__machine__list_dir failed: %r", args)
            raise

    @tool(
        "drives",
        prompts.text("tools.drives.description"),
        {"type": "object",
         "properties": {"probe": {"type": "boolean"}},
         "required": []},
    )
    async def machine_drives(args: dict) -> dict:
        try:
            if fs is None or guest is None:
                return _text(prompts.text("tools.fs.refused.unavailable"),
                             error=True)
            try:
                rows = await fs.drives(guest, probe=bool(args.get("probe")))
            except FsError as exc:
                return _text(_fs_refused(exc.status), error=True)
            kinds = {0: "unknown", 1: "no such drive", 2: "removable",
                     3: "fixed", 4: "network", 5: "CD-ROM", 6: "RAM disk"}
            lines = [f"{'drive':<6} {'kind':<12} {'free':>10} "
                     f"{'size':>10}  label"]
            for d in rows:
                if d.has_media is None:
                    # Not probed -- a removable or a CD-ROM, and `probe` was
                    # not set. Distinct from `False`: this is "nobody asked",
                    # not "asked, and it is empty" (BACKLOG 20).
                    free = prompts.text("tools.drives.view.media_unknown")
                    size = ""
                elif not d.has_media:
                    # Reported on the guest's own answer, not inferred from
                    # zero bytes free. A mounted CD has none and is not an
                    # empty drive -- which is exactly how D: RETROTOOLS came
                    # back as "empty" on the first guest run.
                    free = prompts.text("tools.drives.view.no_media")
                    size = ""
                else:
                    free = f"{d.free_kb // 1024} MB"
                    size = f"{d.total_kb // 1024} MB" if d.total_kb else ""
                lines.append(f"{d.letter + ':':<6} "
                             f"{kinds.get(d.type, 'unknown'):<12} "
                             f"{free:>10} {size:>10}  {d.label}")
            return _text("\n".join(lines))
        except Exception:
            log.exception("mcp__machine__drives failed: %r", args)
            raise

    @tool(
        "toolnote",
        prompts.text("tools.toolnote.description"),
        {"type": "object",
         "properties": {
             "name": {"type": "string",
                      "description": prompts.text("tools.toolnote.args.name")},
             "version": {
                 "type": "string",
                 "description": prompts.text("tools.toolnote.args.version")},
         },
         "required": ["name"]},
    )
    async def machine_toolnote(args: dict) -> dict:
        """The one tool that reaches nothing.

        The note is rendered against the root and version DETECTION FOUND,
        not against the one the profile was written on. A machine with the
        JDK under Java WorkShop and a machine with a bare `C:\\JDK1.1.8`
        get the same note with different commands in it -- which is the
        whole reason the bodies are written against `{root}`.

        `guest.tools` is read at CALL time rather than captured at build
        time, for the same reason every other closure here takes `guest`:
        it is the inventory of the machine whose conversation this is.

        `version` IS OPTIONAL AND THE DEFAULT IS WHAT THE MACHINE SAID.
        Detection already reports a version for most toolchains, and that
        is the version the caller almost always means -- asking the model
        to repeat it back would be asking it to retype a number out of its
        own prompt and give a wrong note when it fat-fingered one.

        THE DISCLOSURE SITS ABOVE THE BODY, not under it. An agent that
        reads "no collections framework" and then a caveat has already
        decided what to write; the caveat is an instruction about how to
        read what follows, so it goes where instructions go. There is no
        second copy at the bottom: two warnings about one note train the
        model to skim both.
        """
        try:
            found = list(getattr(guest, "tools", None) or []) if guest else []
            known = [f.name for f in found if f.name in notes.NOTES]
            if not found:
                return _text(prompts.text("tools.toolnote.view.none_at_all"),
                             error=True)
            want = str(args.get("name") or "").strip().lower()
            # Matched case-insensitively because the inventory is printed
            # into a prompt and a model retyping `MSVC` or `JDK` from it is
            # not making a mistake worth a refusal.
            hit = next((f for f in found if f.name.lower() == want), None)
            if hit is None:
                return _text(prompts.text("tools.toolnote.view.unknown",
                                          name=args.get("name"),
                                          known=", ".join(known) or "none"),
                             error=True)
            note = notes.NOTES.get(hit.name)
            if note is None:
                return _text(prompts.text("tools.toolnote.view.no_note",
                                          name=hit.name,
                                          known=", ".join(known) or "none"),
                             error=True)
            asked = str(args.get("version") or "").strip()
            reported = getattr(hit, "version", "") or ""
            res = note.resolve(asked or reported)
            out = [prompts.text("tools.toolnote.view.header",
                                title=res.body.title, path=hit.path,
                                measured=res.body.measured)]
            if not res.understood:
                # The argument was dropped, and saying so is the whole of
                # the handling. Guessing which release `latest` or `VC98`
                # meant is exactly the silent approximation this change
                # exists to stop.
                documented = ", ".join(note.versions)
                out.append(prompts.text(
                    "tools.toolnote.view.bad_version",
                    version=asked, name=hit.name,
                    versions=documented or prompts.text(
                        "tools.toolnote.view.versions_none")))
            if res.tier != notes.TIER_EXACT:
                origin = prompts.text(
                    "tools.toolnote.view.origin_asked" if asked
                    else "tools.toolnote.view.origin_reported")
                # ONE QUIET LINE FOR A PATCH RELEASE, THE BANNER FOR A
                # DIFFERENT LINE. Spending "THIS IS NOT THE VERSION IN FRONT
                # OF YOU" on 1.1.3 answering 1.1.8 is how the agent learns to
                # skim it before the 1.1-answering-1.3 case arrives, which is
                # the one the JDK profile exists as a refusal because of. The
                # patch case is still said out loud, because `Measured:` is
                # the whole basis for acting without checking -- it is just
                # said in proportion.
                if res.tier == notes.TIER_PATCH:
                    major, minor = notes.line(
                        notes.parse_version(res.body.version) or ())
                    out.append(prompts.text(
                        "tools.toolnote.view.patch",
                        asked=res.asked, have=res.body.version,
                        origin=origin, line=f"{major}.{minor}"))
                else:
                    out.append(prompts.text(
                        f"tools.toolnote.view.{res.direction}",
                        asked=res.asked, have=res.body.version, origin=origin))
            # WHICH NUMBER GOES INTO `{version}` IN THE BODY. On an exact
            # match, the version in play -- so a bare `C:\\JDK1.1.8` still
            # renders "JDK 1.1.8" as it always has. On an inexact one, the
            # version the body was MEASURED on, because "JDK 1.3, measured
            # on this machine:" over 1.1 facts is the sentence the whole
            # disclosure above is there to prevent.
            shown = (res.body.version if not res.exact
                     else (res.asked if res.understood else reported))
            # Unchanged by the tiers: anything short of exact renders the
            # version the body was MEASURED on, because "JDK 1.1.8, measured
            # on this machine:" over 1.1.3 facts is a version claim nobody
            # checked, in the authoritative voice of a measured note.
            out.append(res.body.render(hit.path,
                                       shown or note.default_version))
            return _text("\n\n".join(out))
        except Exception:
            log.exception("mcp__machine__toolnote failed: %r", args)
            raise

    # THE SCREEN TOOLS ARE NOT REGISTERED FOR A CLIENT THAT CANNOT BE DRIVEN.
    #
    # Declaring a tool the client cannot use is wrong whatever it costs, and
    # it is not free: measured 2026-10-01 against haiku's 200K window, these
    # five are 1,576 tokens -- 46% of everything this server contributes and
    # 7.7% of the 20,360 a fresh session starts with. They sit in the
    # request's `tools` array, so that is once for the SESSION (about 0.8% of
    # the window, billed once per cache write), not per request.
    #
    # The closures above are still defined either way. Defining one costs
    # nothing; it is the schema reaching the model that costs, and that is
    # this list.
    #
    # Not a security boundary. A conversation outlives a reconnect, so a
    # session built while the setting was on keeps these five after the client
    # comes back declaring none of them -- and the client refuses every screen
    # op itself in that window (`rc_uirpc_handle`), which is where the control
    # has to live anyway. This list is about what the agent is TOLD it has.
    # A client that runs step lists gets `input` IN PLACE OF click, keys and
    # focus: one tool that does all three and more, in one round trip, with
    # the release and focus guarantees the single-shot tools never had. An
    # older client keeps the three it can answer.
    steps_ok = automation and getattr(_caps, "automation",
                                      0) >= C.AUTOMATION_STEPS
    screen = ([machine_ui, machine_input, machine_shot] if steps_ok
              else [machine_ui, machine_shot] if automation else [])
    if automation and not steps_ok:
        # click, keys and focus were retired for `input` (2026-10-02). A
        # client too old to run step lists can still be READ, not driven.
        log.info("machine: this client runs no step lists, so it is offered "
                 "ui and shot only -- update it for `input`")
    if not automation:
        log.info("machine: screen tools withheld -- the client declares no "
                 "automation support")
    # The fs mutations and `launch`, for a client that answers them. One
    # that does not is not told they exist -- and list_dir refuses on the
    # live caps, since it was registered before the gate existed.
    ops_ok = _caps is not None and getattr(_caps, "ops", 0) >= 2
    ops = ([machine_mkdir, machine_copy, machine_move, machine_delete,
            machine_rmdir, machine_launch] if ops_ok else [])
    offered = [machine_exec, machine_sync_status, machine_sync_resolve,
               *screen, machine_ps, machine_kill, machine_get_file,
               machine_list_dir, *ops, machine_put_file, machine_drives,
               machine_toolnote]
    # OFF MEANS NOT OFFERED (policy.py). A tool every one of whose
    # categories the conversation's mode turns Off is not registered at
    # all, so the model is never told it exists; the desk still refuses one
    # that arrives from a session built before the mode changed.
    from . import policy
    kept = offered if mode is None else [
        t for t in offered if policy.offered(mode, "mcp__machine__" + t.name)]
    if len(kept) != len(offered):
        log.info("machine: off in this mode: %s", ", ".join(
            t.name for t in offered if t not in kept))
    server = create_sdk_mcp_server(name="machine", version="0.1.0",
                                   tools=kept)
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
    # And for `input`: a step list may run 10 minutes (steps.py,
    # SCRIPT_TIMEOUT_MAX), the bridge waits that plus 20s and then 5s more
    # for the answer to its UICANCEL -- so this sits above 625s.
    return {"machine": {**server, "timeout": 700_000}}
