"""Tool permission prompts, rendered for a 640x480 screen.

The guest shows ONE prompt at a time, as a modeless bar above the input box.
Everything else queues. The model can request tools in parallel and a single
guest cannot show two bars, so the alternative to a queue is a second UI state
nobody has designed.

Three things were measured against the real CLI rather than assumed, and each
deleted work:

  * A Read or Grep INSIDE cwd never reaches can_use_tool at all -- Claude Code
    resolves read-tool permissions locally first. The auto-allow policy this
    module was going to carry was dead code, so there is none. Everything that
    arrives here gets a bar.
  * `blocked_path` was None on a Read of /etc/hostname. It is a hint, not the
    source of truth; the path arrived in input["file_path"] and in description.
  * `title` was None, with display_name="Read" and description="/etc/hostname".
    So the bar renders from display_name and description, and tolerates a
    missing title rather than assuming the CLI wrote one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..view import html as H

log = logging.getLogger("bridge.permissions")

# How long a prompt may sit unanswered before the bridge denies it for you.
# can_use_tool has no timeout of its own, so without this a prompt nobody
# answers wedges the session for as long as the process lives.
DEADLINE_S = 600.0

# What one prompt's HTML body may occupy. The frame cap is 64KB and _send
# gets no headroom for headers, so a large Write/Task/Edit body -- or just
# many distinct arguments -- could otherwise produce a frame GuestLink.send
# refuses outright (over the wire's MAX_FRAME, undeliverable to any client).
# Losing the argument table is survivable; losing the connection is not, so
# this degrades to a short fallback instead, logged below since the guest
# itself never shows that a table was cut.
MAX_BODY = 8 * 1024

# Per-value cap inside the rendered argument table. Long enough for a real
# path or a command line, short enough that ten of them still fit.
MAX_VALUE = 400

# Arguments worth showing first. The rest follow in whatever order they came,
# because a tool we have never seen should still render something useful.
_PREFERRED = ("command", "file_path", "path", "pattern", "url", "prompt",
              "old_string", "new_string", "content")

# The argument most worth naming in a ONE-LINE description -- narrower than
# _PREFERRED above, which orders a whole table: old_string/new_string/content
# belong in a row, not as the short label everything else is built from.
# render_request's `desc` fallback, `summary`'s bar text, and
# server._summarise's activity-pane line all read this one table instead of
# picking their own order by hand. docs/audit/02-bridge-python.md 02.15.
_SUMMARY_KEYS = ("command", "file_path", "pattern", "path", "url", "prompt")


def telling_argument(args: dict) -> tuple[str, str] | None:
    """The single argument most worth naming for this call, if any."""
    if not isinstance(args, dict):
        return None
    for key in _SUMMARY_KEYS:
        if args.get(key):
            return key, str(args[key])
    return None


@dataclass
class PendingPrompt:
    """One outstanding permission request.

    `future` is resolved by the PERMREPLY handler, by the deadline sweeper, by
    the disconnect path, or not at all -- if the SDK cancels the callback the
    coroutine simply dies and `future` is never read again.
    """

    tool_use_id: str
    tool_name: str
    future: Any                      # asyncio.Future[PermissionResult]
    suggestions: list = field(default_factory=list)
    # The rendered body, kept so a reconnect can re-issue the prompt without
    # re-rendering it from a context object that is long out of scope.
    body: str = ""
    allows: bool = False
    # The bar's one-line STATIC text, declared as a real field so readers
    # never need a defensive getattr(prompt, "summary", prompt.tool_name).
    summary: str = ""
    # What "Allow for this chat" grants for this call (policy.grant_keys);
    # empty means the button is not offered.
    grants: list = field(default_factory=list)


def _clip(text: str, cap: int = MAX_VALUE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _rows(args: dict) -> list[tuple[str, str]]:
    """Tool arguments, most useful first, clipped."""
    if not isinstance(args, dict):
        return []
    seen, rows = set(), []
    for key in _PREFERRED:
        if key in args and args[key] not in (None, ""):
            rows.append((key, _clip(args[key])))
            seen.add(key)
    for key, value in args.items():
        # `_`-keys are the desk's own annotations (the preview), shown in
        # their own paragraph, not as an argument the agent passed.
        if key in seen or key.startswith("_") or value in (None, "", [], {}):
            continue
        rows.append((key, _clip(value)))
    return rows


def render_request(tool_name: str, args: dict, ctx,
                   warning: str = "") -> str:
    """The HTML block describing one request.

    The bar itself only shows a one-line summary, because the transcript is not
    guaranteed to be visible -- a user paging back through history would get a
    bar and no context at all. This block is the elaboration, not the message.

    `warning` is the command guard's finding, and it is rendered HERE in full
    because here is the only place with room for it. On the bar it is a
    two-character marker and a clipped tail: that control is one
    `SS_ENDELLIPSIS` line of roughly seventy legible characters, and putting
    the guard's sentence in front of the command meant the user read the
    sentence and never saw the command (02.4).
    """
    display = plain_name(tool_name,
                         getattr(ctx, "display_name", None) or tool_name)
    title = getattr(ctx, "title", None)
    desc = getattr(ctx, "description", None)

    if not desc and isinstance(args, dict):
        told = telling_argument(args)
        if told:
            desc = told[1]

    # Two heads, one shown at a time, and a fixed id (not tool_use_id: the
    # desk shows one prompt at a time, so there is never more than one
    # outstanding -- same reasoning RC_ID_PENDING uses for the echo).
    # rc_html::settle_perm flips the container's class on answer; the CSS
    # picks whichever head matches, so no DOM text rewrite is needed.
    parts = ['<div class="perm" id="perm">',
             '<div class="permhead">Permission required</div>',
             '<div class="permdonehead">Answered</div>',
             "<p><b>" + H.esc(_clip(title or display, 120)) + "</b>"]
    if desc:
        parts.append(" &mdash; " + H.esc(_clip(desc)))
    parts.append("</p>")

    if warning:
        parts.append('<p class="permwarn"><b>Check this one:</b> '
                     + H.esc(_clip(warning, 400)) + "</p>")
    if isinstance(args, dict) and args.get("_preview"):
        parts.append("<p><b>What it touches:</b> "
                     + H.esc(_clip(args["_preview"], 600)) + "</p>")

    # A step list, as the numbered steps it will take -- the bar has room
    # for the first few words of that and this block has room for all of
    # it. The raw JSON follows in the table for anyone who wants it.
    if tool_name == "mcp__machine__input" and isinstance(args, dict):
        from .steps import StepError, compile_steps
        try:
            phrases = compile_steps(args.get("steps")).phrases
        except (StepError, TypeError, ValueError):
            phrases = []
        if phrases:
            parts.append("<ol>" + "".join(
                "<li>" + H.esc(_clip(p, 200)) + "</li>" for p in phrases)
                + "</ol>")

    rows = _rows(args)
    if rows:
        parts.append("<table>")
        for key, value in rows:
            parts.append("<tr><th>" + H.esc(key) + "</th><td>"
                         + H.esc(value) + "</td></tr>")
        parts.append("</table>")
    parts.append("</div>")

    html = "".join(parts)
    if len(html) <= MAX_BODY:
        return html
    # Log loudly here -- the guest only ever shows the fallback text, so this
    # is the only place an operator would see that a table got dropped.
    log.warning("permission prompt for %s is %d bytes, over the %d byte "
                "budget; the argument table is too large to display and "
                "was dropped", tool_name, len(html), MAX_BODY)
    # The guard's warning survives even though the argument table doesn't --
    # it is the one sentence saying why this call is worth reading.
    short = ('<div class="perm" id="perm">'
             '<div class="permhead">Permission required</div>'
             '<div class="permdonehead">Answered</div>'
             "<p><b>" + H.esc(_clip(title or display, 120)) + "</b></p>"
             + ('<p class="permwarn"><b>Check this one:</b> '
                + H.esc(_clip(warning, 400)) + "</p>" if warning else "")
             + "<p><i>(arguments too large to display)</i></p></div>")
    return short[:MAX_BODY]


def _machine_detail(tool_name: str, args: dict) -> str:
    """What a machine tool is about to do, for a tool whose telling argument
    is not one of _SUMMARY_KEYS. `kill` rendered as the bare word "kill":
    approving the end of a program without being told WHICH program."""
    if tool_name == "mcp__machine__kill" and args.get("pid"):
        line = f"process {args['pid']}"
        return line + " (forced)" if args.get("force") else line
    # EVERY STEP, in words. Approving one `input` call approves the whole
    # list -- focus this, type that, press Enter, close the other -- so the
    # bar is the one place the person sees what is about to happen to their
    # keyboard and mouse, and a bare tool name there would be a blank cheque.
    # Both ends: "move: C:\\WORK\\A.TXT" alone hides where it is going. And
    # the guest's own count of what it touches, when the desk asked.
    seen = f" ({args['_preview']})" if args.get("_preview") else ""
    if tool_name in ("mcp__machine__move", "mcp__machine__copy") \
            and args.get("path"):
        replace = ", REPLACING what is there" if args.get("overwrite") else ""
        return f"{args['path']} -> {args.get('to', '')}{replace}{seen}"
    if tool_name == "mcp__machine__delete" and args.get("path"):
        where = ("PERMANENTLY" if args.get("permanent")
                 else "to the Recycle Bin")
        return f"{args['path']} {where}{seen}"
    if tool_name == "mcp__machine__launch" and args.get("command"):
        return str(args["command"])
    if tool_name == "mcp__machine__input" and args.get("steps"):
        from .steps import summary as steps_summary
        return steps_summary(args["steps"])
    return ""


#: The bar is 640 pixels wide and the person reads it in a second.
#: `put_file: C:\\WINDOWS\\SYSTEM.INI` is not a sentence.
_PLAIN = {"exec": "Run on your machine",
         "put_file": "Write a file on your machine",
         "get_file": "Read a file from your machine",
         "drives": "List your machine's drives",
         "list_dir": "List a folder on your machine",
         "ps": "List the programs running on your machine",
         "kill": "End a program on your machine",
         "input": "Use your machine's keyboard and mouse",
         "mkdir": "Make a folder on your machine",
         "move": "Move or rename on your machine",
         "delete": "Delete on your machine",
         "copy": "Copy on your machine",
         "rmdir": "Remove a folder on your machine",
         "launch": "Start a program on your machine"}


def plain_name(tool_name: str, display: str) -> str:
    """A machine tool in words ("Delete on your machine"), else `display`.
    The bar line and the block above it say the same thing -- the block used
    to head itself `mcp__machine__delete`."""
    if not tool_name.startswith("mcp__machine__"):
        return display
    bare = display.lower().replace("mcp__machine__", "")
    return _PLAIN.get(bare, display)


def summary(tool_name: str, ctx, args: dict | None = None) -> str:
    """The one line the guest puts in the bar's STATIC.

    Must stand alone: this is all a user paging through history will see, and
    the whole decision rests on it.

    The CLI writes `description` for its own tools, but for an MCP tool it is
    empty -- so the first version of this rendered the bar as just "Exec",
    which asks someone to approve running a command without showing them the
    command. When there is no description, fall back to the most telling
    argument.
    """
    display = plain_name(tool_name,
                         getattr(ctx, "display_name", None) or tool_name)
    desc = getattr(ctx, "description", None) or getattr(ctx, "title", None)
    if not desc and args:
        desc = _machine_detail(tool_name, args)
    if not desc and args:
        told = telling_argument(args)
        if told:
            desc = told[1]
    line = display if not desc else f"{display}: {desc}"
    return _clip(line, 160)


# Tools whose input names a filesystem path on the proxy, and which key holds
# it. Bash is deliberately absent: a command line cannot be parsed for paths
# reliably, and a guard that is wrong half the time is worse than the system
# prompt telling the model the rule.
_PATH_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
}


#: Of those, the ones that MODIFY. Reading inside `.gaston/` is the whole
#: reason it sits within the agent's reach; writing there is never right.
_WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}


def off_limits(tool_name: str, args: dict, workdir: str | None,
               shared: str | None = None) -> str | None:
    """Why this call must be refused outright, or None to ask the user.

    The proxy is infrastructure, not the subject of the conversation. Every
    request the user makes is about THEIR machine, so a tool call reaching into
    the proxy's filesystem is either a mistake or an attempt to answer a
    question about the user's computer with the proxy's answer -- wrong even
    when the file it read was real.

    The same containment check that would have been dead code as an
    auto-ALLOW (Claude Code approves in-cwd reads locally and never asks us)
    is very much alive inverted: by the time a path tool reaches this
    callback, the path is already somewhere it should not be.

    `shared` is the directory the agent's session actually RUNS in, which is
    not always `workdir`: a new conversation's cwd is fixed before it commits
    to a folder, so the agent runs in the shared root while the mirror
    narrows to a folder underneath it. Passing it buys a second, more useful
    refusal -- see the two returns below.
    """
    if not workdir:
        return None
    key = _PATH_TOOLS.get(tool_name)
    if key is None:
        return None
    raw = (args or {}).get(key)
    if not raw:
        # Glob and Grep default to the working directory when path is absent,
        # which is exactly where they are allowed to be.
        return None

    try:
        root = os.path.realpath(workdir)
        target = os.path.realpath(str(raw))
    except (OSError, ValueError):
        return None

    # commonpath rather than startswith: "/work" must not match "/workspace".
    try:
        inside = os.path.commonpath([root, target]) == root
    except ValueError:      # different drives, or a relative/absolute mix
        inside = False
    if inside:
        return _inside_gaston(tool_name, raw, root, target)

    # THE CONFLICT DIRECTORY IS OUTSIDE cwd AND MUST STILL BE READABLE.
    #
    # `syncrun.Pass._conflict_dir` (`dirname(work_dir)/.gaston/conflict`) sits
    # outside the mirrored folder on purpose, because a staged copy inside the
    # tree would sync back. But the agent is told by `server.py`'s conflict
    # notice to read `mine.N`/`base.N` there to merge -- without this it would
    # hit the near-miss refusal below for the one directory it was just told
    # to open.
    #
    # Reads only: `_inside_gaston` below still refuses writes wherever
    # `.gaston` sits, since a staged copy is the only remaining copy of the
    # agent's own work.
    #
    # Keyed on where syncrun PUTS it -- the work folder's parent -- and not
    # only on `shared`: a bridge started with a --cwd apart from its
    # --sync-dir runs the agent somewhere else entirely, and then the one
    # directory a conflict notice names was refused as "on the proxy"
    # (measured 2026-10-03: the agent could not read its own mine.1 and
    # never merged).
    if tool_name not in _WRITE_TOOLS:
        bases = [os.path.dirname(root)]
        if shared:
            bases.append(os.path.realpath(shared))
        for base in bases:
            try:
                staged = os.path.join(base, ".gaston")
                if os.path.commonpath([staged, target]) == staged:
                    return None
            except (OSError, ValueError):
                pass

    # NEAR MISS deserves its own answer: `/home/x/work/notes.txt` is just as
    # much "on the proxy" as `/etc/passwd`, so the generic refusal below
    # explains nothing about why a sibling directory is fine and this one
    # isn't -- an agent that wrote one directory too high needs to be told
    # which folder, not sent off diagnosing a sync fault.
    if shared:
        try:
            near = os.path.commonpath(
                [os.path.realpath(shared), target]) == os.path.realpath(shared)
        except (OSError, ValueError):
            near = False
        if near:
            return (f"`{raw}` is outside this conversation's folder, and only "
                    f"that folder reaches the user's machine. Write to "
                    f"`{workdir}` instead -- the same file one directory "
                    f"down. Nothing else on this proxy is copied anywhere, "
                    f"however well the write appears to succeed.\n\n"
                    f"This is not a synchronisation fault and there is "
                    f"nothing to diagnose.")

    return (f"`{raw}` is on the proxy, not on the user's machine, and the "
            f"proxy is not what this conversation is about. Refused "
            f"automatically -- the user did not see this request.\n\n"
            f"If you were answering a question about the user's computer, that "
            f"machine is reached only through the `mcp__machine__` tools, and "
            f"this path very likely does not exist there. Say the path is not "
            f"valid for their machine rather than substituting the proxy's "
            f"copy. If you need a staging file, work inside `{workdir}`.")


def _inside_gaston(tool_name: str, raw, root: str, target: str) -> str | None:
    """Refuse a WRITE into `.gaston/`, which containment cannot catch.

    A staged copy exists precisely because the user's version won and the
    agent's was about to be lost, so it is the only remaining copy of that
    work; destroying it is the one outcome this layer is built to prevent.
    The containment check above only refuses paths OUTSIDE `workdir`, so
    without this the same placement that lets the agent read a staged copy
    would let it overwrite one.

    BOTH LOCATIONS: `syncrun.Pass` puts `.gaston` at `dirname(work_dir)/.gaston`
    (outside cwd, so Bash cannot reach it either), not at `work_dir/.gaston`,
    which nothing creates -- guarding only the phantom path was not a guard.

    Reads are untouched, which is what the staged copies are for. So is Bash:
    this module deliberately does not parse it, so the baseline is kept out
    of reach by PLACEMENT instead (`--sync-state` must resolve outside the
    working directory).
    """
    if tool_name not in _WRITE_TOOLS:
        return None
    here = os.path.join(root, ".gaston")
    above = os.path.join(os.path.dirname(root.rstrip(os.sep)), ".gaston")
    for gaston in (here, above):
        try:
            if os.path.commonpath([gaston, target]) == gaston:
                break
        except ValueError:
            continue
    else:
        return None
    return (f"`{raw}` is inside `.gaston/`, which belongs to the sync layer "
            f"rather than to the project. Refused automatically -- the user "
            f"did not see this request.\n\n"
            f"The copies under `.gaston/conflict/` exist because a conflict "
            f"was resolved in the user's favour and your version would "
            f"otherwise have been lost; they are the only remaining copy of "
            f"it. Read them freely -- that is what they are for -- but write "
            f"your merge to the real file in the sync root, which is what "
            f"reaches the user's machine.")


# --- what can run without asking --------------------------------------------
#
# NOT a rules table read from `.claude/settings*.json`: `Session` runs with
# `setting_sources=[]` on purpose -- the tool set a client gets must not
# depend on whose laptop runs the bridge -- so the operator's rules ARE NOT IN
# EFFECT here, and listing them would answer "what can happen?" with rules
# that cannot happen at all.
#
# It also has to admit what it cannot see (04.16): Claude Code resolves
# Read/Glob/Grep inside its working directory and sandboxed Bash LOCALLY and
# never calls `can_use_tool`, so nothing on the bridge observes them. A view
# that listed only what reaches the prompt would read as complete when it is
# already known to be false.


def rules_view(mode, lists=None, guest_root: str = "") -> str:
    """A read-only answer to "what can run without asking?": the
    conversation's execution mode, its table, and the client's lists that
    decide which row a call lands in."""
    from . import categories as K
    from . import policy, prompts

    def t(key: str, **kw) -> str:
        return prompts.text("policy.view." + key, **kw)

    p = ['<div class="perm">',
         '<div class="permhead">' + H.esc(t("head")) + "</div>",
         "<p><b>" + H.esc(mode.label) + "</b> &mdash; "
         + H.esc(policy.summary(mode)) + "</p>",
         "<table>"]
    for cat in K.CATS:
        p.append("<tr><th>" + H.esc(prompts.text(f"policy.cat.{cat}"))
                 + "</th><td>" + H.esc(t("value." + mode.value(cat)))
                 + "</td></tr>")
    p.append("</table>")
    p.append("<p>" + H.esc(t("work", root=guest_root or t("no_root")))
             + "</p>")
    protect = tuple(getattr(lists, "protect", ()) or ())
    p.append("<p>" + H.esc(t("protected", places=", ".join(protect))
                           if protect else t("no_protected")) + "</p>")
    p.append("<p>" + H.esc(t("honest")) + "</p>")
    p.append("<p>" + H.esc(t("unseen")) + "</p>")
    p.append("</div>")
    return "".join(p)
