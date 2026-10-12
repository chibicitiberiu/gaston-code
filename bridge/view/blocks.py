"""What the model said, as structure. No markup anywhere in this file.

This is the half of the old `html.py` that stage 3 keeps on the bridge. The
split is along one line: **this decides what a thing IS, and the client
decides what it looks like.** A heading is a heading here and an `<h3>` in
win/resources/view.tpl; a fenced block is `code` here and a `<pre>` inside a
`<div class="codewrap">` there. Nothing in this module knows an element name,
a class name or an escape. (The other half was `htmlrender.py`, kept through
stage 3 as the oracle the guest's renderer was proved against and deleted in
3b once that proof was recorded as a fixture.)

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

from ..wire import fold

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
    "permission":   (("settleable",),           ("h", "b", "d", "t", "w",
                                                 "p", "s", "k", "v")),
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
    in it, and the permission card uses eight of its own -- `h` which head,
    `b` the bold lead, `d` the description after it, `w` a warning, `p` a
    preview, `s` a numbered step, `k`/`v` a table row.

    ORDER IS MEANINGFUL and not just preserved: `render_request` puts its
    table last while `rules_view` puts its paragraphs after the table, so a
    renderer that emitted by tag in a fixed order would get one of them
    wrong. Consecutive `s` items form one list and consecutive `k`/`v` pairs
    form one table, which is a loop with one open-container variable on
    either side of the wire.

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
        payload.extend(("c", runs(clean(c))) for c in r)
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

#: What a client sends for `toolmax` when it wants the whole thing. A
#: sentinel rather than a large number, so "no limit" survives a future
#: change to what "large" means -- and distinct from an ABSENT key, which
#: means the client has no opinion and gets TOOL_RESULT_MAX.
NO_TOOL_CAP = -1


def user_turn(text: str) -> Block:
    """One user message, as its own block."""
    return Block("user", {}, [("t", clean(ln))
                              for ln in (text.strip().splitlines() or [""])])


#: One line per line, for the kinds the renderer rejoins with "\n". A
#: payload line cannot contain the separator that ends it, and these three
#: take text that can: a notice about a path, an error from the far side, a
#: tool summary with an argument in it.
def _lines(text: str) -> list:
    return [("t", clean(ln)) for ln in text.split("\n")]


def notice(text: str) -> Block:
    return Block("notice", {}, _lines(text))


def error(text: str) -> Block:
    return Block("error", {}, _lines(text))


def tool_call(name: str, summary: str) -> Block:
    return Block("tool", {},
                 [("n", clean(name).replace("\n", " ")), *_lines(summary)])


def tool_result(body: str, *, error: bool = False, cap: int = 0) -> Block:
    """What a tool actually returned.

    Capped, because a tool can return a great deal more than a 640x480 screen
    or a 32MB machine wants, and the model already has the full text. The cap
    is a count of DROPPED characters rather than a marker, so the client words
    it and a client with no screen can ignore it.

    `cap` is the CLIENT's, declared at the handshake as `toolmax` from stage
    3 C1. Threaded as a parameter here at A12 so that C1 only has to fill it
    in, the way Paginator.max_blocks already worked.

      0            the client did not say, so TOOL_RESULT_MAX stands
      NO_TOOL_CAP  it asked for all of it
      n > 0        it asked for n

    An ABSENT opinion and "none, thank you" are different answers and the
    handshake exists to tell them apart, which is why the second is a
    sentinel rather than a zero.

    This cap stays on the bridge while the wrap column leaves, and the line
    between them is bandwidth against appearance: truncating on the client
    means the bytes already crossed a 5 KB/s link.
    """
    body = (body or "").strip()
    if not body:
        body = "(no output)"
    limit = TOOL_RESULT_MAX if cap == 0 else cap
    attrs = {"error": 1 if error else 0}
    if limit == NO_TOOL_CAP or len(body) <= limit:
        kept = body
    else:
        kept = body[:limit]
        attrs["clipped"] = len(body) - limit
    return Block("toolresult", attrs,
                 [("t", clean(ln)) for ln in kept.split("\n")])


def code_block(text: str, lang: str = "") -> Block:
    payload: list[tuple[str, str]] = [("l", clean(lang))] if lang else []
    payload.extend(("t", clean(ln)) for ln in text.split("\n"))
    return Block("code", {}, payload)


def para(text: str) -> Block:
    return Block("para", {}, [("t", runs(clean(ln)))
                              for ln in text.split("\n")])


def thinking(text: str) -> Block:
    return Block("thinking", {}, [("t", clean(ln))
                                  for ln in text.split("\n")])


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
                                                 [("t", runs(clean(m.group(2))))])))
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
            return Block("para", {}, [("t", runs(clean(x))) for x in lines])

        if self._kind == "code":
            # LITERAL: cleaned but not marked. Inside a fence everything is
            # text, which is the whole meaning of a fence.
            payload: list[tuple[str, str]] = []
            if self._lang:
                payload.append(("l", clean(self._lang)))
            payload.extend(("t", clean(x)) for x in lines)
            return Block("code", {}, payload)
        if self._kind == "table":
            return table_block(lines)
        if self._kind in ("para", "bullet", "numbered", "quote"):
            return Block(self._kind, {},
                         [("t", runs(clean(x))) for x in lines])
        return None


# --- inline runs, as marks -------------------------------------------------
#
# The bridge finds the runs; the client draws them. A client with no layout
# engine wants to know that a stretch of text is code, not that it is wrapped
# in <code>.
#
# THE ENCODING IS A STACK, NOT A FLAG BYTE. An earlier draft of the plan used
# a flat 3-bit state per character on three claims, all of which are false and
# all of which were checked by running the old renderer:
#
#   * <b><code>x</code></b> and <code><b>x</b></code> do NOT render alike.
#     `code` is specified with the `font:` SHORTHAND, which resets
#     font-weight and font-style, so the nesting order is visible.
#   * Nesting reaches depth three: "__a *b `c` d* e__" is
#     <b>a <i>b <code>c</code> d</i> e</b>.
#   * A flat state is lossy: "**a****b**" is <b>a</b><b>b</b>, two spans that
#     a run list cannot tell from one.
#
#: Opens a span. Followed by exactly one letter naming which.
MARK_OPEN = "\x01"
#: Closes the innermost open span. Provably sufficient: the inline parser
#: concatenates balanced fragments, so the output is always well-nested.
MARK_CLOSE = "\x02"

#: Letter -> what the run means. Semantic, not an element: `s` is strong, not
#: bold, because a teletype client underlines it and a speech client stresses
#: it, and neither draws a <b>.
SPANS = {"c": "code", "s": "strong", "e": "emphasis"}

#: Depth is structurally bounded at three and the bound is CHECKED rather
#: than assumed: **X**/*X* exclude * from X and __X__/_X_ exclude _, so a
#: chain uses at most one *-family level, one _-family level and code.
MAX_SPAN_DEPTH = 3

#: The three bytes a record cannot carry as content. \x00 truncates a block
#: in the DOM (see link.py), and the other two are the marks themselves.
RESERVED = "\x00\x01\x02"


def clean(text: str) -> str:
    """Drop the bytes a record reserves. Model text, before anything else.

    HERE, AND NOT IN THE ENCODER, which is where the plan first put it and
    where it cannot work: by the time a payload reaches the encoder it is
    full of \\x01 and \\x02 that this function would strip, and the record
    would arrive as one unstyled run. The strip has to happen upstream of the
    marks, which means at the moment model text becomes payload.

    Not in `fold` either: fold.encode short-circuits on text.isascii(), so a
    NUL never reaches _fold_char, and protocol.py routes every header value
    through the same function.
    """
    if not text:
        return text
    for ch in RESERVED:
        if ch in text:
            text = text.replace(ch, "")
    return text


def runs(text: str, _depth: int = 0) -> str:
    """One line of prose, with its inline runs marked. No markup.

    Code spans do not recurse, which matches the old renderer: backticks win
    and everything inside them is literal.
    """
    out = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        if m.group("code"):
            out.append(MARK_OPEN + "c" + m.group("code")[1:-1] + MARK_CLOSE)
        elif m.group("bold"):
            inner = m.group("bold")[2:-2]
            out.append(MARK_OPEN + "s"
                       + (runs(inner, _depth + 1)
                          if _depth < MAX_SPAN_DEPTH else inner)
                       + MARK_CLOSE)
        else:
            inner = m.group("em")[1:-1]
            out.append(MARK_OPEN + "e"
                       + (runs(inner, _depth + 1)
                          if _depth < MAX_SPAN_DEPTH else inner)
                       + MARK_CLOSE)
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return "".join(out)


def span_depth(text: str) -> int:
    """The deepest nesting in a marked string. For the bound's own test."""
    depth = best = 0
    i = 0
    while i < len(text):
        if text[i] == MARK_OPEN and i + 1 < len(text) and text[i + 1] in SPANS:
            depth += 1
            best = max(best, depth)
            i += 2
            continue
        if text[i] == MARK_CLOSE and depth:
            depth -= 1
        i += 1
    return best


