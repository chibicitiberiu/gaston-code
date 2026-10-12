"""The shim over the split. Nothing of the transcript is built here.

WHAT HAPPENED HERE. This file was the whole transcript renderer: the markdown
parser, the HTML emitter and the stylesheet in one place. Stage 3 moves the
emitter to the client, so the parser and the emitter had to stop being the
same object first. They are now:

  * `bridge/view/blocks.py`      what the model said, as structure. No markup.
  * `bridge/view/htmlrender.py`  blocks to HTML. Deleted in stage 3b -- it
                                 was the oracle the guest's renderer was
                                 proved against, and once the proof was
                                 recorded as a fixture there was no reason
                                 for a second renderer to exist.

and this file is what is left plus a re-export of everything its importers
already name.

NOTHING IS LEFT OF THE DOCUMENT. `STYLESHEET`, `WIRE_CSS`, `SKELETON` and
`document` went at C4 with the DOC frame; `export_document`, `EXPORT_CSS`
and `esc` went at C3 with the export. None of them were moved into the two
new modules, because their destination was never this machine: the
stylesheet is win/resources/view.css and the markup is view.tpl, both
beside the exe.

What is left here is a handful of re-exports and the one-shot block
constructors, which nine modules and 32 call sites name. `HtmlStream` and
`Update` went with the oracle in stage 3b: one was `BlockStream` with a
renderer bolted on and the other was the pair it returned.

The incremental rule that made the parser tractable is in `blocks.py` now, but
it is worth repeating where people will look for it: **only complete lines are
classified.** Anything after the last newline is held, because a line starting
``*`` might be a bullet or might be the start of ``**bold**`` and you cannot
tell yet.
"""

from __future__ import annotations

from . import blocks as _blocks
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

#: The one cap that STAYS on the bridge, and the line is bandwidth against
#: appearance: truncating on the client means the bytes already crossed a
#: 5 KB/s link. The wrap column left at C2 with the renderer -- the client
#: knows its own window's width and this one was measured once in a
#: 640x480 default that a resize makes wrong.
_TOOL_RESULT_MAX = _blocks.TOOL_RESULT_MAX
_cells = _blocks.cells


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
