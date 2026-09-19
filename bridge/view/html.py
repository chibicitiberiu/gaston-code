"""Markdown -> HTML, incrementally, for the MSHTML transcript.

The guest hosts the HTML engine and does nothing else with the text: it takes
finished fragments and puts them in the document. All parsing, escaping and
styling happens here, which is the same division of labour the styled-run
renderer had -- only now the wire carries HTML instead of (style, text) pairs,
and the guest gets padding, borders, background tints and real lists for free.

Two shapes of update, because a transcript is both a log and a live thing:

  * ``append`` -- a finished block. It is inserted at the end of the
    transcript and never touched again.
  * ``live``   -- the block currently being written, re-rendered in place as
    more of it arrives. Without this a paragraph would only appear once it
    was complete, which reads as a stall rather than as streaming.

The incremental rule is the one that made the run renderer tractable and it
carries over unchanged: **only complete lines are classified.** Anything after
the last newline is held, because a line starting ``*`` might be a bullet or
might be the start of ``**bold**`` and you cannot tell yet.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass

APPEND = "append"
LIVE = "live"


@dataclass
class Update:
    mode: str
    html: str

    def __repr__(self) -> str:
        return f"Update({self.mode}, {self.html!r})"


_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEP = re.compile(r"^\s*:?-{2,}:?\s*$")

# Longest delimiter first, so ** beats *.
# Underscore emphasis only counts at a word boundary. Without the lookarounds,
# `BPB_RsvdSecCnt ... BPB_NumFATs` becomes one italic run from the first
# underscore to the last, which mangles exactly the identifiers this project
# talks about all day. Asterisk emphasis has no such restriction, which matches
# how every markdown implementation behaves.
_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*[^*\n]+\*\*|(?<![A-Za-z0-9_])__[^_\n]+__(?![A-Za-z0-9_]))"
    r"|(?P<em>\*[^*\n]+\*|(?<![A-Za-z0-9_])_[^_\n]+_(?![A-Za-z0-9_]))"
)


def esc(text: str) -> str:
    """Escape for HTML. Everything the model emits goes through this.

    Not optional and not only about tidiness: a transcript that renders
    ``<script>`` from model output would be executing it.
    """
    return _html.escape(text, quote=False)


def inline(text: str) -> str:
    """Inline spans within one line."""
    out = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(esc(text[pos:m.start()]))
        if m.group("code"):
            out.append("<code>" + esc(m.group("code")[1:-1]) + "</code>")
        elif m.group("bold"):
            # Recurse: `code` inside **bold** is common, and escaping the inner
            # span instead of parsing it puts literal backticks on screen.
            # The delimiters are stripped first, so this cannot re-match the
            # span it is already inside.
            out.append("<b>" + inline(m.group("bold")[2:-2]) + "</b>")
        else:
            out.append("<i>" + inline(m.group("em")[1:-1]) + "</i>")
        pos = m.end()
    if pos < len(text):
        out.append(esc(text[pos:]))
    return "".join(out)


def user_turn(text: str) -> str:
    """One user message, as its own block.

    The styled-run version drew a row of dashes and a ``>`` prefix because that
    was the most a bitmap-font transcript could manage. Here it is a tinted box
    with a rule down the side, which is what the dashes were imitating.
    """
    lines = text.strip().splitlines() or [""]
    return ('<div class="you">'
            + "<br>".join(esc(ln) for ln in lines)
            + "</div>")


#: A tool result is for the person watching, not for the model -- it already
#: has the whole thing. Long enough to be useful on a 640x480 screen and
#: short enough not to spend the guest's memory on a `dir /s`.
_TOOL_RESULT_MAX = 1200


def tool_call(name: str, summary: str) -> str:
    return ('<div class="tool"><span class="toolname">' + esc(name)
            + "</span> " + esc(summary) + "</div>")


def tool_result(body: str, *, error: bool = False) -> str:
    """What a tool actually returned, as a finished block.

    This used to go out as a raw TOOLRESULT frame carrying plain text, and the
    guest had NO handler for it -- so tool results have never appeared on
    Windows 98 at all. The user saw the call ("Bash: ls") and then silence,
    then whatever the model said next about output they could not see.

    Rendered here rather than there for the reason everything else is: the
    guest transforms nothing. Going through _write_block also puts it in the
    paginator, so it survives a reconnect and is still there when paging back
    -- which a raw frame never was.

    Capped, because a tool can return a great deal more than a 640x480 screen
    or a 32MB machine wants, and the model already has the full text.
    """
    body = (body or "").strip()
    if not body:
        body = "(no output)"
    clipped = body[:_TOOL_RESULT_MAX]
    out = ('<div class="toolresult' + (' err' if error else '') + '"><pre>'
           + esc(clipped) + "</pre>")
    if len(body) > _TOOL_RESULT_MAX:
        out += ('<div class="clip">-- shown to you in full; '
                + str(len(body) - _TOOL_RESULT_MAX)
                + " more characters were sent to the model --</div>")
    return out + "</div>"


def notice(text: str) -> str:
    return '<div class="notice">' + esc(text) + "</div>"


def error(text: str) -> str:
    return '<div class="err">' + esc(text) + "</div>"


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def _table(lines: list[str]) -> str:
    """Render a markdown table.

    The separator row (|---|---|) is what marks the row above it as a header;
    without one every row is a body row, which is what a model emitting a
    pipe-delimited list without a header expects.
    """
    rows = [_cells(l) for l in lines]
    header = None
    body = rows
    if len(rows) >= 2 and all(_SEP.match(c) for c in rows[1] if c):
        header, body = rows[0], rows[2:]

    out = ["<table>"]
    if header:
        out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in header)
                   + "</tr>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


class HtmlStream:
    """Feed model text in, get HTML updates out."""

    def __init__(self) -> None:
        self._pending = ""          # after the last newline, not yet classified
        self._lines: list[str] = []  # lines of the block being built
        self._kind: str | None = None
        self._lang = ""

    # --- public ------------------------------------------------------------

    def feed(self, text: str) -> list[Update]:
        self._pending += text
        if "\n" not in self._pending:
            return self._live_only()
        head, _, self._pending = self._pending.rpartition("\n")
        out: list[Update] = []
        for line in (head + "\n").splitlines():
            out.extend(self._line(line))
        out.extend(self._live_only())
        return out

    def flush(self) -> list[Update]:
        """The turn is over: close whatever is open."""
        out: list[Update] = []
        if self._pending:
            rest, self._pending = self._pending, ""
            out.extend(self._line(rest))
        closed = self._close()
        if closed:
            out.append(Update(APPEND, closed))
        out.append(Update(LIVE, ""))
        return out

    # --- internals ---------------------------------------------------------

    def _live_only(self) -> list[Update]:
        return [Update(LIVE, self._render(partial=True))]

    def _start(self, kind: str, lang: str = "") -> list[Update]:
        """Close the open block and begin a new one."""
        out: list[Update] = []
        closed = self._close()
        if closed:
            out.append(Update(APPEND, closed))
        self._kind = kind
        self._lang = lang
        self._lines = []
        return out

    def _close(self) -> str:
        if self._kind is None:
            return ""
        rendered = self._render(partial=False)
        self._kind = None
        self._lines = []
        return rendered

    def _line(self, raw: str) -> list[Update]:
        line = raw.rstrip("\n")

        m = _FENCE.match(line)
        if m:
            if self._kind == "pre":
                out = []
                closed = self._close()
                if closed:
                    out.append(Update(APPEND, closed))
                return out
            return self._start("pre", m.group(2))

        # Inside a fence everything is literal, which is why fence state has to
        # survive across feeds.
        if self._kind == "pre":
            self._lines.append(line)
            return []

        if not line.strip():
            out = []
            closed = self._close()
            if closed:
                out.append(Update(APPEND, closed))
            return out

        m = _RULE.match(line)
        if m:
            out = self._start("hr")
            out.append(Update(APPEND, "<hr>"))
            self._kind = None
            return out

        m = _HEADING.match(line)
        if m:
            out = self._start("h")
            level = min(len(m.group(1)) + 2, 6)
            out.append(Update(APPEND, f"<h{level}>{inline(m.group(2))}</h{level}>"))
            self._kind = None
            return out

        m = _ROW.match(line)
        if m:
            # A markdown table. Worth having now that the transcript can
            # actually draw one; the styled-run renderer could only ever have
            # shown the pipes.
            out = [] if self._kind == "table" else self._start("table")
            self._lines.append(line)
            return out

        m = _BULLET.match(line)
        if m:
            # Numbered markers get <ol>, so "1." does not silently become a
            # bullet and lose the numbering the model meant.
            kind = "ol" if m.group(2)[:1].isdigit() else "ul"
            out = [] if self._kind == kind else self._start(kind)
            self._lines.append(m.group(3))
            return out

        m = _QUOTE.match(line)
        if m:
            out = [] if self._kind == "quote" else self._start("quote")
            self._lines.append(m.group(1))
            return out

        out = [] if self._kind == "p" else self._start("p")
        self._lines.append(line)
        return out

    def _render(self, *, partial: bool) -> str:
        lines = list(self._lines)
        if partial and self._pending:
            lines.append(self._pending)
        if not lines:
            return ""

        if self._kind is None:
            # Text arrived before any complete line, so no block is open yet.
            # Show it as a paragraph anyway: this is the first thing the reader
            # sees of a turn, and holding it back until a newline arrives is
            # exactly the stall the live block exists to avoid.
            return "<p>" + "<br>".join(inline(x) for x in lines) + "</p>"

        if self._kind == "pre":
            body = esc("\n".join(lines))
            label = (f'<div class="lang">{esc(self._lang)}</div>'
                     if self._lang else "")
            return f'<div class="codewrap">{label}<pre>{body}</pre></div>'
        if self._kind in ("ul", "ol"):
            tag = self._kind
            return (f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in lines)
                    + f"</{tag}>")
        if self._kind == "table":
            return _table(lines)
        if self._kind == "quote":
            return "<blockquote>" + "<br>".join(inline(x) for x in lines) + "</blockquote>"
        if self._kind == "p":
            return "<p>" + "<br>".join(inline(x) for x in lines) + "</p>"
        return ""


# --- the document the guest hosts ------------------------------------------
#
# Colours lean on the Win98 system palette. The display is 8bpp, so a colour
# outside the 20 system entries is dithered, and large flat areas of a dithered
# tint look like noise. White, silver (#C0C0C0), grey (#808080) and navy
# (#000080) are exact; the two light tints are close enough to white and silver
# to dither cleanly.
#
# Sizes are in points against an 8pt UI font, because the whole layout has to
# work in a 470px column on a 640x480 screen.
STYLESHEET = """
/* Verdana, not MS Sans Serif, and the reason is italics.
   MS Sans Serif is a bitmap .FON with no italic face, so GDI fakes one by
   shearing the glyph grid -- which is what made emphasis look broken. It is
   not a smoothing problem, and smoothing is unavailable at 8bpp anyway.
   Measured on the guest: C:\WINDOWS\FONTS carries VERDANA, VERDANAB,
   VERDANAI and VERDANAZ, so the italic and bold-italic faces are real ones.
   Verdana was drawn for small unsmoothed screen type, which is exactly this.
   (Tahoma would not do: it ships bold only, same defect.)
   MS Sans Serif stays as the fallback for a guest without IE's font set. */
