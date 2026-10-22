"""libfs under Wine, through tests/guest/fscheck.exe.

Wine proves the code and never the platform (CLAUDE.md), and fscheck splits
its output accordingly: `pass`/`FAIL` rows are arithmetic and control flow
that hold anywhere, and `note` rows are platform behaviour -- read-only
semantics, share-deny locks, drive types -- which is exercised here and
confirmed on the real guest.
"""
from __future__ import annotations

import conftest
import pytest

pytestmark = conftest.needs_guest


# Every case fscheck is expected to run. Hard-coded on purpose: without it, a
# case that stops being emitted -- renamed, or returned early out of -- takes
# its own assertion with it and the suite goes green with less coverage than
# it had yesterday.
EXPECTED = {
    "path.contained",
    "path.working_name",
    "path.reserve",
    "writer.commit",
    "writer.mismatch",
    "writer.unreadable",
    "writer.abort",
    "writer.no_aside",
    "recover.restores",
    "recover.drops_stale",
    "recover.drops_temp",
    "reader.whole",
    "reader.max_bytes",
    "reader.cap_at_eof",
    "reader.sink_stops",
    "reader.empty",
    "reader.missing",
    "dir.walk",
    "dir.filter",
    "dir.overflow",
    "dir.depth",
    "dir.no_root",
    "dir.sink_stops",
    "dir.list",
    "dir.list_root",
    "dir.list_refuses",
    "file.stat_file",
    "file.stat_dir",
    "file.stat_missing",
    "file.move",
    "xfer.sink_commit",
    "xfer.sink_out_of_order",
    "xfer.sink_abort",
    "xfer.sink_verify_fails",
    "xfer.source",
    "xfer.source_tee",
    "xfer.source_stops",
    "xfer.source_max_bytes",
    "drives.enumerate",
    "tree.stats",
    "tree.copy",
    "tree.copy_refuses_existing",
    "tree.copy_overwrite",
    "tree.copy_into_itself",
    "tree.move_rename",
    "tree.delete_permanent",
    "tree.delete_refuses_root",
    "tree.delete_refuses_relative",
    "drives.media_is_not_free_space",
    "drives.quiet_leaves_removable_unknown",
    "drives.probe_asks_what_quiet_skipped",
    "drives.volume_info",
    "drives.free_bytes",
    "drives.saturates",
    "drives.refuses_nonsense",
    "file.remove_readonly",
    "summary",
}


@pytest.fixture(scope="module")
def rows() -> dict:
    return conftest.guest_verdicts("fscheck.exe", "fsscratch")


def test_every_case_ran(rows):
    """The set of cases, not just their verdicts."""
    assert set(rows) == EXPECTED, (
        f"missing {EXPECTED - set(rows)}, unexpected {set(rows) - EXPECTED}")


def test_nothing_failed(rows):
    failed = {name: detail for name, (verdict, detail) in rows.items()
              if verdict == "FAIL"}
    assert not failed, f"fscheck failures: {failed}"


def test_the_platform_half_is_only_noted(rows):
    """A `note` is exercised and reported, never asserted.

    It exists so the guest run has something to compare against. If one of
    these ever becomes a pass/FAIL row, that is a claim that Wine settles it,
    and this test is where that claim has to be made deliberately.
    """
    noted = {name for name, (verdict, _) in rows.items() if verdict == "note"}
    assert noted == {"file.remove_readonly", "drives.volume_info"}
