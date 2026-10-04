"""What a destructive machine call will touch, and -- in Auto -- a second
opinion on whether it should.

Two jobs, both in front of the permission bar:

* **The preview.** "Delete C:\\WORK\\OLD" is not something a person can
  approve; "C:\\WORK\\OLD to the Recycle Bin -- 132 files in 14 folders,
  3.2 MB: a.txt, b\\c.txt ..." is. Before a delete, copy or move reaches the
  bar, the guest is asked what is actually there (FSSTATS), and for a copy or
  move that may replace something, what is at the destination.

* **The reviewer, in Auto mode only.** The user asked for it in those words:
  "in auto mode -- literally asking an agent if this is safe". Auto used to
  mean nothing here: with the permission callback installed, the CLI's own
  classifier is never consulted, so every machine call still asked the
  person (services/options.py, MODE_CAVEAT). Now a separate model reads the
  person's recent requests, the call and its preview, and answers SAFE or
  ASK. SAFE goes ahead without a bar. ASK -- and anything that goes wrong
  while asking: a timeout, an unreadable answer -- falls through to the
  person, with the reviewer's reason on the bar. It can never deny on its
  own and never approves by default: the person stays the backstop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from . import prompts

log = logging.getLogger("bridge.review")

#: How long the bar waits for the guest to count a tree before showing
#: without a preview. A preview is advice; a hung count must not hold the
#: question.
PREVIEW_S = 20.0

#: And for the reviewer's answer. Past this the person is asked.
REVIEW_S = 60.0

REVIEW_MODEL = "haiku"

#: The machine tools whose preview is a count of what they touch.
TREE_TOOLS = ("mcp__machine__delete", "mcp__machine__copy",
              "mcp__machine__move")


def _mb(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, n // 1024)} KB" if n else "0 KB"
    return f"{n / (1024 * 1024):.1f} MB"


def describe(stats: dict, names: list[str]) -> str:
    """One stats reply in words: "a folder of 132 files in 14 folders,
    3.2 MB: a.txt, b\\c.txt, ..." or "a file, 12 KB"."""
    files = int(stats.get("files", 0) or 0)
    dirs = int(stats.get("dirs", 0) or 0)
    size = _mb(int(stats.get("bytes", 0) or 0))
    if stats.get("how") == "file":
        return f"a file, {size}"
    text = (f"a folder of {files} file{'s' if files != 1 else ''}"
            f"{f' in {dirs} folders' if dirs else ''}, {size}")
    if names:
        shown = ", ".join(n.replace("/", "\\") for n in names[:4])
        more = ", ..." if files + dirs > len(names[:4]) else ""
        text += f": {shown}{more}"
    return text


async def preview(fs, guest, tool_name: str, args: dict) -> str:
    """What a delete, copy or move is about to touch, or "" when it is not
    one of those or the guest cannot say in time."""
    if fs is None or guest is None or tool_name not in TREE_TOOLS:
        return ""
    if getattr(getattr(guest, "caps", None), "ops", 0) < 2:
        return ""
    path = str((args or {}).get("path") or "").strip()
    if not path:
        return ""
    parts: list[str] = []
    try:
        stats, names = await asyncio.wait_for(fs.stats(guest, path),
                                              PREVIEW_S)
        parts.append(f"{path} is {describe(stats, names)}")
    except Exception as exc:                  # advice only: never block on it
        log.info("preview of %s failed: %s", path, exc)
        return ""
    to = str((args or {}).get("to") or "").strip()
    if to and (args or {}).get("overwrite"):
        try:
            stats, names = await asyncio.wait_for(fs.stats(guest, to),
                                                  PREVIEW_S)
            parts.append(f"and it may REPLACE what is at {to}, "
                         f"{describe(stats, names)}")
        except Exception:                     # nothing there to replace
            pass
    return "; ".join(parts)


#: Arguments that carry DATA rather than a choice -- what a file will hold,
#: what will be typed. Fenced as untrusted and the only thing ever cut short.
#: The old serialisation, `json.dumps(args)[:1500]`, cut wherever 1500 fell,
#: and the model picks key order: a `put_file` with its content first hid
#: the destination from the reviewer entirely (red team, 2026-10-04).
CONTENT_KEYS = ("content", "new_string", "old_string", "steps", "text")
CONTENT_CAP = 1500
VALUE_CAP = 400


