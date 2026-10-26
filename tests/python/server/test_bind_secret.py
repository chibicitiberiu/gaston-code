r"""The bridge refuses to serve the network on a secret everybody has.

`bridge/server.py` used to warn and carry on: no GASTON_SECRET meant
`dev-secret`, which is a literal in this repository, and
`win/app/config.cpp:9` compiles the same string as the CLIENT's default
with no refusal of its own. So a client whose ini had lost its `secret=`
line and a bridge with nothing in the environment agreed on a published
value and connected, authenticated by nothing.

That is not a weak first factor, it is the only one. A peer that completes
the handshake sends its own permission modes in PREFS; `policy.lookup`
falls through to that table and `prefs.floor` ratchets only
`lists.protect`, never a mode value. One frame declaring a mode with
`bridge_shell=allow` is unprompted shell on this host.

`win/remote/config.cpp:210-214` has held the opposite line since it was
written -- gremote refuses an absent secret AND refuses the shipped
default -- and these pin the same two refusals here. Loopback is left
alone: a developer on 127.0.0.1 is the case the placeholder exists for.
"""

from __future__ import annotations

import pytest

from bridge.server import DEV_SECRET, binds_loopback, load_config, parse_args


def _cfg(argv, monkeypatch, secret):
    if secret is None:
        monkeypatch.delenv("GASTON_SECRET", raising=False)
    else:
        monkeypatch.setenv("GASTON_SECRET", secret)
    return load_config(parse_args(argv))


# --- the predicate ----------------------------------------------------------
#
# Every row here is a spelling `ipaddress.ip_address()` gets wrong, and it
# gets them wrong in BOTH directions -- which is why the predicate resolves
# instead of parsing. '' and '0' raise ValueError and listen on every
# interface; 'localhost' and '127.1' raise ValueError and are loopback.

@pytest.mark.parametrize("host", ["", "0", "0.0.0.0", "::"])
def test_wildcard_spellings_are_not_loopback(host):
    assert binds_loopback(host, 7890) is False


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.1", "::1"])
def test_loopback_spellings_are_loopback(host):
    assert binds_loopback(host, 7890) is True


def test_a_routable_address_is_not_loopback():
    assert binds_loopback("10.0.0.158", 7890) is False


def test_a_name_that_does_not_resolve_declines_to_refuse():
    """The bind is about to fail and say so properly. Refusing here would
    replace a clear error with a confusing one, and a listener that never
    starts exposes nothing."""
    assert binds_loopback("no-such-host.invalid", 7890) is True


# --- the refusals -----------------------------------------------------------

def test_loopback_without_a_secret_still_works(monkeypatch):
    """The placeholder exists for this case and keeps working."""
    c = _cfg(["--no-sync"], monkeypatch, None)
    assert c.secret == DEV_SECRET


def test_outward_bind_without_a_secret_refuses(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _cfg(["--host", "0.0.0.0", "--no-sync"], monkeypatch, None)
    assert "GASTON_SECRET" in str(e.value)


def test_outward_bind_with_the_shipped_default_refuses(monkeypatch):
    """The check an earlier draft of this did not make. Refusing only an
    UNSET secret passes `GASTON_SECRET=dev-secret gaston-bridge --host
    0.0.0.0`, which is exactly as exposed as the case it exists to stop:
    a speed bump rather than a gate."""
    with pytest.raises(SystemExit) as e:
        _cfg(["--host", "0.0.0.0", "--no-sync"], monkeypatch, DEV_SECRET)
    assert DEV_SECRET in str(e.value)


def test_outward_bind_with_a_real_secret_is_allowed(monkeypatch):
    c = _cfg(["--host", "0.0.0.0", "--no-sync"], monkeypatch, "s3cret-of-mine")
    assert c.secret == "s3cret-of-mine"


@pytest.mark.parametrize("host", ["", "0"])
def test_the_wildcard_spellings_that_do_not_parse_are_refused(monkeypatch, host):
    """These are the ones a parse-only predicate waved through."""
    with pytest.raises(SystemExit):
        _cfg(["--host", host, "--no-sync"], monkeypatch, None)


@pytest.mark.parametrize("host", ["localhost", "127.1"])
def test_the_loopback_spellings_that_do_not_parse_are_allowed(monkeypatch, host):
    """And these are the ones it refused."""
    c = _cfg(["--host", host, "--no-sync"], monkeypatch, None)
    assert c.secret == DEV_SECRET