body { font: 8pt Verdana, 'MS Sans Serif', sans-serif; background: #ffffff;
       color: #000000; margin: 0; padding: 6px 9px 24px 9px; }
p  { margin: 0 0 7px 0; line-height: 1.35; }
h3, h4, h5, h6 { font-size: 9pt; margin: 12px 0 5px 0; padding-bottom: 2px;
       border-bottom: 1px solid #c0c0c0; }
b { font-weight: bold; }
i { font-style: italic; }

/* A user turn. The styled-run transcript drew a row of dashes and a '>' to
   imitate exactly this, because it had no other way to box anything. */
.you { background: #e8e8f0; border-left: 3px solid #000080;
       padding: 5px 7px; margin: 12px 0 9px 0; color: #000080;
       font-weight: bold; }

/* Code. The label sits on the block rather than floating above it as the
   bracketed "[c]" used to. */
.codewrap { margin: 7px 0; border: 1px solid #c0c0c0; background: #f4f4f4;
             overflow: auto; width: 100%; }
.lang { font-size: 7pt; color: #808080; background: #ececec;
        border-bottom: 1px solid #d8d8d8; padding: 1px 5px; }
/* The code block scrolls inside its own box. Without `overflow: auto` on the
   wrapper a single long line gives the whole transcript a horizontal
   scrollbar, and then every paragraph is being panned to read one line of C.
   IE5 has no pre-wrap and no word-wrap, so scrolling the box is the option
   that exists. */
pre { font: 8pt 'Courier New', monospace; color: #000080;
      margin: 0; padding: 5px 6px; white-space: pre; }
code { font: 8pt 'Courier New', monospace; color: #000080;
       background: #f4f4f4; padding: 0 2px; }

table { border-collapse: collapse; margin: 8px 0; font-size: 8pt; }
th, td { border: 1px solid #c0c0c0; padding: 2px 6px; text-align: left;
         vertical-align: top; }
th { background: #e8e8f0; font-weight: bold; }

ul, ol { margin: 5px 0 8px 0; padding-left: 22px; }
li { margin: 2px 0; line-height: 1.3; }
blockquote { margin: 7px 0; padding: 3px 8px; border-left: 2px solid #c0c0c0;
             color: #505050; }
hr { border: none; border-top: 1px solid #c0c0c0; margin: 10px 0; }

.tool { font: 8pt 'Courier New', monospace; color: #006000;
        background: #f2f7f2; border-left: 2px solid #008000;
        padding: 3px 6px; margin: 6px 0; }
.toolname { font-weight: bold; }
/* `overflow: auto` AND `width: 100%`, the same pair as .codewrap above and
   for the same reason -- PROGRESS.md records that the width was required, not
   decorative. This block used to have neither, while its `pre` asked for
   `pre-wrap` and `word-wrap`, which the comment twenty lines up says IE5 does
   not have. So command output could not wrap and had no box to scroll inside,
   and the whole TRANSCRIPT got the horizontal scrollbar -- which then stayed
   for the rest of the conversation. The first line of a capped result reads
   "Output was capped at 32KB by the connected machine and trun", cut off at
   the right edge: the most important line in the block was the one you could
   not read.

   The two wrapping declarations are kept. `word-wrap: break-word` is a
   Microsoft extension that shipped in IE 5.5, and the guest's mshtml version
   is not pinned anywhere in this repo -- so on 5.5 they wrap and the box
   never needs to scroll, and on 5.0 they are ignored and it does. Either way
   the document no longer moves. */
.toolresult { font: 8pt 'Courier New', monospace; color: #303030;
              background: #f4f4f4; border-left: 3px solid #b0b0b0;
              padding: 2px 6px; margin: 2px 0 6px 0;
              overflow: auto; width: 100%; }
.toolresult pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; }
.toolresult.err { color: #a00000; background: #fff0f0;
                  border-left-color: #a00000; }
.clip { color: #808080; font-style: normal; margin-top: 2px; }
.notice { color: #808080; margin: 6px 0; }
.err { color: #a00000; background: #fff0f0; border-left: 3px solid #a00000;
       padding: 4px 6px; margin: 7px 0; font-weight: bold; }

/* A permission request. Amber rather than red: this is a question, not a
   failure, and the guest is asking rather than reporting. */
.perm { background: #fffbe6; border: 1px solid #e0c060; border-left: 3px solid #c08000;
        padding: 5px 7px; margin: 9px 0; }
.permhead { font-weight: bold; color: #805000; font-size: 7pt;
            text-transform: uppercase; margin-bottom: 3px; }
.perm table { margin: 4px 0 0 0; }
.perm th { background: #f6eccc; font-weight: normal; color: #604000;
           white-space: nowrap; vertical-align: top; }
.perm td { font-family: 'Courier New', monospace; }
"""

# Two containers: finished blocks accumulate in #t, and #l holds the one block
# still being written, replaced wholesale on each update. Keeping them separate
# is what lets the guest stream without ever re-parsing what is already there.
SKELETON = ("<html><head>"
            "<meta http-equiv=\"Content-Type\" "
            "content=\"text/html; charset=windows-1252\">"
            "<style>%s</style></head>"
            "<body><div id=\"t\"></div><div id=\"l\"></div></body></html>")


def document(css: str | None = None) -> str:
    return SKELETON % (css if css is not None else STYLESHEET)
