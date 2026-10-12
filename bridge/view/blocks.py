"""What the model said, as structure. No markup anywhere in this file.

This is the half of the old `html.py` that stage 3 keeps on the bridge. The
split is along one line: **this decides what a thing IS, and `htmlrender.py`
decides what it looks like.** A heading is a heading here and an `<h3>` there;
a fenced block is `code` here and a `<pre>` inside a `<div class="codewrap">`
there. Nothing in this module knows an element name, a class name or an escape.

WHY THE PARSE STAYS. It is the larger half -- the fence state that has to
survive a feed boundary, the underscore lookarounds, the code-inside-bold
recursion, `_tail`'s bandwidth policy -- and it is also what makes a block
ADDRESSABLE. A client asking for blocks 120..160 needs something that knows
where block 120 begins, and only a parser does. Markdown on the wire would
move ~180 lines of this into C on a machine with 3KB of image slack and give
up range-serving in the same move. See docs/stage3-plan.md §1.

THE PAYLOAD SHAPE IS THE WIRE SHAPE. A `Block` carries `(tag, text)` pairs,
one per line, because that is literally what A5 serialises: a tag letter, a
space, the text. Keeping the in-memory form and the wire form the same thing
means the encoder is a loop and not a translation, and means a Block that
round-trips is a Block the renderer will see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

APPEND = "append"
LIVE = "live"

#: Bumped when a client would mis-render what an older bridge sends. Declared
#: at the handshake (`blocks` in HELLO, stage 3 C1) rather than assumed, per
#: CLAUDE.md's "client rules are declared at handshake, never assumed".
VERSION = 1

#: Every kind, with the attributes it may carry and the payload tags it may
#: use. The agreement test (tests/python/meta/test_block_vocab.py) reads this
#: and the guest's render.h and compares them, so a kind added on one side
#: and not the other fails loudly rather than rendering as nothing.
#:
#: `link` is deliberately absent. BACKLOG 31 claimed an inline link run and
#: there has never been one -- no [text](url) pattern, no href, no <a> rule --
#: and the reason not to add one is not effort: the transcript is a
#: WebBrowser control, so an <a href> navigates it and takes the transcript
#: with it.
KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    #  kind            attrs                    payload tags
    "para":         ((),                        ("t",)),
    "code":         (("clipped",),              ("l", "t")),
    "heading":      (("level",),                ("t",)),
    "bullet":       ((),                        ("t",)),
    "numbered":     ((),                        ("t",)),
    "quote":        ((),                        ("t",)),
    "rule":         ((),                        ()),
    "table":        (("header",),               ("r", "c")),
    "user":         ((),                        ("t",)),
    "notice":       ((),                        ("t",)),
    "error":        ((),                        ("t",)),
    "tool":         ((),                        ("n", "t")),
    "toolresult":   (("error", "clipped"),      ("t",)),
    "thinking":     ((),                        ("t",)),
    "permission":   (("settleable",),           ("h", "t", "w", "p", "s",
                                                 "k", "v")),
    "clip":         (("dropped",),              ()),
    "exportheading": ((),                       ("h", "t")),
}

#: Attribute ranges. Every one is a small integer or a bounded enumeration,
#: because an attribute rides on the record's first line and that line is
#: capped. Anything unbounded is a payload line instead -- which is why a
#: fence's language is an `l` line and not an attribute: `_FENCE` captures it
#: from model output and nothing bounds what a model writes after ```.
ATTRS: dict[str, tuple[int, int]] = {
    "level":      (1, 6),
    "header":     (0, 1),
    "error":      (0, 1),
    "settleable": (0, 1),
    "clipped":    (0, 1 << 30),
    "dropped":    (0, 1 << 30),
}


@dataclass
class Block:
    """One finished thing in the transcript.

    `payload` is a list of `(tag, text)` pairs in order. A tag is one ASCII
    letter and its meaning is per-kind (see KINDS): `t` is the general text
    line, `l` is a fence's language, `r` opens a table row and `c` is a cell
    in it, and the permission card uses six of its own.

    A pair with empty text is REAL and load-bearing: `user_turn("a\\n\\nb")`
    has a blank line in the middle, and an empty `t` is how it survives to
    become the `<br><br>` it renders as today.
    """

    kind: str
    attrs: dict[str, int] = field(default_factory=dict)
    payload: list[tuple[str, str]] = field(default_factory=list)

    def __repr__(self) -> str:
        a = "".join(f" {k}={v}" for k, v in sorted(self.attrs.items()))
        return f"Block({self.kind}{a}, {self.payload!r})"

    def texts(self) -> list[str]:
        """Just the `t` lines, which is what most kinds are made of."""
        return [t for tag, t in self.payload if tag == "t"]


@dataclass
class BlockUpdate:
    """One thing to do to the transcript.

    `block` is None on a LIVE update that clears the open slot -- the turn
    ended, or nothing is open yet. That is not the same as a block with no
    payload, and the guest treats them differently: one empties `#l`, the
    other renders.
    """

    mode: str
    block: Block | None

    def __repr__(self) -> str:
        return f"BlockUpdate({self.mode}, {self.block!r})"


# --- the grammar -----------------------------------------------------------

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


def cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def table_block(lines: list[str]) -> Block:
    """A markdown table, as rows and cells.

    The separator row (|---|---|) is what marks the row above it as a header;
    without one every row is a body row, which is what a model emitting a
    pipe-delimited list without a header expects.

    The separator itself never reaches the payload -- it is punctuation, not
    content, and a client that received it would have to know to drop it.
    `header=1` is the one bit of it worth carrying.
    """
    rows = [cells(line) for line in lines]
    header = 0
    body = rows
    head: list[str] = []
    if len(rows) >= 2 and all(_SEP.match(c) for c in rows[1] if c):
        header, head, body = 1, rows[0], rows[2:]

    payload: list[tuple[str, str]] = []
    for r in ([head] if header else []) + body:
        payload.append(("r", ""))
        payload.extend(("c", c) for c in r)
    return Block("table", {"header": header}, payload)


# --- the one-shot constructors ---------------------------------------------
#
# These were in html.py and returned finished markup. They return structure
# now, and every one of their ~33 call sites is unchanged because the names
# and the signatures are.

#: A tool result is for the person watching, not for the model -- it already
#: has the whole thing. Long enough to be useful on a 640x480 screen and
#: short enough not to spend the guest's memory on a `dir /s`.
#:
#: This one stays on the bridge after stage 3 while the wrap column leaves,
#: and the line between them is bandwidth against appearance: truncating on
#: the client means the bytes already crossed a 5 KB/s link.
TOOL_RESULT_MAX = 1200


def user_turn(text: str) -> Block:
    """One user message, as its own block."""
    return Block("user", {}, [("t", ln)
                              for ln in (text.strip().splitlines() or [""])])


def notice(text: str) -> Block:
    return Block("notice", {}, [("t", text)])


def error(text: str) -> Block:
    return Block("error", {}, [("t", text)])


def tool_call(name: str, summary: str) -> Block:
    return Block("tool", {}, [("n", name), ("t", summary)])


def tool_result(body: str, *, error: bool = False) -> Block:
    """What a tool actually returned.

    Capped, because a tool can return a great deal more than a 640x480 screen
    or a 32MB machine wants, and the model already has the full text. The cap
    is a count of DROPPED characters rather than a marker, so the client words
    it and a client with no screen can ignore it.
    """
    body = (body or "").strip()
    if not body:
        body = "(no output)"
    kept = body[:TOOL_RESULT_MAX]
    attrs = {"error": 1 if error else 0}
    if len(body) > TOOL_RESULT_MAX:
        attrs["clipped"] = len(body) - TOOL_RESULT_MAX
    return Block("toolresult", attrs, [("t", ln) for ln in kept.split("\n")])


def code_block(text: str, lang: str = "") -> Block:
    payload: list[tuple[str, str]] = [("l", lang)] if lang else []
    payload.extend(("t", ln) for ln in text.split("\n"))
    return Block("code", {}, payload)


def para(text: str) -> Block:
    return Block("para", {}, [("t", ln) for ln in text.split("\n")])


def thinking(text: str) -> Block:
    return Block("thinking", {}, [("t", ln) for ln in text.split("\n")])


# --- the stream ------------------------------------------------------------


class BlockStream:
    """Feed model text in, get block updates out.

    The state machine is the old `HtmlStream`'s, line for line; only what it
    emits changed. The kinds are renamed to the vocabulary's words on the way
    through -- `pre` became `code`, `h` became `heading`, `ul`/`ol` became
    `bullet`/`numbered` -- because a client with no layout engine should never
    be told the name of an element it is not going to draw.
    """

    #: How much of an open block a LIVE update may carry. 02.17.
    #:
    #: A LIVE update replaces the whole open element and `_render` returned
    #: all of it every coalescer flush (100-600ms), so a 60 KB fenced file
    #: went out as ~65 frames averaging 30 KB -- about two MEGABYTES on the
    #: wire for 60 KB of content, on a link measured in single-digit KB/s.
    #: Worse, past `MAX_FRAME - 1024` `send_html` drops LIVE frames without a
    #: word (link.py), so live updating just stopped partway through with no
    #: way to tell.
    #:
    #: Capping the TAIL, not the head: the newest lines are what a reader
    #: watching a long code block wants to see, and the finished block still
    #: arrives whole as an APPEND -- only the intermediate copies are bounded.
    LIVE_CAP = 4096

    def __init__(self) -> None:
        self._pending = ""          # after the last newline, not yet classified
        self._lines: list[str] = []  # lines of the block being built
        self._kind: str | None = None
        self._lang = ""

    # --- public ------------------------------------------------------------

    def feed(self, text: str) -> list[BlockUpdate]:
        self._pending += text
        if "\n" not in self._pending:
            return self._live_only()
        head, _, self._pending = self._pending.rpartition("\n")
        out: list[BlockUpdate] = []
        # splitlines(), not split("\n"), and it matters: it splits on \r too,
        # so a lone carriage return is already a line break today. The
        # renderer has to agree, and B2 compares the two byte for byte.
        for line in (head + "\n").splitlines():
            out.extend(self._line(line))
        out.extend(self._live_only())
        return out

    def flush(self) -> list[BlockUpdate]:
        """The turn is over: close whatever is open."""
        out: list[BlockUpdate] = []
        if self._pending:
            rest, self._pending = self._pending, ""
            out.extend(self._line(rest))
        closed = self._close()
        if closed is not None:
            out.append(BlockUpdate(APPEND, closed))
        out.append(BlockUpdate(LIVE, None))
        return out

    # --- internals ---------------------------------------------------------

    def _live_only(self) -> list[BlockUpdate]:
        return [BlockUpdate(LIVE, self._build(partial=True))]

    def _start(self, kind: str, lang: str = "") -> list[BlockUpdate]:
        """Close the open block and begin a new one."""
        out: list[BlockUpdate] = []
        closed = self._close()
        if closed is not None:
            out.append(BlockUpdate(APPEND, closed))
        self._kind = kind
        self._lang = lang
        self._lines = []
        return out

    def _close(self) -> Block | None:
        if self._kind is None:
            return None
        built = self._build(partial=False)
        self._kind = None
        self._lines = []
        return built

    def _line(self, raw: str) -> list[BlockUpdate]:
        line = raw.rstrip("\n")

        m = _FENCE.match(line)
        if m:
            if self._kind == "code":
                out = []
                closed = self._close()
                if closed is not None:
                    out.append(BlockUpdate(APPEND, closed))
                return out
            return self._start("code", m.group(2))

        # Inside a fence everything is literal, which is why fence state has to
        # survive across feeds.
        if self._kind == "code":
            self._lines.append(line)
            return []

        if not line.strip():
            out = []
            closed = self._close()
            if closed is not None:
                out.append(BlockUpdate(APPEND, closed))
            return out

        m = _RULE.match(line)
        if m:
            # `rule` and `heading` are built HERE rather than by _build, and
            # that is not an inconsistency to tidy away: both are complete in
            # one line, so there is no open block for _build to close. The old
            # code had the same shape -- _render has no branch for either.
            out = self._start("rule")
            out.append(BlockUpdate(APPEND, Block("rule")))
            self._kind = None
            return out

        m = _HEADING.match(line)
        if m:
            out = self._start("heading")
            level = min(len(m.group(1)) + 2, 6)
            out.append(BlockUpdate(APPEND, Block("heading", {"level": level},
                                                 [("t", m.group(2))])))
            self._kind = None
            return out

        m = _ROW.match(line)
        if m:
            out = [] if self._kind == "table" else self._start("table")
            self._lines.append(line)
            return out

        m = _BULLET.match(line)
        if m:
            # Numbered markers get `numbered`, so "1." does not silently
            # become a bullet and lose the numbering the model meant.
            #
            # The indent and the marker are captured and DISCARDED, here as
            # before: there is no nesting in the output and never has been, so
            # carrying a depth attribute would be the vocabulary advertising
            # something nothing fills.
            kind = "numbered" if m.group(2)[:1].isdigit() else "bullet"
            out = [] if self._kind == kind else self._start(kind)
            self._lines.append(m.group(3))
            return out

        m = _QUOTE.match(line)
        if m:
            out = [] if self._kind == "quote" else self._start("quote")
            self._lines.append(m.group(1))
            return out

        out = [] if self._kind == "para" else self._start("para")
        self._lines.append(line)
        return out

    def _tail(self, lines: list[str]) -> list[str]:
        """The last `LIVE_CAP` bytes' worth of an open block, newest first.

        Returned in original order with an elision marker in front when
        anything was dropped, so the reader can see that the block is longer
        than what is on screen rather than silently reading a fragment as the
        whole thing.
        """
        if self._kind == "table":
            # NOT capped. Dropping leading rows drops the header row and the
            # separator under it, so the table would be re-read as headerless
            # and the live view would disagree with the sealed one about what
            # the columns are. A table long enough to matter here is rare; a
            # table that changes shape while you watch it is not something to
            # ship to fix a bandwidth problem.
            return lines
        total = 0
        keep: list[str] = []
        for line in reversed(lines):
            total += len(line) + 1
            if keep and total > self.LIVE_CAP:
                break
            keep.append(line)
        keep.reverse()
        if len(keep) == len(lines):
            return lines
        # Plain "...", not an entity: every kind but `code` goes through the
        # inline parser, which escapes, so "&hellip;" would appear literally.
        # Inside a fence everything is literal anyway.
        return ["...", *keep]

    def _build(self, *, partial: bool) -> Block | None:
        lines = list(self._lines)
        if partial and self._pending:
            lines.append(self._pending)
        if not lines:
            return None
        if partial:
            lines = self._tail(lines)

        if self._kind is None:
            # Text arrived before any complete line, so no block is open yet.
            # Show it as a paragraph anyway: this is the first thing the reader
            # sees of a turn, and holding it back until a newline arrives is
            # exactly the stall the live block exists to avoid.
            return Block("para", {}, [("t", x) for x in lines])

        if self._kind == "code":
            payload: list[tuple[str, str]] = []
            if self._lang:
                payload.append(("l", self._lang))
            payload.extend(("t", x) for x in lines)
            return Block("code", {}, payload)
        if self._kind == "table":
            return table_block(lines)
        if self._kind in ("para", "bullet", "numbered", "quote"):
            return Block(self._kind, {}, [("t", x) for x in lines])
        return None