# --- the record codec ------------------------------------------------------
#
# THE RECORD LIVES IN THE FRAME BODY, not in frame headers, and the frame
# headers do not change. Headers were the first design and three things
# argued it down:
#
#   * A 90-block page goes out as ONE deflated frame today, because Page.add
#     coalesces same-style runs. One frame per block costs ~40 header bytes
#     each plus an independent deflate per frame, on a 5 KB/s link.
#   * An export would become thousands of frames and would rewrite
#     ChunkSink's offset protocol to do it.
#   * The composite kinds need a body parser regardless, so headers would
#     have saved the key=value scan and encoded the kind twice.
#
#     <kind> [<attr>=<value>]*   LF   first line, ASCII, bounded
#     <tag>[ <payload>]          LF   zero or more
#     .                          LF   end of record
#
# No dot-stuffing. A payload line always begins with its tag letter and only
# the terminator begins with '.', so the ambiguity dot-stuffing exists to
# solve cannot arise.

#: The first line is ASCII and bounded, because it is the one line a reader
#: must parse before it knows what it is reading. Attributes are small
#: integers and bounded enumerations; anything a model can grow is a payload
#: line instead.
MAX_HEAD = 128

#: Refused rather than truncated. The client declares its own limit at the
#: handshake (`maxblock`); this is the bridge's own sanity bound.
MAX_RECORD = 1 << 20


