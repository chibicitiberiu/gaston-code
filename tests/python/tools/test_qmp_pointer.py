"""The pointer walk lands on the pixel it was asked for.

`tools/qmp.py` drives a PS/2 mouse, which is relative, through a guest that
applies pointer acceleration. So "click at (400,300)" is arithmetic, and the
arithmetic is built on constants MEASURED on the guest:

    delta sent      1    2    3    4   ..  127   128+
    px travelled    1    2    6    8   ..  254    254

Those numbers cost an afternoon of screendump-diffing to establish, and the
first attempt at them was wrong in a way nothing caught: the threshold was
taken to be 8 rather than 3, so the "fine" pass sent deltas of 7 that were
silently doubled and every move landed exactly 14px long. A click 14px off
lands on the next control, which looks like a flaky VM rather than a bug in
a lookup table.

This suite is the guard. `Model` below is the measured guest, not a guess:
each test drives the real `_walk` into it and checks the pointer arrived.
It needs no VM and no Wine, so it runs in the ordinary suite -- which is the
point, because the failure it catches is one you would otherwise only meet
while trying to debug something else.
"""

import os
import sys

import pytest

import conftest

HERE = conftest.REPO
sys.path.insert(0, os.path.join(HERE, "tools"))

import qmp                                                    # noqa: E402



class Model:
    """A Win98 guest's pointer, as measured.

    The one rule worth restating, because it is what shapes `_walk`: the
    scale is chosen PER PACKET from the larger axis, then applied to both.
    A packet of x=96 y=3 moves (192, 6) -- y is doubled because x is big.
    """

    def __init__(self, x=0, y=0, w=800, h=600):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.packets = 0

    def send(self, dx, dy):
        self.packets += 1
        dx = max(-127, min(127, dx))      # qemu clamps a ps2 delta
        dy = max(-127, min(127, dy))
        scale = 2 if max(abs(dx), abs(dy)) >= 3 else 1
        self.x = max(0, min(self.w - 1, self.x + dx * scale))
        self.y = max(0, min(self.h - 1, self.y + dy * scale))

    @property
    def at(self):
        return (self.x, self.y)


class FakeMonitor(qmp.Monitor):
    """The real device logic, with the wire replaced by the model."""

    def __init__(self, model):
        self.model = model

    def cmd(self, name, **args):
        assert name == "input-send-event"
        dx = dy = 0
        for e in args["events"]:
            assert e["type"] == "rel", "the walk sends nothing but motion"
            if e["data"]["axis"] == "x":
                dx = e["data"]["value"]
            else:
                dy = e["data"]["value"]
        self.model.send(dx, dy)
        return {}


@pytest.fixture
def guest(monkeypatch):
    monkeypatch.setattr(qmp.time, "sleep", lambda *_: None)
    return FakeMonitor(Model())


def targets():
    """Corners, edges, the awkward small numbers, and a spread across
    the screen. 3 and 6 are in here by name: 3 is the first accelerated
    delta and 6 is the smallest distance the coarse pass will touch."""
    named = [(0, 0), (1, 1), (2, 2), (3, 3), (5, 5), (6, 6), (7, 7),
             (1, 599), (799, 1), (799, 599), (400, 300), (33, 31)]
    spread = [(x, y) for x in (0, 97, 256, 511, 640, 799)
                     for y in (0, 59, 154, 377, 450, 599)]
    return named + spread


@pytest.mark.parametrize("tx,ty", targets())
def test_the_pointer_lands_on_the_pixel(guest, tx, ty):
    guest._walk(tx, ty)
    assert guest.model.at == (tx, ty)


@pytest.mark.parametrize("tx,ty", targets())
def test_a_move_homes_first_and_still_lands(guest, tx, ty):
    """`move` is `home` plus `_walk`, and homing is what makes it absolute.

    Start the pointer somewhere else entirely: if `move` were to trust a
    remembered position it would land wrong, which is the failure mode the
    position cache was removed to avoid.
    """
    guest.model.x, guest.model.y = 613, 208
    guest.move(tx, ty)
    assert guest.model.at == (tx, ty)


