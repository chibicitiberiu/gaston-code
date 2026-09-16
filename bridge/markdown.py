"""Markdown -> styled runs, incrementally.

The guest never sees markdown. It receives (style, text) runs and looks style
up in a static CHARFORMAT2 table -- that is the whole of its formatting logic.
Parsing here rather than in C on a 32MB machine is the single biggest scope cut
in the project.

Incremental parsing needs care because markdown is not parseable without
lookahead: `*` might open emphasis or be a literal asterisk, and you cannot
tell until more text arrives. The rule that makes this tractable is that the
parser only ever commits **complete lines**. Anything after the last newline is
held until the next feed, or until `flush()` says the turn is over.

That is also exactly what the coalescer wants, and what the compressor wants.
One policy, three reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Must match rc_style in win/transcript.h.
STYLE_PLAIN = "plain"
STYLE_CODE = "code"
STYLE_BOLD = "bold"
STYLE_THINKING = "thinking"
STYLE_TOOL = "tool"
STYLE_ERROR = "error"
STYLE_USER = "user"


@dataclass
class Run:
    style: str
    text: str

    def __repr__(self) -> str:  # tests read much better with this
        return f"Run({self.style}, {self.text!r})"


_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")

# Inline spans, longest-delimiter-first so ** wins over *.
_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*[^*\n]+\*\*|__[^_\n]+__)"
    r"|(?P<em>\*[^*\n]+\*|_[^_\n]+_)"
)


class MarkdownStream:
    """Feed text in, get styled runs out.

    Stateful across calls: a fenced code block opened in one feed stays open
    until closed in a later one.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._in_fence = False

    def feed(self, text: str) -> list[Run]:
        """Consume text, emitting runs for every COMPLETE line in it."""
        self._pending += text
        if "\n" not in self._pending:
            return []
        head, _, self._pending = self._pending.rpartition("\n")
        return self._parse_block(head + "\n")

    def flush(self) -> list[Run]:
        """Turn is over: emit whatever is left, complete line or not."""
        if not self._pending:
            return []
        rest, self._pending = self._pending, ""
        return self._parse_block(rest)

    # --- internals ---------------------------------------------------------

    def _parse_block(self, text: str) -> list[Run]:
        runs: list[Run] = []
        for line in text.splitlines(keepends=True):
            runs.extend(self._parse_line(line))
        return _merge(runs)

    def _parse_line(self, line: str) -> list[Run]:
        stripped = line.rstrip("\n")
        eol = line[len(stripped):]

        m = _FENCE.match(stripped)
        if m:
            # The fence markers themselves are not shown -- a bitmap-font
            # transcript reads better without ``` on its own line.
            self._in_fence = not self._in_fence
            if self._in_fence and m.group(2):
                return [Run(STYLE_TOOL, f"[{m.group(2)}]{eol}")]
            return []

        if self._in_fence:
            # Inside a fence everything is literal, including things that look
            # like emphasis. This is why fence state has to survive across
            # feeds.
            return [Run(STYLE_CODE, line)]

        m = _HEADING.match(stripped)
        if m:
            return [Run(STYLE_BOLD, m.group(2) + eol)]

        m = _QUOTE.match(stripped)
        if m:
            return [Run(STYLE_THINKING, "| " + m.group(1) + eol)]

        m = _BULLET.match(stripped)
        if m:
            indent, _marker, rest = m.groups()
            runs = [Run(STYLE_PLAIN, f"{indent}* ")]
            runs.extend(_inline(rest))
            runs.append(Run(STYLE_PLAIN, eol))
            return runs

        runs = _inline(stripped)
        if eol:
            runs.append(Run(STYLE_PLAIN, eol))
        return runs


def _inline(text: str) -> list[Run]:
    """Split one line into inline spans."""
    runs: list[Run] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            runs.append(Run(STYLE_PLAIN, text[pos:m.start()]))
        if m.group("code"):
            runs.append(Run(STYLE_CODE, m.group("code")[1:-1]))
        elif m.group("bold"):
            runs.append(Run(STYLE_BOLD, m.group("bold")[2:-2]))
        else:
            # No italic style on the guest: RichEdit could do it, but italic
            # bitmap text at 8pt on a 640x480 CRT is genuinely hard to read.
            runs.append(Run(STYLE_PLAIN, m.group("em")[1:-1]))
        pos = m.end()
    if pos < len(text):
        runs.append(Run(STYLE_PLAIN, text[pos:]))
    return runs


def _merge(runs: list[Run]) -> list[Run]:
    """Coalesce adjacent same-style runs.

    Worth doing: each run costs an EM_SETCHARFORMAT plus an EM_REPLACESEL on
    the guest, and that pair is the expensive path in RichEdit.
    """
    out: list[Run] = []
    for r in runs:
        if not r.text:
            continue
        if out and out[-1].style == r.style:
            out[-1] = Run(r.style, out[-1].text + r.text)
        else:
            out.append(r)
    return out
