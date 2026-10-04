"""The client's settings: the PREFS frame, at the handshake and re-sent.

PER CLIENT (docs/OPTIONS-PLAN.md): the client holds every setting and
declares it at each handshake, so a reconnect is always its current answer
and a client that sends none gets the defaults -- never the last client's.
"""
from __future__ import annotations

import asyncio

import pytest
from harness import harness  # noqa: F401

from bridge import prefs as P
from bridge.agent import categories as K
from bridge.wire.protocol import Frame

# --- parsing ------------------------------------------------------------------


def test_a_full_body_parses():
    p = P.parse(b"general.mode=auto\ngeneral.model=haiku\nview.page=large\n"
                b"sync.sweep_s=-1\nsync.max_kb=2048\nsync.ignore=*.obj\n"
                b"sync.ignore=a=b\nperm.deadline_s=300\n"
                b"protect=C:\\WINDOWS\\**\ntrust=wcl386\n"
                b"danger.format=erases a disk\n"
                b"mode.u-0a1b2c3d.label=Mine\nmode.u-0a1b2c3d.c.run_else=off\n")
    assert p.general_mode == "auto" and p.page == "large"
    assert p.sync_sweep_s == -1 and p.sync_max_kb == 2048
    assert p.sync_ignore == ("*.obj", "a=b")      # split on the FIRST =
    assert p.lists.protect == ("C:\\WINDOWS\\**",)
    assert p.lists.trust == {"wcl386"}
    assert p.lists.danger == {"format": "erases a disk"}
    m = p.modes["u-0a1b2c3d"]
    assert m.value(K.RUN_ELSE) == "off" and m.value(K.READ) == "ask"
    assert p.declared


@pytest.mark.parametrize("body,key", [
    (b"nonsense=1", "nonsense"),
    (b"view.page=huge", "view.page"),
    (b"general.effort=ultra", "general.effort"),
    (b"sync.sweep_s=x", "sync.sweep_s"),
    (b"mode.u-1.label=x", "mode.u-1.label"),
    (b"mode.u-0a1b2c3d.c.nope=ask", "mode.u-0a1b2c3d.c.nope"),
    (b"mode.u-0a1b2c3d.c.read=maybe", "mode.u-0a1b2c3d.c.read"),
    (b"trust=a b", "trust"),
    (b"no equals sign", "no equals sign"),
])
def test_an_unknown_setting_is_refused_never_guessed(body, key):
    with pytest.raises(P.PrefsError) as exc:
        P.parse(body)
    assert exc.value.key == key


def test_lists_are_bounded_by_refusal_not_truncation():
    body = b"\n".join(b"protect=C:\\X%d" % i for i in range(P.MAX_LIST + 1))
    with pytest.raises(P.PrefsError):
        P.parse(body)
    many = b"\n".join(b"mode.u-%08x.label=m" % i
                      for i in range(P.MAX_MODES + 1))
    with pytest.raises(P.PrefsError):
        P.parse(many)


def test_a_mode_without_a_label_is_refused():
    with pytest.raises(P.PrefsError):
        P.parse(b"mode.u-0a1b2c3d.c.read=allow")


def test_the_protected_list_never_shrinks_inside_a_session():
    old = P.parse(b"protect=A\nprotect=B")
    new = P.parse(b"protect=B\nprotect=C")
    merged, held = P.floor(old, new)
    assert set(merged.lists.protect) == {"A", "B", "C"}
    assert held == ["A"]


def test_page_size_scales_within_the_ceiling():
    from bridge.view import pages
    big = P.parse(b"view.page=large")
    b, k = P.scaled(big, pages.MAX_BYTES * 4, pages.MAX_BLOCKS * 4)
    assert b == pages.MAX_BYTES * 4 and k == pages.MAX_BLOCKS * 4
    small = P.parse(b"view.page=small")
    assert P.scaled(small, 16384, 90) == (8192, 45)


# --- at the handshake -----------------------------------------------------------


async def _options(harness, r):
    return await harness.recv_until(r, {"OPTIONSEND"}, 5.0)


async def test_a_client_that_declares_prefs_has_them_applied(harness):
    r, w = await harness.connect(prefs=b"general.mode=auto\n"
                                       b"protect=C:\\WINDOWS\\**")
    await _options(harness, r)
    g = harness.bridge.connection.guest
    assert g.prefs.declared and g.prefs.general_mode == "auto"
    assert g.prefs.lists.protect == ("C:\\WINDOWS\\**",)
    # A new chat starts in the client's default mode.
    assert g.mode_id == "auto"
    await harness.close(w)