def test_homing_reaches_the_corner_from_the_far_side(guest):
    guest.model.x, guest.model.y = 799, 599
    guest.home()
    assert guest.model.at == (0, 0)


@pytest.mark.parametrize("tx,ty", [(799, 599), (400, 300), (97, 499)])
def test_a_move_costs_a_handful_of_packets_not_hundreds(guest, tx, ty):
    """The whole point of the calibration.

    A pixel-at-a-time walk to the far corner is 1398 packets; over libvirt
    that is 15 seconds and over `virsh` it was closer to 40. The bound here
    is deliberately loose -- it is guarding an order of magnitude, not a
    particular packing -- but it fails immediately if someone "simplifies"
    the coarse pass away.
    """
    guest.move(tx, ty)
    assert guest.model.packets <= 20


@pytest.mark.parametrize("tx,ty", [(731, 4), (4, 597), (799, 1), (600, 3), (3, 599)])
def test_a_long_axis_does_not_drag_a_short_one_past_its_target(guest, tx, ty):
    """The trap the measurements exist to describe.

    Acceleration is decided once per packet and applied to BOTH axes, so a
    packet carrying a big x and a tiny y doubles the y as well. `_walk` may
    still put them in one packet -- it halves both axes and credits both
    with twice what it sent -- but a walk that reasoned per axis would send
    y=4 expecting 4 and get 8. The short axis is where that shows, and only
    ever by a few pixels, which is the hardest kind of error to see.
    """
    guest._walk(tx, ty)
    assert guest.model.at == (tx, ty)


def test_the_fine_pass_cannot_be_accelerated(guest):
    """Whatever the coarse pass leaves behind must be finishable exactly.

    It exits once both axes are under `2 * COARSE_MIN`, so the remainder is
    always coverable by deltas of `FINE_MAX` or less -- which travel 1:1.
    If the coarse pass ever left more than that, the fine pass would have to
    send an accelerated delta and would overshoot by exactly that delta.
    """
    seen = []
    guest.model.send = lambda dx, dy: seen.append((dx, dy))
    guest._walk(731, 4)
    tail = [p for p in seen
            if max(abs(p[0]), abs(p[1])) < qmp.Monitor.COARSE_MIN]
    assert tail, "a walk to an odd target has to end with a fine pass"
    assert len(tail) <= 3, "the remainder is under 6px on each axis"
    for dx, dy in tail:
        assert max(abs(dx), abs(dy)) <= qmp.Monitor.FINE_MAX


def test_the_fine_pass_stays_under_the_threshold():
    """FINE_MAX and COARSE_MIN describe one measured boundary from both
    sides. If they ever disagree the walk has a gap or an overlap, and both
    show up as a move that is off by a few pixels."""
    assert qmp.Monitor.FINE_MAX == qmp.Monitor.COARSE_MIN - 1


def test_the_coarse_ceiling_is_what_qemu_will_carry():
    """qemu clamps a ps2 delta to 127; sending more travels no further and
    only makes the arithmetic lie about how far the packet went."""
    assert qmp.Monitor.COARSE_MAX == 127


def test_every_transport_offers_the_same_surface():
    """The three transports differ only in how a command reaches qemu.

    They used to differ in more than that: the methods were copied between
    classes by assignment, one class was missing `DOUBLE_GAP`, and `dclick`
    raised AttributeError through that one and worked through the other.
    Inheritance makes that unrepresentable; this says so out loud.
    """
    for cls in (qmp.SockMonitor, qmp.LibvirtMonitor, qmp.VirshMonitor):
        assert issubclass(cls, qmp.Monitor)
        assert cls.cmd is not qmp.Monitor.cmd, f"{cls.__name__} must send"
        for name in ("shot", "key", "type", "click", "move", "home",
                     "DOUBLE_GAP", "STEP_GAP"):
            assert getattr(cls, name) is getattr(qmp.Monitor, name)
