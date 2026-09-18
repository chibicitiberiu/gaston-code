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

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any

from ..view import html as H
# How long a prompt may sit unanswered before the bridge denies it for you.
# can_use_tool has no timeout of its own, so without this a prompt nobody
# answers wedges the session for as long as the process lives.
DEADLINE_S = 600.0

# What one prompt's HTML body may occupy. The frame cap is 64KB and _send
# gets no headroom for headers, so the body is capped well below it: a Write
# with a large `content`, a Task prompt or an Edit with a long new_string
# would otherwise produce a frame that cannot be encoded -- and, because
# _send stamps into the replay buffer BEFORE encoding, one of those poisons
# every future replay permanently.
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
# render_request's `desc` fallback, `summary`'s bar text and
# server._summarise's activity-pane line each used to answer "which argument
# matters" independently, in three different orderings, so a tool added with
# a new interesting argument needed all three edited by hand -- and missing
# one is exactly how a permission bar reading "Click" with no coordinates
# ships. docs/audit/02-bridge-python.md 02.15.
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


def session_scoped(suggestions: list) -> list:
    """Force every suggested rule to session scope.

    PermissionUpdate.destination can name userSettings or projectSettings, and
    a dialog on a Win98 box quietly rewriting the developer's settings files is
    a surprise nobody asked for. The CLI already proposed "session" in every
    case measured, so this is usually a no-op -- but nothing promises that, and
    the failure mode is silent and permanent.
    """
    out = []
    for s in suggestions or []:
        try:
            out.append(dataclasses.replace(s, destination="session"))
        except Exception:
            # An unknown shape is still better forwarded than dropped: the CLI
            # authored it and the user pressed Always.
            out.append(s)
    return out


def allows_always(tool_name: str, suggestions: list) -> bool:
    """Whether the Always button should be offered at all.

    MCP rule names cannot carry specifiers -- the CLI rejects them with "MCP
    rules do not support patterns in parentheses" -- so the only rule that can
    be expressed for mcp__machine__exec is the WHOLE TOOL. `suggestions` is
    non-empty regardless, so keying the button off it would light it up, and
    one press on "run vol c:" would grant silent arbitrary command execution on
    the user's computer for the rest of the session.

    For a built-in like Bash the suggestion is scoped to one command shape, so
    Always means what it appears to mean and is offered.
    """
    if tool_name.startswith("mcp__machine__"):
        return False
    return bool(suggestions)


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
        if key in seen or value in (None, "", [], {}):
            continue
        rows.append((key, _clip(value)))
    return rows


def render_request(tool_name: str, args: dict, ctx) -> str:
    """The HTML block describing one request.

    The bar itself only shows a one-line summary, because the transcript is not
    guaranteed to be visible -- a user paging back through history would get a
    bar and no context at all. This block is the elaboration, not the message.
    """
    display = getattr(ctx, "display_name", None) or tool_name
    title = getattr(ctx, "title", None)
    desc = getattr(ctx, "description", None)

    if not desc and isinstance(args, dict):
        told = telling_argument(args)
        if told:
            desc = told[1]

    parts = ['<div class="perm">',
             '<div class="permhead">Permission required</div>',
             "<p><b>" + H.esc(_clip(title or display, 120)) + "</b>"]
    if desc:
        parts.append(" &mdash; " + H.esc(_clip(desc)))
    parts.append("</p>")

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
    # Losing the argument table is survivable; losing the connection is not.
    short = ('<div class="perm">'
             '<div class="permhead">Permission required</div>'
             "<p><b>" + H.esc(_clip(title or display, 120)) + "</b></p>"
             "<p><i>(arguments too large to display)</i></p></div>")
    return short[:MAX_BODY]


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
    display = getattr(ctx, "display_name", None) or tool_name
    if display.lower() == "exec" and tool_name.startswith("mcp__machine__"):
        display = "Run on your machine"
    desc = getattr(ctx, "description", None) or getattr(ctx, "title", None)
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


def off_limits(tool_name: str, args: dict, workdir: str | None) -> str | None:
    """Why this call must be refused outright, or None to ask the user.

    The proxy is infrastructure, not the subject of the conversation. Every
    request the user makes is about THEIR machine, so a tool call reaching into
    the proxy's filesystem is either a mistake or an attempt to answer a
    question about the user's computer with the proxy's answer -- which is
    wrong even when the file it read was real.

    Note the symmetry with what was deleted from this module: the plan wanted a
    containment check to auto-ALLOW reads inside the working copy, and that was
    dead code, because Claude Code approves those locally and never asks us.
    The same check inverted is not dead at all -- by the time a path tool
    reaches this callback, the path is already somewhere it should not be.
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

    `.gaston/` is inside the working directory deliberately, so the agent can
    read a conflict copy and the ancestor beside it without a new permission
    path. The containment check above only refuses what is OUTSIDE, so the same
    placement that lets the agent read a staged copy also let it overwrite one
    -- and a staged copy exists precisely because the user's version won and
    theirs was about to be lost. Destroying it is the one outcome this whole
    layer is built to prevent.

    Reads are untouched. So is Bash, which this module deliberately does not
    parse: the baseline itself is out of its reach by PLACEMENT instead, since
    `--sync-state` must resolve outside the working directory.
    """
    if tool_name not in _WRITE_TOOLS:
        return None
    gaston = os.path.join(root, ".gaston")
    try:
        if os.path.commonpath([gaston, target]) != gaston:
            return None
    except ValueError:
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
