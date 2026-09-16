"""Session listing and history replay for the chat list.

One live ClaudeSDKClient at a time: switching chats closes the current one and
reopens with resume=<id>. The list is read-only history. A warm client pool is
roughly three times the work and nobody notices on a single-user box.

Transcripts live at ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl, but
that format is internal and changes between releases. Use the SDK accessors.
"""

from __future__ import annotations

import logging

from claude_agent_sdk import get_session_messages, list_sessions

from .markdown import STYLE_CODE, STYLE_PLAIN, STYLE_TOOL, STYLE_USER

log = logging.getLogger("bridge.sessions")


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
        out.append({
            "id": r.session_id,
            "title": title,
            "cwd": getattr(r, "cwd", "") or "",
            "modified": str(getattr(r, "last_modified", "") or ""),
            "bytes": str(getattr(r, "file_size", "") or ""),
        })
    return out


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
            runs.append((STYLE_TOOL, f"\n  -> {block.get('name', '?')}\n"))
        elif btype == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "".join(b.get("text", "") for b in body
                               if isinstance(b, dict))
            text = str(body or "")[:500]
            if text:
                runs.append((STYLE_CODE, text + "\n"))
    return runs


def history_runs(session_id: str, limit: int = 2000) -> list[tuple[str, str]]:
    """Replay a stored session as styled runs, for repopulating the pages."""
    try:
        msgs = get_session_messages(session_id, limit=limit)
    except Exception:
        log.exception("get_session_messages failed for %s", session_id)
        return []

    runs: list[tuple[str, str]] = []
    for m in msgs:
        msg = getattr(m, "message", None)
        for style, text in _text_of(msg):
            if text:
                runs.append((style, text))
        if getattr(m, "type", "") == "user":
            runs.append((STYLE_PLAIN, "\n"))
    return runs
