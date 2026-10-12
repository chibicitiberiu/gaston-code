"""The shim over the split, and the stylesheet that has not moved yet.

WHAT HAPPENED HERE. This file was the whole transcript renderer: the markdown
parser, the HTML emitter and the stylesheet in one place. Stage 3 moves the
emitter to the client, so the parser and the emitter had to stop being the
same object first. They are now:

  * `bridge/view/blocks.py`      what the model said, as structure. No markup.
  * `bridge/view/htmlrender.py`  blocks to HTML. The oracle the guest's
                                 renderer is proved against, byte for byte.

and this file is what is left plus a re-export of everything its importers
already name. Nothing here is new behaviour; `tests/python/view/test_html.py`
passes unchanged, all 42 of it, which is the point of doing the split as its
own commit.

WHAT IS STILL REALLY HERE: `STYLESHEET`, `WIRE_CSS`, `EXPORT_CSS`, `SKELETON`,
`document` and `export_document`. Those leave at C3 and C4, when the guest
composes its own document from files beside the exe. They are not moved into
either new module because both of their destinations are off this machine.

The incremental rule that made the parser tractable is in `blocks.py` now, but
it is worth repeating where people will look for it: **only complete lines are
classified.** Anything after the last newline is held, because a line starting
``*`` might be a bullet or might be the start of ``**bold**`` and you cannot
tell yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..files import caps as C
from ..wire import fold
from . import blocks as _blocks
from . import htmlrender as _render
from .blocks import (  # noqa: F401  -- re-exported for importers
    _BULLET,
    _FENCE,
    _HEADING,
    _INLINE,
    _QUOTE,
    _ROW,
    _RULE,
    _SEP,
    APPEND,
    LIVE,
    Block,
    BlockStream,
    BlockUpdate,
)
from .htmlrender import esc, inline, render  # noqa: F401

#: The old private names, kept because tests and probes import them by these
#: spellings and A4 is a split, not a rename. They go as their callers do:
#: `_TOOL_WRAP_COL` and `_fold_long_lines` with the wrap at C2, when the
#: client takes over wrapping; `_TOOL_RESULT_MAX` stays on the bridge.
_TOOL_WRAP_COL = _render.TOOL_WRAP_COL
_TOOL_RESULT_MAX = _blocks.TOOL_RESULT_MAX
_fold_long_lines = _render.fold_long_lines
_cells = _blocks.cells


@dataclass
class Update:
    mode: str
    html: str

    def __repr__(self) -> str:
        return f"Update({self.mode}, {self.html!r})"


def _table(lines: list[str]) -> str:
    """Kept as a string-in, string-out helper for the importers that call it.

    The real work is a parse (blocks.table_block) followed by a render
    (htmlrender), which is the whole shape of the stage in one function.
    """
    return _render.render(_blocks.table_block(lines))


def user_turn(text: str) -> str:
    return _render.render(_blocks.user_turn(text))


def notice(text: str) -> str:
    return _render.render(_blocks.notice(text))


def error(text: str) -> str:
    return _render.render(_blocks.error(text))


def tool_call(name: str, summary: str) -> str:
    return _render.render(_blocks.tool_call(name, summary))


def tool_result(body: str, *, error: bool = False) -> str:
    return _render.render(_blocks.tool_result(body, error=error))


class HtmlStream:
    """Feed model text in, get HTML updates out.

    A `BlockStream` with the renderer bolted on, which is exactly what the
    bridge stops being at C2: the guest will take the records and run the
    renderer itself. Until then this keeps every caller and every golden
    working, and `htmlrender` keeps being exercised by the whole test suite
    rather than only by the new tests -- which is what makes it a trustworthy
    oracle for B2.
    """

    #: Here as well as on BlockStream because `html.py:368` is cited in four
    #: places and imported by name in two.
    LIVE_CAP = _blocks.BlockStream.LIVE_CAP

    def __init__(self) -> None:
        self._s = _blocks.BlockStream()

    def feed(self, text: str) -> list[Update]:
        return [self._as_update(u) for u in self._s.feed(text)]

    def flush(self) -> list[Update]:
        return [self._as_update(u) for u in self._s.flush()]

    @staticmethod
    def _as_update(u: BlockUpdate) -> Update:
        return Update(u.mode,
                      _render.render(u.block) if u.block is not None else "")


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
   Measured on the guest: its FONTS directory carries VERDANA, VERDANAB,
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

/* A message the guest has echoed but the bridge has not yet acknowledged by
   starting a turn. Drawn back: hollow left rule, grey text, no bold. On a
   LAN that state lasts a moment; on the 5 KB/s dialup link this project also
   targets it is seconds, and "did that send?" is the question it answers.
   The class is dropped in place when TURN state=start arrives, so the block
   does not move or re-render -- it just stops looking provisional. */
.you.pending { background: #f4f4f8; border-left-color: #a0a0b8;
               color: #606078; font-weight: normal; }

/* Code. The label sits on the block rather than floating above it as the
   bracketed "[c]" used to. */
.codewrap { margin: 7px 0; border: 1px solid #c0c0c0; background: #f4f4f4;
             overflow: auto; width: 100%;
             height: expression(this.scrollWidth > this.clientWidth
                                ? this.scrollHeight + 18 : 'auto'); }
/* THE HEIGHT EXPRESSION, on this box and on .toolresult below. IE 5.0 sizes
   an `overflow: auto` box to its content and then draws the horizontal
   scrollbar INSIDE that height, on top of the only line there is -- with
   this stylesheet, not in a bare test page (measured on the Athlon,
   2026-10-03, MSIE 5.0: a code block and a tool result that were nothing
   but a scrollbar). The expression adds the scrollbar's height only when
   the box actually scrolls, so a short block keeps its own size. Tried
   and rejected there: overflow-x/-y alone, `width: auto` (the whole
   transcript scrolls), and a fixed padding-bottom (every block grows). */
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

   The two wrapping declarations are kept and are NOT relied on.
   `word-wrap: break-word` is a Microsoft extension that shipped in IE 5.5
   and the guest's mshtml version is not pinned anywhere in this repo, so on
   5.0 both are ignored -- `pre-wrap` is not a value it knows, which drops
   the declaration and leaves the `pre` on plain `white-space: pre`.

   That is not a cosmetic difference. A `pre` that cannot wrap holds one
   enormous line, `overflow: auto` gives the box a horizontal scrollbar, and
   IE draws that scrollbar INSIDE a box whose height is exactly one line --
   so it sits on top of the only text there is. Observed on the guest: a
   block that was nothing but a scrollbar.

   So the lines are broken on the BRIDGE now, by `_fold_long_lines`, which
   is where every other transformation already happens. These two stay for a
   newer guest and as a second line of defence; nothing depends on them. */
.toolresult { font: 8pt 'Courier New', monospace; color: #303030;
              background: #f4f4f4; border-left: 3px solid #b0b0b0;
              padding: 2px 6px; margin: 2px 0 6px 0;
              overflow: auto; width: 100%;
              height: expression(this.scrollWidth > this.clientWidth
                                 ? this.scrollHeight + 18 : 'auto'); }
.toolresult pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; }
.toolresult.err { color: #a00000; background: #fff0f0;
                  border-left-color: #a00000; }
.clip { color: #808080; font-style: normal; margin-top: 2px; }
.notice { color: #808080; margin: 6px 0; }

/* The status line, under everything. Grey like a notice because it is the
   same kind of thing -- the app talking about itself -- but it does not
   scroll away, it is replaced. The spinner is a real .gif file beside the
   document: IE5 animates GIFs but has no data: URI support, so it cannot be
   inlined. If the file is missing the alt text is empty and the line still
   reads correctly, which is why the wording never depends on it. */
.status { color: #808080; margin: 6px 0; }
.status img { vertical-align: middle; margin-right: 5px; }
.err { color: #a00000; background: #fff0f0; border-left: 3px solid #a00000;
       padding: 4px 6px; margin: 7px 0; font-weight: bold; }

/* A permission request. Amber rather than red: this is a question, not a
   failure, and the guest is asking rather than reporting. */
.perm { background: #fffbe6; border: 1px solid #e0c060; border-left: 3px solid #c08000;
        padding: 5px 7px; margin: 9px 0; }
.permhead { font-weight: bold; color: #805000; font-size: 7pt;
            text-transform: uppercase; margin-bottom: 3px; }
.perm table { margin: 4px 0 0 0; }
/* The command guard's finding, in the one place there is room to read it.
   The bar is a single clipped line; this is not. See 02.4. */
.permwarn { margin: 4px 0 0 0; color: #803000; }
.permdone .permwarn { color: #706050; }
.perm th { background: #f6eccc; font-weight: normal; color: #604000;
           white-space: nowrap; vertical-align: top; }
.perm td { font-family: 'Courier New', monospace; }

/* The same request, once it has been answered. A long session used to be
   mostly amber: every approval stayed a full argument table forever, and on
   a 640x480 screen a handful of them is the whole page.

   A DIFFERENT CLASS, not `.perm.done`. IE5 and IE6 read a compound class
   selector as its LAST class only, so `.perm.done` would style every `.perm`
   -- which is the kind of bug that looks like the rule not working and is
   actually the rule working on everything.

   The table is hidden rather than removed, and the block keeps its place in
   the transcript: what was asked and what was answered is a record, and a
   record that deletes itself once it stops being urgent is not one. Clicking
   is not needed to see it again -- paging back to a sealed page re-renders
   the original, because the paginator holds what was written and nothing
   rewrites history there. */
.permdone { background: #fbfbf4; border: 1px solid #d8d8c8;
            border-left: 3px solid #b0b090; padding: 3px 7px; margin: 6px 0; }
.permdone table { display: none; }
.permdone .permhead { display: none; }
.perm .permdonehead { display: none; }
.permdonehead { font-weight: bold; color: #707060; font-size: 7pt;
                text-transform: uppercase; margin-bottom: 2px; }
.permdone p { margin: 0; color: #606050; }
"""

