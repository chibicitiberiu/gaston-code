"""The shim over the split. Nothing of the transcript is built here.

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

NOTHING IS LEFT OF THE DOCUMENT. `STYLESHEET`, `WIRE_CSS`, `SKELETON` and
`document` went at C4 with the DOC frame; `export_document`, `EXPORT_CSS`
and `esc` went at C3 with the export. None of them were moved into the two
new modules, because their destination was never this machine: the
stylesheet is win/resources/view.css and the markup is view.tpl, both
beside the exe.

What is left here is a handful of re-exports and `HtmlStream`, which is
`BlockStream` with the oracle bolted on. That goes too, with the oracle,
when pagination lands.

The incremental rule that made the parser tractable is in `blocks.py` now, but
it is worth repeating where people will look for it: **only complete lines are
classified.** Anything after the last newline is held, because a line starting
``*`` might be a bullet or might be the start of ``**bold**`` and you cannot
tell yet.
"""

from __future__ import annotations

from dataclasses import dataclass

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
from .htmlrender import render  # noqa: F401

#: The one cap that STAYS on the bridge, and the line is bandwidth against
#: appearance: truncating on the client means the bytes already crossed a
#: 5 KB/s link. The wrap column left at C2 with the renderer -- the client
#: knows its own window's width and this one was measured once in a
#: 640x480 default that a resize makes wrong.
_TOOL_RESULT_MAX = _blocks.TOOL_RESULT_MAX
_cells = _blocks.cells


@dataclass
class Update:
    mode: str
    html: str

    def __repr__(self) -> str:
        return f"Update({self.mode}, {self.html!r})"


# The one-shot constructors. A7 changed WHAT they return -- a Block rather
# than finished markup -- and nothing else: same names, same signatures, and
# so all 33 write_block call sites across eight services are untouched. That
# is the whole design of the conversion: the type moves, the call graph does
# not.
#
# Nine CONSUMERS did break, every one of them something doing string work on
# a return value, and "the goldens are unchanged" covers none of them. They
# are listed in the commit for A7 because a reader looking at this line later
# will want to know where the blast radius actually was.
user_turn = _blocks.user_turn
notice = _blocks.notice
error = _blocks.error
tool_call = _blocks.tool_call
tool_result = _blocks.tool_result
code_block = _blocks.code_block
para = _blocks.para
thinking = _blocks.thinking


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
