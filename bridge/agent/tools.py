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
import logging
import os
import pathlib
from typing import Annotated

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

log = logging.getLogger("bridge.tools")

# The guest clamps this too; a tool argument is a suggestion, not a promise.
DEFAULT_TIMEOUT_MS = 20_000
MAX_TIMEOUT_MS = 120_000


def _ui_explain(exc: UiError) -> str:
    """A guest status, as something the model can act on.

    Every one of these says what to do next, which is why they live in
    tools.toml with the rest of the prose the agent reads."""
    fields = dict(exc.headers)
    fields.setdefault("status", exc.status)
    #: A guest older than the on_top header still has to get the sentence,
    #: not the "unknown status" fallback that a missing placeholder would
    #: otherwise produce.
    fields.setdefault("on_top", "another window")
    try:
        base = prompts.text(f"tools.ui.refused.{exc.status}", **fields)
    except KeyError:
        base = prompts.text("tools.ui.refused.unknown", status=exc.status)
    return f"{base} ({exc.detail})" if exc.detail else base


def _text(body: str, *, error: bool = False) -> dict:
    out: dict = {"content": [{"type": "text", "text": body}]}
    if error:
        out["isError"] = True
    return out


def build(guest_exec, sync, workspace, guest=None, ui=None,
          proc=None, fs=None, workdir: str = "") -> dict:
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
            cwd = (args or {}).get("cwd") or workspace.guest_work_root(guest)

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
            nodes, headers = await ui.tree(
                guest,
                scope=scope,
                pid=int(a.get("pid") or 0),
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
            return _text(_ui_explain(exc), error=True)
        except Exception:
            log.exception("mcp__machine__ui failed: %r", args)
            raise

    @tool(
        "click",
        prompts.text("tools.click.description"),
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": prompts.text("tools.click.args.target")},
                "double": {
                    "type": "boolean",
                    "description": prompts.text("tools.click.args.double")},
                "settle": {
                    "type": "integer",
                    "description": prompts.text("tools.click.args.settle")},
            },
            "required": ["target"],
        },
    )
    async def machine_click(args: dict) -> dict:
        try:
            why = _ui_ready()
            if why:
                return _text(why, error=True)
            a = args or {}
            target = str(a.get("target") or "").strip()
            if not target:
                return _text(prompts.text("tools.ui.refused.no_target"),
                             error=True)
            await ui.click(guest, target, double=bool(a.get("double")),
                           settle=max(0, min(int(a.get("settle") or 0), 5000)))
            return _text(prompts.text("tools.ui.view.clicked",
                                      target=target))
        except UiError as exc:
            return _text(_ui_explain(exc), error=True)
        except Exception:
            log.exception("mcp__machine__click failed: %r", args)
            raise

    @tool(
        "keys",
        prompts.text("tools.keys.description"),
        {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": prompts.text("tools.keys.args.text")},
                "target": {
                    "type": "string",
                    "description": prompts.text("tools.keys.args.target")},
                "settle": {
                    "type": "integer",
                    "description": prompts.text("tools.keys.args.settle")},
            },
            "required": ["text"],
        },
    )
    async def machine_keys(args: dict) -> dict:
        try:
            why = _ui_ready()
            if why:
                return _text(why, error=True)
            a = args or {}
            text = str(a.get("text") or "")
            if not text:
                return _text(prompts.text("tools.ui.refused.no_text"),
                             error=True)
            await ui.keys(guest, text, target=str(a.get("target") or ""),
                          settle=max(0, min(int(a.get("settle") or 0), 5000)))
            where = a.get("target") or "the focused control"
            return _text(prompts.text("tools.ui.view.typed",
                                      n=len(text), where=where))
        except UnicodeEncodeError:
            # fold.encode_exact refuses rather than approximating. Typing an
            # approximation of what was asked for is how the wrong thing
            # ends up in somebody's file.
            return _text(prompts.text("tools.ui.view.bad_character"),
                         error=True)
        except UiError as exc:
            return _text(_ui_explain(exc), error=True)
        except Exception:
            log.exception("mcp__machine__keys failed: %r", args)
            raise

    @tool(
        "focus",
        prompts.text("tools.focus.description"),
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": prompts.text("tools.focus.args.target")},
                "settle": {
                    "type": "integer",
                    "description": prompts.text("tools.focus.args.settle")},
            },
            "required": ["target"],
        },
    )
    async def machine_focus(args: dict) -> dict:
        try:
            why = _ui_ready()
            if why:
                return _text(why, error=True)
            a = args or {}
            target = str(a.get("target") or "").strip()
            if not target:
                return _text(prompts.text("tools.ui.refused.no_target"),
                             error=True)
            await ui.focus(guest, target,
                           settle=max(0, min(int(a.get("settle") or 0), 5000)))
            return _text(prompts.text("tools.ui.view.focused", target=target))
        except UiError as exc:
            return _text(_ui_explain(exc), error=True)
        except Exception:
            log.exception("mcp__machine__focus failed: %r", args)
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
            return _text(_ui_explain(exc), error=True)
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
            try:
                got = await fs.get(guest, path,
                                   max_bytes=int(args.get("max_bytes") or 0))
            except FsError as exc:
                return _text(_fs_refused(exc.status, path), error=True)

            note = (prompts.text("tools.get_file.view.truncated")
                    if got.truncated else "")

            # WHAT THE ENDINGS ARE, said out loud, because the agent cannot
            # see them. On 2026-10-01 a session read a batch file back
            # SPECIFICALLY to find out why it would not run, got text that
            # looked perfect, and learned nothing -- LF and CRLF render
            # identically. The answer it needed was one word.
            cls = P.classify(got.data)
            note += "\n" + prompts.text("tools.get_file.view.endings",
                                        kind=_endings(got.data, cls))

            # A NUL anywhere means binary, the same rule the guest's own
            # classifier uses -- not a sniff window, because a file that is
            # text for its first 8KB and binary afterwards is binary.
            # `translation=false` takes the same route as a binary: the agent
            # cannot hold bytes, so byte-exact means a local file.
            if b"\x00" not in got.data and _translating(args):
                try:
                    body = fold.decode(P.to_bridge(got.data, cls), _acp())
                except Exception:
                    body = None
                if body is not None:
                    return _text(body + note)

            # Binary: the agent cannot hold bytes, so they go to a file and it
            # is told where. Not into the mirror -- see _fetched_dir.
            dest_dir = _fetched_dir()
            await asyncio.to_thread(os.makedirs, dest_dir,
                                    exist_ok=True)
            name = os.path.basename(path.replace("\\", "/").rstrip("/")) or "file"
            dest = os.path.join(dest_dir, name)
            # Off the loop: a few megabytes onto a real disk is not
            # instantaneous, and this runs while a turn is live.
            await asyncio.to_thread(pathlib.Path(dest).write_bytes, got.data)
            return _text(prompts.text("tools.get_file.view.binary",
                                      path=path, local=dest,
                                      bytes=len(got.data)) + note)
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
                return _text(_fs_refused(exc.status, path), error=True)
            return _text(prompts.text("tools.put_file.view.wrote",
                                      path=path, bytes=n) + converted)
        except Exception:
            log.exception("mcp__machine__put_file failed: %r", args)
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
    screen = [machine_ui, machine_click, machine_keys, machine_shot,
              machine_focus] if automation else []
    if not automation:
        log.info("machine: screen tools withheld -- the client declares no "
                 "automation support")
    server = create_sdk_mcp_server(name="machine", version="0.1.0",
                                   tools=[machine_exec, machine_sync_status,
                                          machine_sync_resolve,
                                          *screen,
                                          machine_ps,
                                          machine_kill,
                                          machine_get_file,
                                          machine_put_file,
                                          machine_drives,
                                          machine_toolnote])
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