#: What actually goes on the wire: the stylesheet with its comments taken out.
#:
#: Half of STYLESHEET is comments -- 5,574 bytes of 9,375 in eleven blocks, the
#: largest of them the 1,643-byte note about `overflow: auto`. They are the only
#: record of why several of those rules are the way they are, and three of them
#: are measurements taken on the guest, so they stay in the source. They have no
#: business on a 1998 link: `doc()` builds a plain Frame with no `enc` header, so
#: unlike TEXT and export data the DOC frame is never compressed, and those bytes
#: go out verbatim once per connect and again on every reconnect. On a 5 KB/s
#: link that is close to a full second between plugging in and seeing anything.
#:
#: Rules are untouched; only comments and the blank lines they leave go. The
#: guard that every rule survived is tests/python/meta/test_guest_css.py, which
#: asserts document() still carries every RC_CSS_* class the guest defines.
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _without_comments(css: str) -> str:
    stripped = (ln.rstrip() for ln in _CSS_COMMENT.sub("", css).split("\n"))
    return "\n".join(ln for ln in stripped if ln) + "\n"


WIRE_CSS = _without_comments(STYLESHEET)


# Three containers: finished blocks accumulate in #t, #l holds the one block
# still being written (replaced wholesale on each update), and #s is the
# status line that sits under both. Keeping them separate is what lets the
# guest stream without ever re-parsing what is already there.
#
# #s is written by the GUEST, not from here, and that is not a breach of "the
# guest transforms nothing". It renders no bridge content: it says what the
# app itself is doing -- thinking, receiving, how long -- which is the same
# thing it has always put in the status bar, just somewhere the eye already
# is. The bridge supplies the slot and the stylesheet; the guest supplies the
# one line about its own state.
#
# A guest built against a two-slot skeleton must still work, because the
# bridge's DOC is authoritative and the two can be different versions. The
# guest treats #s as optional for exactly that reason.
SKELETON = ("<html><head>"
            "<meta http-equiv=\"Content-Type\" "
            "content=\"text/html; charset=%s\">"
            "<style>%s</style></head>"
            "<body><div id=\"t\"></div><div id=\"l\"></div>"
            "<div id=\"s\"></div></body></html>")


