"""The transcript: every record, in order.

This file was the paginator -- the thing that cut a stream of HTML into
MSHTML-sized chunks at boundaries safe for MSHTML, with a soft cap, a hard
cap, a block cap, three `over_*` predicates, message boundaries, page
numbering that could never renumber, and anchors into page-relative byte
offsets. 272 lines of it.

All of that existed because the bridge was deciding how much of a
conversation a 1998 machine could hold at once, from a memory figure it had
to be told. Stage 3 made a record indivisible, so every boundary became
safe; stage 3b let the client ask for the records it wants by `offset` and
`limit`, so the only thing left to decide is a slice -- and the one with the
memory constraint decides it.

What is left is the list.
"""

from __future__ import annotations


class Transcript:
    """The transcript: every record, in order, and nothing else.

    What `Paginator` becomes once the client asks for blocks by range
    (`notes/stage3b-plan.md`). It is append-only and it has no opinion about
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
