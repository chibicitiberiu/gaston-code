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


class Transcript:
    """The transcript: every record, in order, and nothing else.

    What `Paginator` becomes once the client asks for blocks by range
    (`docs/stage3b-plan.md`). It is append-only and it has no opinion about
    where a page begins, because after stage 3 a record is indivisible and
    every boundary is safe -- so the only thing left to decide is how many
    records the reader wants, and the reader is the one with the memory
    constraint.

    IT LANDS BEFORE IT IS USED, and `Paginator` feeds it on every write, so
    the cutover has something to compare against: `test_pages.py`'s
    equivalence test asserts that the pages' records flattened in order are
    the same list, record for record, that this holds. That is the whole
    safety argument for deleting the pages, and it is cheap -- a second list
    of references to the same bytes objects, not a second copy of the
    transcript.
    """

    def __init__(self) -> None:
        self._records: list[bytes] = []

    def add(self, record: bytes) -> int:
        """Append one record. Returns the index it landed at."""
        self._records.append(record)
        return len(self._records) - 1

    @property
    def count(self) -> int:
        return len(self._records)

    def range(self, offset: int, limit: int) -> list[bytes]:
        """`limit` records starting at `offset`, zero-based.

        OUT OF RANGE IS EMPTY, NOT AN ERROR. A guest asking for a range that
        the transcript has since shrunk past -- which `reset` does on a
        conversation switch -- should get an empty answer and a total it can
        believe, not a refusal it has to have a recovery path for. The one
        thing that IS an error is a negative offset, because that is a bug on
        the asking side rather than a race.
        """
        if offset < 0:
            raise ValueError(f"offset {offset} is negative")
        if limit <= 0:
            return []
        return self._records[offset:offset + limit]

    def all_records(self) -> list[bytes]:
        """Every record, in order. For the export."""
        return list(self._records)


@dataclass
class PageRun:
    """A run of whole records, in order.

    A LIST OF RECORDS and not one concatenated string, which is the shape
    stage 3 C2 needed and the reason this class survived at all: the
    paginator must be able to hand a caller the records that landed on a
    page without re-parsing a flat buffer to find where each one begins.
    Re-parsing is exactly what moving the markup off the wire was meant to
    stop anybody having to do.

    `style` is gone with it. It was always "html" -- presentation.py had one
    caller and it passed a constant -- and it existed so that add() could
    coalesce adjacent runs of the same kind. There is one kind.
    """

    records: list[bytes] = field(default_factory=list)

    @property
    def body(self) -> bytes:
        return b"".join(self.records)


@dataclass
class Page:
    number: int
    runs: list[PageRun] = field(default_factory=list)
    lines: int = 0
    nbytes: int = 0
    blocks: int = 0
    sealed: bool = False
    #: Split mid-run from the previous page. ALWAYS FALSE from stage 3 C2
    #: and kept only because PAGESTART carries it: a record is never cut, so
    #: no page continues another. Setting it true for a page sealed by the
    #: block cap would be a lie the guest draws a marker for. It dies with
    #: the rest of pagination in stage 3b.
    continued: bool = False

    def add(self, record: bytes) -> None:
        if not self.runs:
            self.runs.append(PageRun())
        self.runs[-1].records.append(record)
        self.lines += record.count(b"\n")
        self.nbytes += len(record)
        self.blocks += 1

    @property
    def body(self) -> bytes:
        return b"".join(r.body for r in self.runs)

    @property
    def records(self) -> list:
        return [rec for r in self.runs for rec in r.records]

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
        #: The same records, in the same order, without the page boundaries.
        #: Fed by `write`; this is what survives stage 3b.
        self._transcript = Transcript()

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

    @property
    def transcript(self) -> Transcript:
        return self._transcript

    def all_records(self) -> list:
        """Every record in the transcript, in order. For the export.

        FROM THE TRANSCRIPT, not from the pages, since stage 3b's A2 -- the
        two are asserted equal by `test_pages.py` and this is the caller
        that proves it in production rather than only in a test.
        """
        return self._transcript.all_records()

    def page(self, number: int) -> Page | None:
        if 1 <= number <= len(self._pages):
            return self._pages[number - 1]
        return None

    # --- writing -----------------------------------------------------------

    def add(self, record: bytes) -> list[Page]:
        """Append a record. Returns any pages sealed as a result."""
        sealed, _chunks = self.write(record)
        return sealed

    def write(self, record: bytes) -> tuple[list[Page], list[tuple[int, bytes]]]:
        """Append one record, saying which page it landed on.

        ONE RECORD AT A TIME, which is what removed the loop. Until stage 3
        this took a string of markup and split it across page seals when it
        did not fit, and a caller streaming what it had just written needed
        to know where each piece went -- sending the whole run tagged with
        the page it STARTED on double-renders the tail once the reader pages
        forward (02.33). A record is indivisible, so there is one piece.

        THE MID-RUN SPLIT IS GONE and this is its replacement, named rather
        than left implicit. A byte-count split through a record cuts between
        payload lines and leaves two fragments the guest must refuse -- the
        same class of damage the old comment describes for HTML, where
        page N ended on an unclosed <pre> and MSHTML "fixed" it into a code
        block missing its frame.

        But deleting the arm alone would make this loop forever: a record
        larger than hard_bytes on a fresh page failed every remaining
        condition and nothing advanced `text`. So the rule is explicit --
        **a record too large for a page gets a page of its own, whole** --
        and it falls out of the structure rather than needing a branch: a
        page with no runs always accepts, whatever the size. What bounds
        that size is the client's declared `maxblock`, not this.
        """
        sealed: list[Page] = []
        if not record:
            return sealed, []

        # A page holding too many elements is sealed before the next one is
        # added. Unlike the byte cap this needs no splitting: a block
        # boundary is already a clean place to stop.
        if self.live.over_hard_blocks(self.hard_blocks) and self.live.runs:
            sealed.append(self._seal(continued=False))

        page = self.live
        if page.runs and (page.nbytes + len(record) > self.hard_bytes
                          or page.over_hard_cap(self.hard_bytes)):
            sealed.append(self._seal(continued=False))
            page = self.live

        page.add(record)
        self._transcript.add(record)
        return sealed, [(page.number, record)]

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
