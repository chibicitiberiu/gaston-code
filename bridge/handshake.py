"""HELLO negotiation: the checks between "a TCP connection landed" and "a
GuestSession may be built or resumed for it".

Before this, `ConnectionService.attach` (`server.py`) did five separable things in one
115-line method: read the first frame under a deadline (still `_read_hello`,
not here), check version and frame cap, compare the secret, parse and
validate caps, and only then attach -- create or resume the `GuestSession`
and run the reissue sequence. The four checks in the middle are pure over
the HELLO headers and the bridge's own secret; nothing about them needs a
socket, a `GuestSession`, or the rest of `Bridge`'s state, which is exactly
why `test_server_integration.py`'s real-socket tests were the only way to
reach any of them. `negotiate` below is directly callable in a unit test.

Deliberately NOT here: the busy check (`ConnectionService.guest and
ConnectionService.guest.connected`). It answers "is another client already holding
THIS bridge's one session", which is live process state, not a fact about
the HELLO frame -- and it is also exactly the check R7 (multi-client) has to
grow a `client_id` lookup into (docs/audit/02-bridge-python.md #7.1), so it
stays with `_attach`, the session-lifecycle half, rather than moving here
with the stateless half. One consequence worth stating plainly: negotiation
now runs to completion (including the caps parse) before the busy check
does, where the caps parse used to run after it. Nothing tests the order
between "busy" and "bad_caps", and no client can usefully tell them apart --
both mean "try again, but not like that" -- so the reorder is judged safe
rather than proven so.
docs/audit/02-bridge-python.md #1.2.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from .files import caps as C
from .view.pages import limits_for
from .wire.protocol import MAX_FRAME, PROTO_VERSION


class Refused(Exception):
    """A HELLO this bridge will not accept.

    `code`/`message` are exactly what `wire.protocol.error(code, message)`
    wants on the wire -- `_attach` does nothing but forward them.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Handshake:
    """What negotiation decided, for `_attach` to build or resume a session
    from."""

    caps: C.Caps
    page_bytes: int
    page_blocks: int
    mem_kb: int


def _int(headers: dict, name: str, code: str) -> int:
    """One numeric HELLO header, or `Refused`.

    Every other thing wrong with a HELLO leaves the guest holding a typed
    error frame it can render. A header that is not a number has to do the
    same, or it is the one refusal that looks like the bridge crashing.
    """
    raw = headers.get(name, "0") or "0"
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise Refused(
            code, f"{name} is not a number: {str(raw)[:40]!r}") from exc


def negotiate(headers: dict, secret: str) -> Handshake:
    """Check version, frame cap, secret and caps against one HELLO's
    headers; raise `Refused` on the first thing wrong.

    Order matches the previous inline checks in `ConnectionService.attach`: a version
    mismatch is reported ahead of a bad secret, because a guest speaking the
    wrong protocol version needs to know that before anything else it is
    told can be trusted.
    """
    if headers.get("ver") != str(PROTO_VERSION):
        raise Refused(
            "bad_version",
            f"bridge speaks {PROTO_VERSION}, guest speaks {headers.get('ver')}")

    # Checked together with the version: inheriting a peer's idea of the cap
    # is how you end up allocating 32MB on a 32MB machine.
    #
    # PARSED INSIDE A TRY, like `mem_kb` below, because `int()` raises
    # `ValueError` and `_attach` catches only `Refused`. A guest sending
    # `max_frame=64k` used to escape negotiation entirely: the traceback came
    # out of `_on_client`'s catch-all, which closes the socket WITHOUT
    # sending an ERROR frame, so the guest saw a bare disconnect, applied its
    # 1/2/4/8s backoff and retried forever while the bridge logged a
    # traceback per attempt. This line runs BEFORE the secret is compared, so
    # that was also a log flood any unauthenticated peer could trigger.
    peer_cap = _int(headers, "max_frame", "bad_frame_cap")
    if peer_cap != MAX_FRAME:
        raise Refused(
            "bad_frame_cap",
            f"bridge caps at {MAX_FRAME}, guest says {peer_cap}")

    if not secrets.compare_digest(headers.get("secret", ""), secret):
        raise Refused("bad_secret", "rejected")

    try:
        caps = C.from_hello(headers)
    except C.CapsError as exc:
        raise Refused("bad_caps", str(exc)[:200]) from exc

    # Page size is scaled to the guest's RAM. A 128MB machine can hold a far
    # bigger page than the 32MB floor the design is built around, and the
    # handshake is the only place the bridge learns which it has.
    mem_kb = _int(headers, "mem_kb", "bad_mem")
    page_bytes, page_blocks = limits_for(mem_kb)

    return Handshake(caps=caps, page_bytes=page_bytes,
                     page_blocks=page_blocks, mem_kb=mem_kb)