def action_text(tool_name: str, args: dict, finding: str = "",
                category: str = "") -> str:
    """The action as the reviewer reads it: what the classifier found, then
    every argument on its own line, then the data, fenced."""
    lines = [prompts.text("review.tool", tool=tool_name.replace(
        "mcp__machine__", ""))]
    if category:
        lines.append(prompts.text("review.category", category=category))
    if finding:
        lines.append(prompts.text("review.finding", finding=finding))
    data = []
    for key, value in (args or {}).items():
        if key.startswith("_"):
            continue
        if key in CONTENT_KEYS:
            text = (value if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False))
            data.append((key, text[:CONTENT_CAP]))
            continue
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False)
        lines.append(f"{key}: {text[:VALUE_CAP]}")
    for key, text in data:
        lines.append(prompts.text("review.fenced", key=key, text=text))
    return "\n".join(lines)


async def review(guest, tool_name: str, args: dict,
                 preview_text: str = "", *, finding: str = "",
                 category: str = "") -> tuple[bool, str]:
    """(approved, reason). Never raises; any failure is (False, why)."""
    requests: list[str] = [str(r) for r in
                           getattr(guest, "recent_prompts", ()) or ()]
    first = str(getattr(guest, "first_prompt", "") or "")
    if not requests and first:
        requests = [first]
    action = action_text(tool_name, args, finding, category)
    parts = [prompts.text("review.requests", requests="\n".join(
                 f"- {r[:600]}" for r in requests[-3:])),
             prompts.text("review.action", action=action)]
    if preview_text:
        parts.append(prompts.text("review.touches", preview=preview_text))
    try:
        text = await asyncio.wait_for(_ask("\n\n".join(parts)), REVIEW_S)
    except Exception as exc:
        log.warning("reviewer failed: %s", exc)
        return False, prompts.text("review.unreachable")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    verdict = lines[0].upper() if lines else ""
    reason = lines[1] if len(lines) > 1 else ""
    if verdict.startswith("SAFE"):
        return True, reason or prompts.text("review.consistent")
    return False, reason or prompts.text("review.unsure")


def _options():
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(model=REVIEW_MODEL,
                              system_prompt=prompts.text("review.system"),
                              tools=[], allowed_tools=[], max_turns=1,
                              setting_sources=[], strict_mcp_config=True)


class _Warm:
    """One reviewer process started AHEAD of need.

    Every review used to be a fresh `query()`: a CLI cold start, several
    seconds, before the model read a word -- and Auto sends every call it
    does not allow outright through here. A connected client waits idle
    instead; each review uses it for ONE question and then it is closed and
    the next one started in the background, so no review ever sees another
    review's conversation.
    """

    def __init__(self) -> None:
        self._next: asyncio.Task | None = None

    def _start(self) -> asyncio.Task:
        async def connect():
            from claude_agent_sdk import ClaudeSDKClient
            client = ClaudeSDKClient(options=_options())
            await client.connect()
            return client
        return asyncio.ensure_future(connect())

    async def ask(self, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, TextBlock
        task, self._next = self._next or self._start(), None
        client = await task
        try:
            await client.query(prompt)
            out: list[str] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    out += [b.text for b in msg.content
                            if isinstance(b, TextBlock)]
            return "\n".join(out)
        finally:
            self._next = self._start()
            try:
                await client.disconnect()
            except Exception:                     # it answered; that is all
                log.debug("reviewer disconnect", exc_info=True)

    def warm(self) -> None:
        """Start the next reviewer now, if none is starting."""
        if self._next is None:
            self._next = self._start()


_WARM = _Warm()


def warm() -> None:
    """Called when a mode that checks is chosen, so the first check of the
    session does not pay the cold start either."""
    with contextlib.suppress(RuntimeError):       # no running loop (tests)
        _WARM.warm()


async def _ask(prompt: str) -> str:
    """One answer from a model with no tools. Separate so tests replace it."""
    return await _WARM.ask(prompt)
