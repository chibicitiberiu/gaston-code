"""Session listing and history replay for the chat list.

One live ClaudeSDKClient at a time: switching chats closes the current one and
reopens with resume=<id>. The list is read-only history. A warm client pool is
roughly three times the work and nobody notices on a single-user box.

Transcripts live at ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl, but
that format is internal and changes between releases. Use the SDK accessors.
"""

from __future__ import annotations

import datetime as _dt
import logging

from claude_agent_sdk import get_session_messages, list_sessions

from ..view import html as H
from ..view.html import APPEND, HtmlStream
from . import permissions as _perms

# How a piece of a stored message is tagged on the way to the renderer. These
# are internal to the replay path -- the wire carries HTML, not styles -- and
# they live here because _text_of is the only thing that produces them.
STYLE_PLAIN = "plain"
STYLE_CODE = "code"
STYLE_TOOL = "tool"
STYLE_USER = "user"

log = logging.getLogger("bridge.sessions")


def _age_group(when) -> str:
    """Which bucket a session falls in, computed here because the guest has no
    date library worth the name and no business having one."""
    if when is None:
        return "Older"
    try:
        if isinstance(when, (int, float)):
            # The SDK reports milliseconds since the epoch. Seconds would be a
            # perfectly reasonable guess and gives a date in the year 58000,
            # which fromtimestamp rejects and which silently became "Older".
            if when > 1e11:
                when = when / 1000.0
            when = _dt.datetime.fromtimestamp(when)
        elif isinstance(when, str):
            when = _dt.datetime.fromisoformat(when)
        if not isinstance(when, _dt.datetime):
            return "Older"
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return "Older"

    today = _dt.date.today()
    day = when.date()
    delta = (today - day).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return "This week"
    if delta < 31:
        return "This month"
    return "Older"


def listing(cwd: str | None = None, limit: int = 50) -> list[dict]:
    """Sessions, newest first, flattened to what the UI actually shows."""
    try:
        rows = list_sessions(directory=cwd, limit=limit)
    except Exception:
        log.exception("list_sessions failed")
        return []

    out = []
    for r in rows:
        # summary / custom_title / first_prompt are all provided, so there is
        # no need to derive a title from the first message.
        title = (getattr(r, "custom_title", None)
                 or getattr(r, "summary", None)
                 or (getattr(r, "first_prompt", "") or "")[:60]
                 or "(untitled)")
        modified = getattr(r, "last_modified", None)
        out.append({
            "id": r.session_id,
            "title": title,
            "cwd": getattr(r, "cwd", "") or "",
            "modified": str(modified or ""),
            "group": _age_group(modified),
            "bytes": str(getattr(r, "file_size", "") or ""),
        })
    return out


#: How Claude Code opens the synthetic message it writes at a compaction
#: boundary. MATCHED ON THE TEXT rather than the stored `isCompactSummary:
#: true` flag, because `SessionMessage.message` is the raw Anthropic dict
#: (`{content, role}`) and does not surface it -- reading it back means
#: opening the JSONL, which this module's docstring says not to.
#:
#: The sentence wins on how each option FAILS: if the wording changes, this
#: just stops matching (today's behaviour); if the JSONL format changes
#: instead, a reader raises or silently returns nothing for every session in
#: the list.
_COMPACT_OPENER = ("This session is being continued from a previous "
                   "conversation")


def _compact_summary(message) -> str | None:
    """The stored compaction summary's text, if this message is one.

    User role only: an assistant message that happens to quote the sentence --
    this project's own transcripts do, discussing this very code -- is a real
    reply and must be replayed.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, list):
        first = content[0] if content else None
        if not isinstance(first, dict) or first.get("type") != "text":
            return None
        content = first.get("text")
    if not isinstance(content, str):
        return None
    return content if content.startswith(_COMPACT_OPENER) else None


def _text_of(message) -> list[tuple[str, str]]:
    """Flatten one stored message into (style, text) runs.

    Defensive throughout: this walks persisted data whose exact shape is not
    guaranteed across releases, and a KeyError here would break the chat list
    rather than one message.
    """
    runs: list[tuple[str, str]] = []
    if not isinstance(message, dict):
        return runs

    role = message.get("role", "")
    content = message.get("content")

    if isinstance(content, str):
        runs.append((STYLE_USER if role == "user" else STYLE_PLAIN, content))
        return runs

    if not isinstance(content, list):
        return runs

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            runs.append((STYLE_USER if role == "user" else STYLE_PLAIN,
                         block.get("text", "")))
        elif btype == "thinking":
            continue  # not replayed: it is long and was never the point
        elif btype == "tool_use":
            # THE SAME LINE THE LIVE PATH WRITES, not a shorter one -- a
            # replayed call used to read `tool -> Write` while the live path
            # wrote `Write file_path: /home/.../plan.md`, so an old
            # conversation looked duller than the one you were having.
            # `telling_argument` (02.15) is the shared answer to "which
            # argument matters", keeping the two paths from drifting again.
            name = block.get("name", "?")
            args = block.get("input")
            told = _perms.telling_argument(args) if isinstance(args, dict) \
                else None
            runs.append((STYLE_TOOL,
                         f"{name} {told[0]}: {told[1][:160]}" if told
                         else name))
        elif btype == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "".join(b.get("text", "") for b in body
                               if isinstance(b, dict))
            text = str(body or "")[:500]
            if text:
                runs.append((STYLE_CODE, text + "\n"))
    return runs


def history_html(session_id: str, limit: int = 2000) -> list[str]:
    """Replay a stored session as HTML blocks, for repopulating the pages.

    Deliberately the same renderer the live path uses. Replaying stored text
    through a different one is exactly how an old conversation ended up flat
    while the conversation you were having had bullets, bold and code blocks.
    """
    try:
        msgs = get_session_messages(session_id, limit=limit)
    except Exception:
        log.exception("get_session_messages failed for %s", session_id)
        return []

    blocks: list[str] = []
    for m in msgs:
        msg = getattr(m, "message", None)

        # The compaction summary is not something the user said, and it opened
        # every restored conversation with several kilobytes of machine-written
        # recap in a user bubble -- on a 640x480 screen that is the whole first
        # page. One line instead. Nothing is lost that the reader had: the
        # summary exists for the MODEL's context, which resuming the session
        # restores regardless of what the transcript shows.
        summary = _compact_summary(msg)
        if summary is not None:
            blocks.append(H.notice(
                "Earlier conversation, condensed (%d characters, kept for "
                "the assistant and not shown here)." % len(summary)))
            continue

        # A fresh renderer per message: a stored message is complete, and
        # fence state must not leak from one into the next.
        md = HtmlStream()
        for style, text in _text_of(msg):
            if not text:
                continue
            if style == STYLE_USER:
                blocks.append(H.user_turn(text))
            elif style == STYLE_PLAIN:
                blocks.extend(u.html for u in md.feed(text)
                              if u.mode == APPEND and u.html)
            elif style == STYLE_TOOL:
                # Split back into the renderer's (name, summary) shape, so a
                # replayed call is the same markup as a live one -- the tool
                # name in `.toolname` and the argument after it, rather than
                # the literal word "tool" followed by everything.
                name, _, rest = text.partition(" ")
                blocks.append(H.tool_call(name, rest))
            elif style == STYLE_CODE:
                blocks.append('<div class="codewrap"><pre>'
                              + H.esc(text) + "</pre></div>")
            else:
                blocks.append("<p>" + H.esc(text) + "</p>")
        blocks.extend(u.html for u in md.flush()
                      if u.mode == APPEND and u.html)
    return blocks
