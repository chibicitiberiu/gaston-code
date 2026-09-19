"""Pagination: the thing that makes a 32MB guest viable.

The transcript is paginated, not scrolled. The RichEdit control holds exactly
one page, so memory is bounded by PAGE SIZE rather than by transcript length. A
200-line page is ~16KB of text; even at 10x internal overhead that is 240KB,
which is nothing against 8-12MB free.

It also deletes the failure mode most likely to have sunk RichEdit -- appending
at the bottom while the reader is scrolled up two thousand lines -- because on a
sealed page nothing is appending at all.

Numbering rules, and the first one is the non-obvious one:

  * Pages fill FORWARD from the oldest. Page 1 is the start of the conversation.
    A sealed page never renumbers. Numbering backwards from the end (so "page 1"
    is newest) would invalidate every anchor the moment new text arrived.
  * A page seals when it is full, and is then frozen forever.
  * In LIVE mode a page seals only at a MESSAGE boundary, so a turn is never
    split mid-sentence under the reader's eyes. Pages may therefore overshoot
    the soft cap.
  * The hard cap is 2x, and it breaks inside a run if it must, marking the page
    `continued`. Without this a single 200KB fenced code block is one unbounded
    page and the memory bound is fiction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Soft cap. Sealing waits for a message boundary past this.
MAX_LINES = 200
MAX_BYTES = 16 * 1024

# And a cap on the number of BLOCKS, which is the one that matters now the
# transcript is HTML. MSHTML's cost scales with element count rather than with
# text length: 200 one-word paragraphs cost far more than one paragraph of 200
# words, and the byte cap cannot tell them apart. Measured on the 32MB guest, a
# 200-element page took the machine down to 48 KB free.
MAX_BLOCKS = 90

# Hard cap. Break mid-run rather than let one huge code block defeat the bound.
HARD_BYTES = MAX_BYTES * 2
HARD_LINES = MAX_LINES * 2
HARD_BLOCKS = MAX_BLOCKS * 2


@dataclass
class PageRun:
    style: str
    text: str


@dataclass
class Page:
    number: int
    runs: list[PageRun] = field(default_factory=list)
    lines: int = 0
    nbytes: int = 0
    blocks: int = 0
    sealed: bool = False
    continued: bool = False       # split mid-run from the previous page

    def add(self, style: str, text: str) -> None:
        if self.runs and self.runs[-1].style == style:
            self.runs[-1].text += text
        else:
            self.runs.append(PageRun(style, text))
        self.lines += text.count("\n")
        self.nbytes += len(text)
        self.blocks += 1

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    # Caps are passed in rather than read from the module, because they are
    # sized to the guest's memory and two guests can differ.
    def over_soft_cap(self, max_bytes: int = MAX_BYTES,
                      max_blocks: int = MAX_BLOCKS) -> bool:
        return (self.lines >= MAX_LINES or self.nbytes >= max_bytes
                or self.blocks >= max_blocks)

    def over_hard_cap(self, hard_bytes: int = HARD_BYTES) -> bool:
        # Blocks are deliberately not here: this governs splitting INSIDE a
        # run, and a block cap is satisfied by sealing between blocks, which
        # add() does before it starts.
        return self.lines >= HARD_LINES or self.nbytes >= hard_bytes

    def over_hard_blocks(self, hard_blocks: int = HARD_BLOCKS) -> bool:
        return self.blocks >= hard_blocks


@dataclass
class Anchor:
    """A user turn: the landmarks a reader actually navigates by."""

    page: int
    offset: int          # character offset within that page
    preview: str         # first ~40 characters


def limits_for(mem_kb: int) -> tuple[int, int]:
    """Page caps for a machine with this much RAM.

    32MB is the floor the whole design is built around and gets the original
    numbers. Anything roomier can hold a bigger page, and being forced to page
    every 90 blocks on a 128MB machine is an artificial restriction inherited
    from a machine it is not running on. Scaled by memory and clamped, because
    the point is to fit the host, not to grow without limit.
    """
    if mem_kb <= 0:
        return MAX_BYTES, MAX_BLOCKS
    factor = mem_kb / (32 * 1024)
    if factor < 1.0:
        factor = 1.0
    if factor > 4.0:
        factor = 4.0
    return int(MAX_BYTES * factor), int(MAX_BLOCKS * factor)


class Paginator:
    def __init__(self, max_bytes: int = MAX_BYTES,
                 max_blocks: int = MAX_BLOCKS) -> None:
        self.max_bytes = max_bytes
        self.max_blocks = max_blocks
        self.hard_bytes = max_bytes * 2
        self.hard_blocks = max_blocks * 2
        self._pages: list[Page] = [Page(number=1)]
        self._anchors: list[Anchor] = []

    # --- state -------------------------------------------------------------

    @property
    def live(self) -> Page:
        return self._pages[-1]

    @property
    def count(self) -> int:
        return len(self._pages)

    @property
    def anchors(self) -> list[Anchor]:
        return list(self._anchors)

    def all_text(self) -> str:
        """Every page's HTML, in order, as one string.

        For Export, which is the one caller that wants the whole transcript
        rather than a page of it. Built here rather than by reaching into
        `_pages` from outside, because "in order" is this class's business --
        the live page is the last one, and that is not obvious from the API.
        """
        return "".join(p.text for p in self._pages)

    def page(self, number: int) -> Page | None:
        if 1 <= number <= len(self._pages):
            return self._pages[number - 1]
        return None

    # --- writing -----------------------------------------------------------

    def add(self, style: str, text: str) -> list[Page]:
        """Append a run. Returns any pages sealed as a result."""
        sealed, _chunks = self.write(style, text)
        return sealed

    def write(self, style: str, text: str) -> tuple[list[Page], list[tuple[int, str]]]:
        """Append a run, tracking exactly which page each piece landed on.

        A run longer than the room left on the live page is split across a
        seal (below), and a caller that streams what it just wrote to a live
        viewer needs to know that split happened -- sending the whole run
        tagged with the page it STARTED on double-renders the tail once the
        reader pages forward to where it actually landed (02.33). `add()` is
        the old interface for callers that only want to know what sealed.
        """
        sealed: list[Page] = []
        chunks: list[tuple[int, str]] = []
        if not text:
            return sealed, chunks

        # A page holding too many elements is sealed before the next one is
        # added. Unlike the byte cap this needs no splitting: a block boundary
        # is already a clean place to stop.
        if self.live.over_hard_blocks(self.hard_blocks) and self.live.runs:
            sealed.append(self._seal(continued=True))

        while text:
            page = self.live
            room = self.hard_bytes - page.nbytes

            if room <= 0:
                sealed.append(self._seal(continued=True))
                continue

            if len(text) <= room and not page.over_hard_cap(self.hard_bytes):
                page.add(style, text)
                chunks.append((page.number, text))
                text = ""
            else:
                # Hard cap reached mid-run. Prefer to break at a newline inside
                # the allowed span so a code block splits between lines.
                chunk = text[:room]
                nl = chunk.rfind("\n")
                if nl > 0:
                    chunk = chunk[: nl + 1]
                page.add(style, chunk)
                chunks.append((page.number, chunk))
                text = text[len(chunk):]
                sealed.append(self._seal(continued=True))

        return sealed, chunks

    def message_boundary(self) -> Page | None:
        """A turn ended. Seal here if the page is over the soft cap.

        This is what keeps a turn from being split mid-sentence while the
        reader is watching it stream.
        """
        if self.live.over_soft_cap(self.max_bytes, self.max_blocks) \
                and self.live.runs:
            return self._seal(continued=False)
        return None

    def mark_user_turn(self, preview: str) -> Anchor:
        page = self.live
        a = Anchor(page=page.number, offset=page.nbytes,
                   preview=preview[:40].replace("\n", " "))
        self._anchors.append(a)
        return a

    def _seal(self, *, continued: bool) -> Page:
        page = self.live
        page.sealed = True
        nxt = Page(number=page.number + 1, continued=continued)
        self._pages.append(nxt)
        return page
