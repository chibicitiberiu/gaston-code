"""One bad manifest row must cost that row, and nothing else.

`parse_rows`'s docstring states the rule plainly: "Unparseable rows are logged
and skipped rather than raising: one bad row must not cost the whole
manifest." That is the right rule, and the count check in the CALLER is what
makes it safe -- a dropped row makes `len(rows) != files`, which refuses the
pass rather than letting a missing row read as a deletion.

The rule was only half implemented. The `try` covered splitting the line and
slicing the path, and then every remaining conversion -- `Type(...)`,
`float(mtime)`, `int(crc_norm)`, `int(crc_raw)` -- sat OUTSIDE it. A manifest
row with an unrecognised type letter, or a non-numeric mtime or checksum,
raised `ValueError` out of `parse_rows`, through `_sync_frame` and `_handle`,
to the outermost `except Exception` in the connection pump. That does not skip
a row. It tears down the guest's connection.

Audit finding 03.18 described this as a row "dropped silently". It was the
opposite: nothing was dropped and nothing was silent, the whole link went
down.
"""
from __future__ import annotations

import pytest

from bridge.files import manifest
from bridge.files.caps import from_hello


def caps(manifest_version: int = 2):
    return from_hello({"manifest": str(manifest_version), "acp": "1252"})


def row(typ=b"f", cls=b"t", size=b"10", mtime=b"1700000000.0",
        crc_norm=b"111", crc_raw=b"222", name=b"a.txt") -> bytes:
    return (typ + b" " + cls + b" " + size + b" " + mtime + b" "
            + crc_norm + b" " + crc_raw + b" "
            + str(len(name)).encode() + b" " + name)


def test_a_good_row_parses():
    """The baseline, so the cases below are about the bad row and not the shape."""
    rows, display = manifest.parse_rows(row(), caps())
    assert len(rows) == 1
    assert display[next(iter(rows))] == "a.txt"


@pytest.mark.parametrize("bad, why", [
    (row(typ=b"q"), "an unrecognised type letter"),
    (row(typ=b""), "an empty type"),
    (row(mtime=b"soon"), "a non-numeric mtime"),
    (row(crc_norm=b"beef"), "a non-numeric normalised checksum"),
    (row(crc_raw=b"beef"), "a non-numeric raw checksum"),
])
def test_a_bad_row_is_skipped_and_does_not_raise(bad, why):
    """Every one of these used to raise out of parse_rows.

    Raising here does not drop a row -- it unwinds through `_sync_frame` and
    `_handle` to the connection pump's outermost handler and drops the LINK.
    """
    rows, _ = manifest.parse_rows(bad, caps())
    assert rows == {}, f"{why} should have been skipped, not parsed"


def test_one_bad_row_does_not_cost_the_good_ones():
    """The actual promise in the docstring, stated as a test.

    Two good rows either side of a bad one. Before the fix this raised on the
    bad row and the caller never saw the good ones -- so the failure was not
    'lose one file', it was 'lose the connection mid-manifest'.
    """
    body = b"\n".join([
        row(name=b"first.txt"),
        row(typ=b"q", name=b"broken.txt"),
        row(name=b"last.txt"),
    ])
    _rows, display = manifest.parse_rows(body, caps())
    assert sorted(display.values()) == ["first.txt", "last.txt"]


def test_the_short_count_is_what_makes_skipping_safe():
    """Skipping is only safe because the caller counts.

    Not a test of parse_rows so much as a record of why dropping a row is an
    acceptable answer at all: the caller compares len(rows) against SYNCEND's
    `files=`, and a mismatch refuses the whole pass. Without that, a dropped
    row reads as a file the user deleted.
    """
    body = b"\n".join([row(name=b"a.txt"), row(typ=b"q", name=b"b.txt")])
    rows, _ = manifest.parse_rows(body, caps())
    files_header = 2
    assert len(rows) != files_header


def test_the_size_field_is_parsed_and_thrown_away():
    """Not a bug, but worth pinning so nobody "fixes" it either way.

    Every manifest row carries a size, and the bridge never uses it -- `Entry`
    has no size field, and the classification and change detection both work
    off the checksums. So a row with a nonsense size parses fine, and that is
    correct rather than lax: validating a number nothing reads would be
    inventing a requirement, and the guest is the only side that could act on
    a size anyway.

    Pinned because the obvious reading of the row format says otherwise, and
    the last person to notice this wrote a test asserting it was validated.
    """
    rows, _ = manifest.parse_rows(row(size=b"-"), caps())
    assert len(rows) == 1