def document(css: str | None = None) -> str:
    """The live view's skeleton.

    The charset is the connection's, not a literal: `link.send_html` encodes
    this document's updates with `fold`, and a <meta> naming a different
    table than the bytes were written with is mojibake by construction.
    """
    return SKELETON % (C.html_charset(fold.codec()),
                       css if css is not None else WIRE_CSS)


def export_document(title: str, body: str, when: str = "") -> str:
    """The whole transcript as one standalone file.

    NOT `document()` with the body filled in. That one is the live view: it
    has the `#l` and `#s` slots the streaming path replaces, and an exported
    file carrying two empty divs named after a protocol nobody reading the
    file has heard of is just litter. This has a heading, the conversation,
    and nothing that only makes sense while connected.

    Same stylesheet, deliberately: an export that looks different from what
    was on screen invites the question of which one is right. It also has to
    render in IE5 on the machine that saved it, which is the same constraint
    the stylesheet was already written to. WIRE_CSS rather than STYLESHEET for
    the same reason the live view uses it -- the comments explain the rules to
    whoever maintains them, not to whoever reads the transcript -- and here it
    is free either way, since export data is compressed.
    """
    head = '<div class="exporthead"><h1>' + esc(title or "Conversation")
    head += "</h1>"
    if when:
        head += '<div class="notice">' + esc(when) + "</div>"
    head += "</div>"
    return ("<html><head>"
            "<meta http-equiv=\"Content-Type\" "
            "content=\"text/html; charset=" + C.html_charset(fold.codec())
            + "\">"
            "<title>" + esc(title or "Conversation") + "</title>"
            "<style>" + WIRE_CSS + EXPORT_CSS + "</style></head>"
            "<body>" + head + body + "</body></html>")


#: Only what the live view has no use for. Kept separate so the shared
#: stylesheet stays exactly what the guest is running.
EXPORT_CSS = (
    ".exporthead { border-bottom: 1px solid #808080; margin-bottom: 12px; }"
    ".exporthead h1 { font-size: 12pt; margin: 0 0 2px 0; }"
)
