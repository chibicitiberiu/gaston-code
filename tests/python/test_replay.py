"""Replay buffer tests.

The contract: the guest crashes, reconnects with from=<seq>, and gets exactly
what it missed -- no gap, no duplicate. A silent gap is the dangerous failure,
because the guest has no way to detect one.
"""

import pytest

from bridge.protocol import Frame
from bridge.replay import Replay


def f(n: int) -> Frame:
    return Frame("TEXT", {}, f"{n}".encode())


def test_append_stamps_monotonic_seqs():
    r = Replay()
    for i in range(5):
        assert r.append(f(i)).headers["seq"] == str(i + 1)
    assert r.next_seq == 6


def test_append_returns_the_same_object():
    """Callers write `await send(replay.append(frame))`."""
    r = Replay()
    frame = f(1)
    assert r.append(frame) is frame


def test_since_returns_the_tail():
    r = Replay()
    for i in range(10):
        r.append(f(i))
    got = r.since(7)
    assert [x.body for x in got] == [b"6", b"7", b"8", b"9"]
    assert [x.seq for x in got] == [7, 8, 9, 10]


def test_since_from_start_returns_everything():
    r = Replay()
    for i in range(3):
        r.append(f(i))
    assert len(r.since(1)) == 3


def test_since_at_head_is_empty_not_an_error():
    """A guest that missed nothing asks for next_seq and gets nothing back."""
    r = Replay()
    r.append(f(0))
    assert r.since(r.next_seq) == []


def test_eviction_advances_the_floor():
    r = Replay(max_frames=5)
    for i in range(10):
        r.append(f(i))
    assert len(r) == 5
    assert r.oldest_seq == 6
    assert r.next_seq == 11
    assert [x.body for x in r.since(6)] == [b"5", b"6", b"7", b"8", b"9"]


def test_resume_below_the_floor_raises_rather_than_lying():
    """Handing back a partial history silently would leave the transcript with
    a hole the guest cannot notice. Make it say so instead."""
    r = Replay(max_frames=3)
    for i in range(10):
        r.append(f(i))
    with pytest.raises(LookupError, match="evicted"):
        r.since(1)


def test_can_resume_from_reports_the_window():
    r = Replay(max_frames=3)
    for i in range(10):
        r.append(f(i))
    assert not r.can_resume_from(1)
    assert r.can_resume_from(r.oldest_seq)
    assert r.can_resume_from(r.next_seq)


def test_seqs_stay_unique_and_ordered_across_eviction():
    r = Replay(max_frames=4)
    seqs = []
    for i in range(20):
        seqs.append(int(r.append(f(i)).headers["seq"]))
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


def test_reconnect_round_trip():
    """The whole point, end to end: send some, 'crash', resume, get the rest."""
    r = Replay()
    for i in range(5):
        r.append(f(i))

    # Guest saw up to seq 3 and died.
    guest_next = 4

    for i in range(5, 8):
        r.append(f(i))

    missed = r.since(guest_next)
    assert [x.seq for x in missed] == [4, 5, 6, 7, 8]
    assert [x.body for x in missed] == [b"3", b"4", b"5", b"6", b"7"]