class RecordError(ValueError):
    """A record that cannot be built or cannot be read."""


def encode(block: Block) -> bytes:
    """One record, folded and ready for a frame body.

    Folded HERE rather than by the caller, so that `pack` measures the bytes
    that actually go on the wire. Folding EXPANDS -- an ellipsis becomes
    three dots and an em dash becomes two hyphens -- so a budget counted in
    characters is not a budget.
    """
    allowed_attrs, allowed_tags = KINDS.get(block.kind, ((), ()))
    if block.kind not in KINDS:
        raise RecordError(f"unknown kind {block.kind!r}")

    head = [block.kind]
    for k in sorted(block.attrs):
        if k not in allowed_attrs:
            raise RecordError(f"{block.kind} has no attribute {k!r}")
        lo, hi = ATTRS[k]
        v = int(block.attrs[k])
        if not lo <= v <= hi:
            raise RecordError(f"{k}={v} is outside {lo}..{hi}")
        head.append(f"{k}={v}")
    line = " ".join(head)
    if len(line) > MAX_HEAD:
        raise RecordError(f"head is {len(line)} bytes, over {MAX_HEAD}")

    out = [line.encode("ascii")]
    for tag, text in block.payload:
        if len(tag) != 1 or not tag.isascii() or not tag.isalpha():
            raise RecordError(f"tag {tag!r} is not one ASCII letter")
        if tag not in allowed_tags:
            raise RecordError(f"{block.kind} has no payload tag {tag!r}")
        if "\n" in text:
            # The line separator cannot be inside a line. Found by the
            # equality proof rather than by reasoning: error("x\ny") put a
            # newline in one `t`, the guest's parser read the tail as a
            # second line with tag "y", and the second half of the message
            # vanished. A constructor that can see multi-line text splits
            # it into several lines of the same tag; this refuses the rest.
            raise RecordError(
                f"{block.kind}'s {tag!r} payload contains a newline; split "
                f"it into one line per tag")
        # A tag alone is a line with no payload, and that is load-bearing: a
        # blank line in a pasted message is an empty `t`, and without it the
        # blank lines vanish.
        out.append(fold.encode(tag + (" " + text if text else "")))
    out.append(b".")
    data = b"\n".join(out) + b"\n"
    if len(data) > MAX_RECORD:
        raise RecordError(f"record is {len(data)} bytes, over {MAX_RECORD}")
    return data


def pack(items: list[Block], budget: int) -> list[bytes]:
    """Records grouped into frame bodies, each within `budget` BYTES.

    A record is never split. A frame body is zero or more WHOLE records, and
    zero is real -- an empty LIVE body is what clears the open slot.

    A single record larger than the budget gets a body of its own, whole,
    rather than being cut: half a record is not a smaller record, it is two
    fragments the client must refuse. Bounding it is the client's job, via
    the `maxblock` it declares at the handshake.
    """
    bodies: list[bytes] = []
    cur: list[bytes] = []
    size = 0
    for b in items:
        rec = encode(b)
        if cur and size + len(rec) > budget:
            bodies.append(b"".join(cur))
            cur, size = [], 0
        cur.append(rec)
        size += len(rec)
    if cur:
        bodies.append(b"".join(cur))
    return bodies


def decode(data: bytes) -> list[Block]:
    """Records back to Blocks. For tests and for the agreement check.

    Nothing in production calls this -- the guest is the only reader of a
    record. It exists so that the encoder can be tested against something
    other than a golden string, and so the guest's parser has a second
    opinion to disagree with.
    """
    text = fold.decode(data)
    out: list[Block] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        head = lines[i]
        i += 1
        parts = head.split(" ")
        kind = parts[0]
        if kind not in KINDS:
            raise RecordError(f"unknown kind {kind!r}")
        attrs: dict[str, int] = {}
        for p in parts[1:]:
            if "=" not in p:
                raise RecordError(f"bad attribute {p!r}")
            k, _, v = p.partition("=")
            if k not in ATTRS:
                raise RecordError(f"unknown attribute {k!r}")
            attrs[k] = int(v)
        payload: list[tuple[str, str]] = []
        while True:
            if i >= len(lines):
                raise RecordError("record ended without a terminator")
            ln = lines[i]
            i += 1
            if ln == ".":
                break
            payload.append((ln[0], ln[2:] if len(ln) > 1 else ""))
        out.append(Block(kind, attrs, payload))
    return out
