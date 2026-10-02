"""Drive the guest's desktop: read the control tree, click, type, capture.

The bridge half of `win/ui/`. Three duties, and only the first is obvious:

* issue a request and assemble the reply, the way `SyncService` does for
  `SYNCSCAN`;
* give every control a selector, because an HWND never leaves the guest --
  see `docs/UIAUTOMATION.md`, "The node record";
* keep what comes back small enough to put in front of a model. A desktop is
  83 windows and most of them are scrollbars.
"""

from __future__ import annotations

import asyncio
import logging

from ..guest import GuestSession, UiCall
from ..wire import protocol as P
from ..wire import uirec, uisel

log = logging.getLogger("bridge.ui")

#: A screenshot at 640x480x8 is 300KB and arrives in 62KB frames. Past this
#: the guest is producing something nobody asked for.
SHOT_MAX = 2 * 1024 * 1024

#: Records, before parsing. A tree of 4000 nodes at ~120 bytes is under
#: this; a guest that keeps sending past it has lost track of its own cap.
RECORDS_MAX = 1024 * 1024

#: The bridge's own budget sits above the guest's so the guest's answer
#: arrives first and says what happened -- a bridge that gives up first
#: reports a timeout for a request that was about to succeed.
BUDGET_SLACK_S = 20.0


class UiError(Exception):
    """A request the guest refused, carrying its status verbatim."""

    def __init__(self, status: str, detail: str = "",
                 headers: dict | None = None):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status = status
        self.detail = detail
        #: The rest of the UIEND. A refusal often carries the one fact that
        #: makes it actionable -- `on_top` for occluded says which window is
        #: in the way -- and dropping it left the advice generic.
        self.headers = headers or {}