async def test_a_legacy_client_is_not_kept_waiting_and_gets_defaults(
        harness):
    """B9: waiting for a frame an older client never sends would hold it
    for the whole hello deadline. And B2: it must not inherit the previous
    client's settings."""
    r, w = await harness.connect(prefs=b"general.mode=auto")
    await _options(harness, r)
    await harness.close(w)
    await asyncio.sleep(0.1)

    t0 = asyncio.get_running_loop().time()
    r, w = await harness.connect()
    await _options(harness, r)
    assert asyncio.get_running_loop().time() - t0 < 2.0
    g = harness.bridge.connection.guest
    assert not g.prefs.declared
    assert g.prefs.lists.protect == ()
    await harness.close(w)


async def test_bad_prefs_refuse_the_handshake_with_a_reason(harness):
    r, _w = await harness.connect(prefs=b"view.page=huge")
    frames = await harness.recv_until(r, {"ERROR"}, 5.0)
    err = frames[-1]
    assert err.headers["code"] == "bad_prefs"
    assert "view.page" in err.headers["msg"]


async def test_a_frame_other_than_prefs_after_declaring_it_is_refused(
        harness):
    r, w = await asyncio.open_connection("127.0.0.1", harness.port)
    from bridge.wire.protocol import hello
    f = hello("s")
    f.headers["prefs"] = "1"
    w.write(f.encode())
    w.write(Frame("PING", {}).encode())
    await w.drain()
    frames = await harness.recv_until(r, {"ERROR"}, 5.0)
    assert frames[-1].headers["code"] == "expected_prefs"


# --- re-sent from Options OK ----------------------------------------------------


async def test_resent_prefs_are_acknowledged_and_applied(harness):
    r, w = await harness.connect(prefs=b"")
    await _options(harness, r)
    await harness.send(w, Frame("PREFS", {}, b"general.model=haiku\n"
                                             b"mode.u-0a1b2c3d.label=Mine"))
    frames = await harness.recv_until(r, {"OPTIONSEND"}, 5.0)
    ack = [f for f in frames if f.type == "PREFSACK"]
    assert ack and ack[0].headers["ok"] == "1"
    modes = [f.headers.get("id") for f in frames
             if f.type == "OPTION" and f.headers.get("kind") == "mode"]
    assert "u-0a1b2c3d" in modes, "a user mode joins the Mode menu"
    await harness.close(w)


async def test_a_bad_resend_is_refused_by_prefsack_not_error(harness):
    """An ERROR mid-turn aborts the client's turn UI; a setting that did not
    take is not a failed turn."""
    r, w = await harness.connect(prefs=b"")
    await _options(harness, r)
    await harness.send(w, Frame("PREFS", {}, b"view.page=huge"))
    frames = await harness.recv_until(r, {"PREFSACK"}, 5.0)
    assert not any(f.type == "ERROR" for f in frames)
    assert frames[-1].headers["ok"] == "0"
    assert frames[-1].headers["key"] == "view.page"
    await harness.close(w)


async def test_a_chat_whose_mode_was_deleted_falls_back(harness):
    body = b"mode.u-0a1b2c3d.label=Mine\nmode.u-0a1b2c3d.c.read=allow"
    r, w = await harness.connect(prefs=body)
    await _options(harness, r)
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("SETMODE", {"mode": "u-0a1b2c3d"}))
    await harness.recv_until(r, {"OPTIONSEND"}, 5.0)
    assert g.mode_id == "u-0a1b2c3d"
    await harness.send(w, Frame("PREFS", {}, b""))
    await harness.recv_until(r, {"OPTIONSEND"}, 5.0)
    assert g.mode_id == "manual"
    await harness.close(w)


async def test_a_removed_protected_place_stays_until_reconnect(harness):
    r, w = await harness.connect(prefs=b"protect=C:\\WINDOWS\\**")
    await _options(harness, r)
    g = harness.bridge.connection.guest
    await harness.send(w, Frame("PREFS", {}, b"protect=C:\\OTHER\\**"))
    await harness.recv_until(r, {"OPTIONSEND"}, 5.0)
    assert set(g.prefs.lists.protect) == {"C:\\WINDOWS\\**", "C:\\OTHER\\**"}
    await harness.close(w)
