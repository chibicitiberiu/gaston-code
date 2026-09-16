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

# Hard cap. Break mid-run rather than let one huge code block defeat the bound.
HARD_BYTES = MAX_BYTES * 2
HARD_LINES = MAX_LINES * 2


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
    sealed: bool = False
    continued: bool = False       # split mid-run from the previous page

    def add(self, style: str, text: str) -> None:
        if self.runs and self.runs[-1].style == style:
            self.runs[-1].text += text
        else:
            self.runs.append(PageRun(style, text))
        self.lines += text.count("\n")
        self.nbytes += len(text)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    def over_soft_cap(self) -> bool:
        return self.lines >= MAX_LINES or self.nbytes >= MAX_BYTES

    def over_hard_cap(self) -> bool:
        return self.lines >= HARD_LINES or self.nbytes >= HARD_BYTES


@dataclass
class Anchor:
    """A user turn: the landmarks a reader actually navigates by."""

    page: int
    offset: int          # character offset within that page
    preview: str         # first ~40 characters


class Paginator:
    def __init__(self) -> None:
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

    def page(self, number: int) -> Page | None:
        if 1 <= number <= len(self._pages):
            return self._pages[number - 1]
        return None

    # --- writing -----------------------------------------------------------

    def add(self, style: str, text: str) -> list[Page]:
        """Append a run. Returns any pages sealed as a result."""
        sealed: list[Page] = []
        if not text:
            return sealed

        while text:
            page = self.live
            room = HARD_BYTES - page.nbytes

            if room <= 0:
                sealed.append(self._seal(continued=True))
                continue

            if len(text) <= room and not page.over_hard_cap():
                page.add(style, text)
                text = ""
            else:
                # Hard cap reached mid-run. Prefer to break at a newline inside
                # the allowed span so a code block splits between lines.
                chunk = text[:room]
                nl = chunk.rfind("\n")
                if nl > 0:
                    chunk = chunk[: nl + 1]
                page.add(style, chunk)
                text = text[len(chunk):]
                sealed.append(self._seal(continued=True))

        return sealed

    def message_boundary(self) -> Page | None:
        """A turn ended. Seal here if the page is over the soft cap.

        This is what keeps a turn from being split mid-sentence while the
        reader is watching it stream.
        """
        if self.live.over_soft_cap() and self.live.runs:
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