class UiService:
    def __init__(self, connection):
        self.connection = connection

    # --- issuing ------------------------------------------------------

    async def _call(self, guest: GuestSession, frame, kind: str,
                    timeout_ms: int) -> tuple[dict, UiCall]:
        """Send one request and wait for its UIEND.

        Serialised per guest: there is one desktop worker over there, and
        letting two requests race only produces answers nobody still wants
        -- the guest would refuse the second with `busy` anyway.
        """
        async with guest.ui_lock:
            guest.ui_seq += 1
            ui_id = f"u{guest.ui_seq}"
            frame.headers["id"] = ui_id
            fut = asyncio.get_running_loop().create_future()
            call = UiCall(future=fut, kind=kind)
            guest.ui_pending[ui_id] = call
            try:
                await self.connection.send(guest, frame)
                headers = await asyncio.wait_for(
                    fut, timeout=(timeout_ms / 1000.0) + BUDGET_SLACK_S)
            except TimeoutError as exc:
                raise UiError(
                    "timeout",
                    "the guest did not answer; it may still be working"
                ) from exc
            finally:
                guest.ui_pending.pop(ui_id, None)

        status = headers.get("status", "")
        if status != "ok":
            raise UiError(status or "no_status", headers.get("detail", ""),
                          headers)
        return headers, call

    async def tree(self, guest: GuestSession, *, scope: str = "desktop",
                   pid: int = 0, max_nodes: int = 400, msaa: bool = False,
                   hidden: bool = False, wait_for: str = "",
                   wait_ms: int = 5000, settle_ms: int = 0,
                   timeout_ms: int = 8000, inproc: bool = False,
                   target: str = "", acc: bool = False) -> tuple[list, dict]:
        budget = timeout_ms + (wait_ms if wait_for else 0) + settle_ms * 4
        headers, call = await self._call(
            guest,
            P.ui_tree("", scope=scope, pid=pid, max_nodes=max_nodes,
                      msaa=msaa, hidden=hidden, wait_for=wait_for,
                      wait_ms=wait_ms, settle_ms=settle_ms,
                      timeout_ms=timeout_ms, inproc=inproc, target=target,
                      acc=acc),
            "tree", budget)

        nodes = uirec.parse_records(bytes(call.records), _acp(guest))
        # The guest counts what it SENT. A mismatch means records were
        # dropped in parsing, and a tree that is quietly short is worse than
        # one that is refused -- the agent concludes a control is absent.
        claimed = int(headers.get("nodes", "0") or 0)
        if claimed and len(nodes) != claimed:
            log.warning("ui: guest sent %d records, %d parsed", claimed,
                        len(nodes))
        uisel.build_selectors(nodes)
        return nodes, headers




    async def shot(self, guest: GuestSession, *, x: int = 0, y: int = 0,
                   w: int = 0, h: int = 0, bpp: int = 8,
                   timeout_ms: int = 30000) -> tuple[bytes, dict]:
        headers, call = await self._call(
            guest, P.ui_shot("", x=x, y=y, w=w, h=h, bpp=bpp),
            "shot", timeout_ms)
        got = bytes(call.data)
        claimed = int(headers.get("bytes", "0") or 0)
        if claimed and len(got) != claimed:
            raise UiError("short_image",
                          f"the guest sent {claimed} bytes, {len(got)} arrived")
        return got, headers

    async def run(self, guest: GuestSession, body: bytes, *,
                  timeout_ms: int, step_ms: int, settle: int = 0
                  ) -> tuple[dict, bytes]:
        """Run a compiled step list. Returns the UIEND headers -- status
        included, NOT raised on, because a list that failed at step 5 still
        ran four steps the agent needs to hear about -- and the per-step
        body.

        When the bridge's own budget runs out first it says so to the guest
        with UICANCEL, so a ten-minute list nobody is waiting for does not
        keep the desktop lane, and the keys it holds, for the rest of its
        time."""
        async with guest.ui_lock:
            guest.ui_seq += 1
            ui_id = f"u{guest.ui_seq}"
            fut = asyncio.get_running_loop().create_future()
            call = UiCall(future=fut, kind="run")
            guest.ui_pending[ui_id] = call
            try:
                await self.connection.send(
                    guest, P.ui_run(ui_id, body, timeout_ms=timeout_ms,
                                    step_ms=step_ms, settle=settle))
                try:
                    headers = await asyncio.wait_for(
                        asyncio.shield(fut),
                        timeout=(timeout_ms / 1000.0) + BUDGET_SLACK_S)
                except TimeoutError:
                    await self._cancel(guest, ui_id)
                    try:
                        # The guest answers a cancel within a second or so;
                        # its reply says how far the list got.
                        headers = await asyncio.wait_for(fut, timeout=5.0)
                    except TimeoutError as exc:
                        raise UiError(
                            "timeout", "the guest did not answer, even to a "
                            "cancel; it may still be working") from exc
                except asyncio.CancelledError:
                    # The turn was abandoned. The list must not run on.
                    await self._cancel(guest, ui_id)
                    raise
            finally:
                guest.ui_pending.pop(ui_id, None)
        return headers, bytes(call.data)

    async def _cancel(self, guest: GuestSession, ui_id: str) -> None:
        try:
            await self.connection.send(guest, P.ui_cancel(ui_id))
        except Exception:                 # best effort: the link may be gone
            log.warning("ui: could not send UICANCEL for %s", ui_id)

    # --- receiving ----------------------------------------------------

    async def ui_frame(self, guest: GuestSession, f) -> None:
        """Route one UI* reply to whoever is waiting for it."""
        ui_id = f.headers.get("id", "")
        call = guest.ui_pending.get(ui_id)
        if call is None:
            # Late, or cancelled. Logged and dropped: a KeyError here would
            # take the connection down, which is how the sync path learned
            # to look before it indexed.
            log.debug("ui frame %s for %s, which nobody awaits", f.type, ui_id)
            return

        if f.type == "UIENT":
            if call.kind != "tree":
                log.warning("UIENT for %s, which is a %s -- ignoring", ui_id,
                            call.kind)
                return
            if len(call.records) + len(f.body) > RECORDS_MAX:
                call.capped = True
                return
            call.records.extend(f.body)

        elif f.type == "UIDAT":
            if call.kind != "shot":
                log.warning("UIDAT for %s, which is a %s -- ignoring", ui_id,
                            call.kind)
                return
            if len(call.data) + len(f.body) > SHOT_MAX:
                call.capped = True
                return
            call.data.extend(f.body)

        elif f.type == "UIEND":
            # A step list's per-step lines ride its UIEND's own body.
            if call.kind == "run" and f.body:
                call.data.extend(f.body[:SHOT_MAX])
            if not call.future.done():
                call.future.set_result(dict(f.headers))

    def fail_calls(self, guest: GuestSession | None, why: str) -> None:
        """The connection went. Every waiter is told, rather than left to
        time out one budget at a time."""
        if guest is None:
            return
        for ui_id, call in list(guest.ui_pending.items()):
            if not call.future.done():
                call.future.set_exception(UiError("disconnected", why))
            guest.ui_pending.pop(ui_id, None)


def _acp(guest: GuestSession) -> str:
    """The guest's ANSI codepage. Window text is ANSI, not OEM -- conflating
    the two is the classic Win9x encoding bug, and `fold.decode`'s docstring
    names window titles specifically."""
    caps = getattr(guest, "caps", None)
    return getattr(caps, "acp", "") or ""
