"""A Linux stand-in for retroclaude.exe that speaks the real wire protocol.

This is the highest-leverage file in the project. It means the entire bridge can
be built and tested before any Win98 code exists, and afterwards it stays as:

  - the protocol conformance harness (if fakegui works and the guest does not,
    the bug is in the C, not the bridge),
  - the break-glass tool when a permission prompt has the session wedged.

Conformance means matching win/app/main.c's render_frame frame-for-frame: TEXT
is rendered by `mode=`, never the `style=` header the bridge stopped sending
when the run-based renderer was replaced by the HTML one, and a `deflate` body
is inflated through ONE persistent stream per connection, created and torn down
exactly where win/net/net.c's inflate_begin()/inflate_finish() do -- decoding
out of order, or with a fresh stream per frame, is not what the guest does and
would hide the very drift this tool exists to catch.

EXEC*/SYNC* frames never reach render_frame on the real guest either --
win/net/net.c's reader routes them straight to the RPC worker
(win/rpc/rpc.c, win/sync/syncrpc.c) before the UI thread ever sees them -- so
fakegui intercepts them the same way and answers them the way a guest with no
configured sync root genuinely would: EXEC actually runs (a subprocess,
streamed back as EXECOUT/EXECRESULT, killable by EXECKILL), and every SYNC*
frame gets the honest `no_root`/equivalent answer, which is what SYNCSCAN
already returns on a real guest with no root configured -- rather than this
script reimplementing the manifest-and-CRC machinery in win/sync/syncrpc.c,
which is not what a ~200 line break-glass tool is for.

No longer ~200 lines -- restoring conformance cost the length back. Still
resist making it a framework.

    python -m bridge.fakegui --port 7890
    /quit  /stop  /model <name>  /resume  /raw  /continue  /allow  /always  /deny
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
import tempfile

from ..wire.compress import (
    StreamCompressor, StreamDecompressor, LEVEL_UP, MIN_WORTH_COMPRESSING
)
from ..wire.protocol import Frame, FrameReader, ProtocolError, hello

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"

# win/rpc/exec.c's EXEC_MAX_OUT: the guest never reports more output than
# this, so answering with more here would exercise a path the guest cannot.
EXEC_MAX_OUT = 32 * 1024

# win/net/net.cpp's rc_net_start sends 22 HELLO headers beyond ver/secret/
# max_frame/sess -- the ones hello() builds. A fakegui that skips them
# only ever drives the bridge's UNDECLARED defaults (bridge/files/caps.py's
# Caps(declared=False), manifest=1), never the DECLARED path the real guest
# exercises on every connection (manifest=2, and every cap explicitly named
# rather than assumed). 03.28: the two test populations did not overlap.
#
# Literal for literal against win/net/caps.h's RC_CAP_* -- this client claims
# to be the same Win9x guest net.cpp actually is, since that is what fakegui
# stands in for. Mirrored by hand, like bridge/files/caps.py already mirrors
# the same header; change one, change the other two.
FAKE_CAPS = {
    "acp": "1252",
    "oemcp": "437",
    "case": "1",        # RC_CASE_FOLD
    "devices": "1",     # RC_DEVICES_DOS
    "trailing": "1",    # RC_TRAILING_STRIP
    "illegal": "1",     # RC_ILLEGAL_DOS
    "eol": "1",         # RC_EOL_CRLF
    "shortnames": "1",  # RC_SHORTNAMES_83
    "dirreserve": "12", # RC_DIRRESERVE_WIN32
    "workreserve": "11",# RC_WORKRESERVE_GASTON
    "maxpath": "260",   # MAX_PATH
    "maxcomp": "255",
    "manifest": "2",    # RC_CAP_MANIFEST
    "maxdepth": "8",    # RC_CAP_MAXDEPTH
    "deflate": "1",     # RC_CAP_DEFLATE
}

# The rest of what rc_net_start reports about the machine itself -- not a
# capability, but read by bridge/view/pages.py's limits_for (mem_kb) and by
# bridge/machine.py's system prompt (os/cpu/screen). 32MB/2420KB match the
# floor CLAUDE.md's "Memory is the binding constraint" section measures
# against, so a page-sizing bug that only shows up on the 32MB guest still
# shows up here. No sync_root: fakegui answers every SYNC* frame the way a
# guest with no configured sync root genuinely would (see _handle_rpc below),
# and a client that names a root while claiming to have none would be lying
# to itself before the bridge ever gets a chance to.
FAKE_IDENTITY = {
    "mem_kb": "32768",
    "mem_free_kb": "2420",
    "os": "Windows 4.10 build 67766095 ",
    "cpu": "x86 family 5 model 8, 1 cpu",
    "screen": "640x480x8",
    "disk_free_kb": "512000",
}


class FakeGui:
    def __init__(self, host: str, port: int, secret: str, *, raw: bool = False):
        self.host = host
        self.port = port
        self.secret = secret
        self.raw = raw
        # The id of the permission request currently on screen, if any.
        self.perm_id: str | None = None
        # The id of the sync halt bar currently on screen, if any -- the
        # guest's g_halt_id. /continue answers it with RESUME.
        self.halt_id: str | None = None
        self.sess: str | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._turn_active = False
        # One persistent inflate stream per CONNECTION -- recreated in
        # connect(), which runs on both the first connect and every /resume,
        # exactly where win/net/net.c calls inflate_begin()/inflate_finish().
        self.zip_in: StreamDecompressor | None = None
        self.zip_out: StreamCompressor | None = None
        self._live_lines = 0   # terminal lines the current LIVE block occupies
        self._doc_seen = False
        self.page_no = 0
        self.page_total = 0
        # The one command the RPC worker runs at a time, mirroring
        # win/rpc/rpc.c's one queued job slot: a second EXEC arriving while
        # this is set is answered `busy`, never queued.
        self._exec_task: asyncio.Task | None = None
        self._exec_id: str | None = None
        # The keepalive sweep, mirroring win/rpc/rpc.cpp's rpc_maybe_nudge.
        self._sweep: asyncio.Task | None = None
        # An export in flight: the temp file being written, where it goes,
        # and the two counters EXPORTEND checks. Mirrors the three statics in
        # win/app/export.cpp (tmp/dest/got/total) because that is what
        # conformance means here -- see _export.
        self._exp = None
        #: Where the last FINISHED export landed, or None. The guest shows
        #: this in a message box; here it is the one piece of the transfer
        #: worth keeping after the temp file is closed, and it is what a
        #: caller (or a test) has to look at, since `_exp` is gone by then.
        self.export_path: str | None = None

    # --- connection --------------------------------------------------------

    async def connect(self, resume: bool = False) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port
        )
        self.zip_in = StreamDecompressor()
        self.zip_out = StreamCompressor(level=LEVEL_UP)
        f = hello(
            self.secret,
            sess=self.sess if resume else None,
        )
        # hello() only builds ver/secret/max_frame/sess -- the fields
        # every client shares. Everything past that is this client saying
        # what it IS, which protocol.py has no constructor for because it is
        # not part of the handshake's shape, only this client's answer to it.
        f.headers.update(FAKE_CAPS)
        f.headers.update(FAKE_IDENTITY)
        self.writer.write(f.encode())
        await self.writer.drain()
        if self._sweep is None or self._sweep.done():
            self._sweep = asyncio.create_task(self._keepalive())

    async def close(self) -> None:
        if self._sweep is not None:
            self._sweep.cancel()
            self._sweep = None
        if self.writer:
            # Match what the guest must do: a graceful FIN, never an abrupt
            # close. An RST is reported to crash Win98's Winsock stack.
            try:
                if self.writer.can_write_eof():
                    self.writer.write_eof()
            except Exception:
                pass
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

    async def send(self, f: Frame) -> None:
        if not self.writer:
            return
        is_bulk = f.type in ("EXECOUT", "SYNCDAT", "SYNCENT")
        if (self.zip_out and is_bulk and f.body
                and len(f.body) >= MIN_WORTH_COMPRESSING):
            f = Frame(f.type, dict(f.headers, enc="deflate"),
                      self.zip_out.compress(f.body))
        self.writer.write(f.encode())
        await self.writer.drain()

    #: win/rpc/rpc.cpp's RPC_SWEEP_MS. The number matters: the bridge reaps a
    #: guest silent for 90 seconds, and the guest's own answer to that is a
    #: frame every 30.
    SWEEP_S = 30.0

    async def _keepalive(self) -> None:
        """What rpc_maybe_nudge does when there is no sync root. 02.16.

        fakegui sent PING only from the typed `/ping`, so a break-glass
        session left idle while somebody reads a permission prompt was reaped
        at 90 seconds -- against the documented 600-second deadline for
        answering that very prompt -- and the conformance harness never
        exercised the keepalive at all. That is the exact failure the guest's
        own sweep was added to fix (rpc.cpp), in the tool whose stated job is
        "if fakegui works and the guest does not, the bug is in the C".

        The real guest sends SYNCDIRTY INSTEAD of PING when it has a sync
        root; fakegui has none, so PING is its whole sweep. It also sends
        STAT from the same tick when bytes have moved, which this does not:
        that is a remaining difference, written down here rather than left to
        be rediscovered.
        """
        while True:
            await asyncio.sleep(self.SWEEP_S)
            if not self.writer or self.writer.is_closing():
                return
            await self.send(Frame("PING", {}))

    # --- receive -----------------------------------------------------------

    async def pump(self) -> None:
        assert self.reader
        frames = FrameReader()
        try:
            while True:
                data = await self.reader.read(8192)
                if not data:
                    print(f"\n{DIM}-- bridge closed the connection --{RESET}")
                    return
                for f in frames.feed(data):
                    self._inflate(f)
                    # EXEC*/SYNC* never reach render_frame on the real guest
                    # either -- net.c's reader routes them to the RPC worker
                    # before the UI thread sees them. Mirror that split here.
                    if f.type in ("EXEC", "EXECKILL") or f.type.startswith("SYNC"):
                        asyncio.create_task(self._handle_rpc(f))
                    else:
                        self.render(f)
        except ProtocolError as exc:
            print(f"\n{RED}protocol error: {exc}{RESET}")
        except (ConnectionResetError, asyncio.IncompleteReadError):
            print(f"\n{DIM}-- connection reset --{RESET}")

    def _inflate(self, f: Frame) -> None:
        """Inflate f.body in place if enc=deflate, through the one persistent
        stream for this connection -- never a fresh one, never out of order.
        A failure here is exactly what win/net/net.c treats as fatal: drop
        the link rather than guess at a desynchronised stream."""
        if f.headers.get("enc", "raw") != "deflate":
            return
        if self.zip_in is None:
            raise ProtocolError("deflate frame arrived with no inflater")
        try:
            f.body = self.zip_in.decompress(f.body)
        except Exception as exc:
            raise ProtocolError(f"cannot inflate frame: {exc}") from exc

    # --- rendering: mirrors win/app/main.c's render_frame ------------------

    def render(self, f: Frame) -> None:
        if self.raw:
            body = f.body[:80]
            print(f"{DIM}[{f.type} {f.headers} {body!r}]{RESET}")
            # NOT a return for EXPORT*. `--raw` says "dump frames instead of
            # rendering them", and an export is not a rendering: the file
            # still has to be written, the same way `--raw` does not stop
            # EXEC from running. Dumping the frame and dropping the transfer
            # would make the debugging mode fail differently from the mode
            # being debugged.
            if not f.type.startswith("EXPORT"):
                return

        t = f.type

        # A file transfer, not a rendering. Decoding the body as text first
        # would be meaningless for a deflated chunk -- and printing
        # "[EXPORTDAT {...}] <200 chars of deflated body>" per 63 KB chunk is
        # exactly what this used to do.
        if t.startswith("EXPORT"):
            self._export(f)
            return

        text = f.body.decode("cp1252", errors="replace")

        if t == "READY":
            self.sess = f.headers.get("sess")
            self.page_no = int(f.headers.get("page", "0") or 0)
            self.page_total = int(f.headers.get("total", "0") or 0)
            print(f"{GREEN}connected{RESET} {DIM}sess={self.sess} "
                  f"model={f.headers.get('model')}{RESET}")
        elif t == "DOC":
            # The stylesheet/skeleton for the MSHTML control. fakegui has no
            # HTML control to hand it to; noted once, like g_doc_sent guards.
            if not self._doc_seen:
                self._doc_seen = True
                print(f"{DIM}[document skeleton received, {len(f.body)}B]{RESET}")
        elif t == "TEXT":
            # "live" replaces the block still being written; anything else is
            # a finished block, appended and never touched again -- the same
            # split render_frame makes on `mode`, never the `style` header
            # the bridge stopped sending.
            mode = f.headers.get("mode", "append")
            if mode == "live":
                self._show_live(text)
            else:
                self._clear_live()
                sys.stdout.write(text)
                if text and not text.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
        elif t == "TOOL":
            # The bridge sends tool activity as a finished block now; this
            # branch only survives for older bridges, same as the guest's.
            pass
        elif t == "TURN":
            state = f.headers.get("state")
            if state == "start":
                self._turn_active = True
                print(f"{DIM}...{RESET}")
            elif state == "end":
                self._turn_active = False
                cost = f.headers.get("cost_usd") or "?"
                ms = f.headers.get("ms", "?")
                ok = f.headers.get("ok") == "1"
                mark = GREEN + "done" if ok else RED + f.headers.get("subtype", "failed")
                print(f"\n{DIM}[{mark}{DIM} {ms}ms ${cost}]{RESET}\n")
            elif state == "error":
                self._turn_active = False
                print(f"\n{RED}turn error: {f.headers.get('msg')}{RESET}\n")
        elif t == "OPTION":
            kind = f.headers.get("kind", "?")
            oid = f.headers.get("id", "?")
            label = f.headers.get("label", oid)
            mark = "*" if f.headers.get("current") == "1" else " "
            grey = " (unavailable)" if f.headers.get("disabled") == "1" else ""
            print(f"{DIM}  [{kind}] {mark} {oid} {label}{grey}{RESET}")
        elif t == "OPTIONSEND":
            print(f"{DIM}[end of model/effort options]{RESET}")
        elif t == "SESSIONS":
            print(f"{DIM}-- chat list --{RESET}")
        elif t == "SESSION":
            sid = f.headers.get("id", "?")
            title = f.headers.get("title") or sid
            mark = "*" if f.headers.get("current") == "1" else " "
            grp = f.headers.get("group")
            group = f" [{grp}]" if grp else ""
            print(f"{DIM} {mark} {sid}: {title}{group}{RESET}")
        elif t == "SESSIONSEND":
            print(f"{DIM}-- end of chat list --{RESET}")
        elif t == "SWITCHED":
            self.sess = f.headers.get("id") or self.sess
            self.page_total = int(f.headers.get("total", "0") or 0)
            self.halt_id = None   # a switch rebuilds the session; see below
            print(f"{GREEN}switched{RESET} {DIM}sess={self.sess}{RESET}")
        elif t == "PAGESTART":
            self.page_no = int(f.headers.get("page", "0") or 0)
            self.page_total = int(f.headers.get("total", "0") or 0)
            cont = " (continued from the previous page)" \
                if f.headers.get("continued") == "1" else ""
            print(f"{DIM}-- page {self.page_no}/{self.page_total}{cont} --{RESET}")
        elif t == "PAGEEND":
            print(f"{DIM}-- end of page {self.page_no} --{RESET}")
        elif t == "PAGESEALED":
            self.page_total = int(f.headers.get("total", "0") or 0)
            print(f"{DIM}[page {f.headers.get('page')} sealed; "
                  f"{self.page_total} pages total]{RESET}")
        elif t == "ANCHOR":
            print(f"{DIM}[anchor page={f.headers.get('page')} "
                  f"preview={f.headers.get('preview', '')!r}]{RESET}")
        elif t == "NOTICE":
            print(f"{YELLOW}[{f.headers.get('kind')}] {text}{RESET}")
        elif t == "RESYNC":
            print(f"{YELLOW}[resync needed: {f.headers.get('reason')}]{RESET}")
        elif t == "ERROR":
            print(f"{RED}[{f.headers.get('code')}] {f.headers.get('msg')}{RESET}")
        elif t == "PONG":
            print(f"{DIM}pong{RESET}")
        elif t in ("PERMREQ", "PERMSTATE"):
            # The break-glass case this tool exists for. Without it a wedged
            # prompt can only be cleared by restarting the bridge.
            #
            # 03.29: `waiting` is NOT read here, on purpose, even though the
            # bridge sends it on this frame too. win/app/main.cpp's PERMREQ/
            # PERMSTATE branch (main.cpp:914-921) calls perm_show() with only
            # id/body/allows -- the queue depth is read nowhere but the
            # PERMWAIT branch below, which is the only frame that can ever
            # change it once a bar is already up. Reading it here would
            # exercise a header interpretation the shipping guest does not
            # share, and would show a stale count besides: this bar's own
            # queue depth is whatever the last PERMWAIT said, not whatever
            # this PERMREQ happens to carry.
            self.perm_id = f.headers.get("id")
            again = " (re-issued)" if t == "PERMSTATE" else ""
            print(f"\n{YELLOW}{BOLD}PERMISSION{RESET}{again} "
                  f"{BOLD}{text}{RESET}")
            opts = "/allow  /deny" if f.headers.get("allows") != "1" \
                else "/allow  /always  /deny"
            print(f"{DIM}  {opts}{RESET}")
        elif t == "PERMWAIT":
            print(f"{DIM}[{f.headers.get('waiting')} more request(s) queued]{RESET}")
        elif t == "PERMEND":
            if self.perm_id == f.headers.get("id"):
                self.perm_id = None
            reason = f.headers.get("reason", "")
            if reason != "answered":
                print(f"{DIM}[permission request {reason}]{RESET}")
        elif t == "HALT":
            # The other wedge case: the bridge has stopped the agent over a
            # sync conflict and is waiting for the user, not a permission
            # reply. Continue on the real guest IS the confirmation; here
            # that is /continue.
            self.halt_id = f.headers.get("id")
            print(f"\n{YELLOW}{BOLD}HALT{RESET} {BOLD}{text}{RESET}")
            print(f"{DIM}  /continue{RESET}")
        elif t == "HALTEND":
            hid = f.headers.get("id")
            if hid and self.halt_id == hid:
                self.halt_id = None
                print(f"{DIM}[halt cleared]{RESET}")
        else:
            print(f"{DIM}[{t} {f.headers}] {text[:200]}{RESET}")

    def _export(self, f: Frame) -> None:
        """EXPORTSTART / EXPORTDAT / EXPORTEND. 02.16.

        The same three-method shape win/app/export.cpp has, because that is
        what conformance means: open a temp file on START, append on DAT,
        rename or discard on END. `render` had no branch for these at all, so
        all three fell into the catch-all and printed the deflated body --
        which meant the one transfer path with a real defect in it (02.3, the
        idle reaper firing during a long export) could not be driven from the
        conformance tool, and `test_fakegui_conformance.py` had no export
        test to hang off.

        `off` is checked the way the guest checks it: a chunk that does not
        continue where the last one stopped would write a file with a hole in
        it, and a hole is indistinguishable from a complete file afterwards.
        """
        if f.type == "EXPORTSTART":
            if self._exp is not None:
                print(f"{RED}export: EXPORTSTART while one is in flight"
                      f"{RESET}")
                self._exp["fh"].close()
            fh = tempfile.NamedTemporaryFile(
                prefix="fakegui-export-", suffix=".html", delete=False)
            self._exp = {"fh": fh, "path": fh.name, "got": 0,
                         "total": int(f.headers.get("total", "0") or 0)}
            print(f"{DIM}[export: {self._exp['total']} bytes incoming]{RESET}")
            return

        if self._exp is None:
            # The real guest logs and drops: a chunk with no destination open
            # has nowhere to go, and inventing one would write a file nobody
            # asked for.
            print(f"{DIM}[export: {f.type} with no destination open]{RESET}")
            return

        exp = self._exp
        if f.type == "EXPORTDAT":
            off = int(f.headers.get("off", "0") or 0)
            if off != exp["got"]:
                print(f"{RED}export: chunk at {off} does not continue "
                      f"{exp['got']}; abandoning{RESET}")
                exp["fh"].close()
                os.unlink(exp["path"])
                self._exp = None
                return
            exp["fh"].write(f.body)
            exp["got"] += len(f.body)
            return

        if f.type == "EXPORTEND":
            exp["fh"].close()
            ok = f.headers.get("status") == "ok"
            short = exp["total"] and exp["got"] != exp["total"]
            if ok and not short:
                self.export_path = exp["path"]
                print(f"{GREEN}exported{RESET} {DIM}{exp['got']} bytes -> "
                      f"{exp['path']}{RESET}")
            else:
                why = f.headers.get("reason", "")
                if short:
                    why = (why + " " if why else "") + \
                          f"(short by {exp['total'] - exp['got']} bytes)"
                # Thrown away, like the guest does. A shortened export is not
                # a smaller export, it is a broken file.
                os.unlink(exp["path"])
                print(f"{RED}export failed{RESET} {DIM}{why}{RESET}")
            self._exp = None

    def _clear_live(self) -> None:
        """Undo _show_live's cursor move before an APPEND or a fresh LIVE
        replaces it -- the terminal equivalent of the guest replacing one DOM
        node in place rather than growing a new one per update."""
        if self._live_lines:
            sys.stdout.write(f"\033[{self._live_lines}A\033[J")
            self._live_lines = 0

    def _show_live(self, text: str) -> None:
        self._clear_live()
        if not text:
            return
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        self._live_lines = text.count("\n") + (0 if text.endswith("\n") else 1)

    # --- EXEC*/SYNC*: the RPC worker's job, not render_frame's -------------

    async def _handle_rpc(self, f: Frame) -> None:
        if f.type == "EXECKILL":
            kid = f.headers.get("id")
            print(f"{DIM}[EXECKILL {kid}]{RESET}")
            if self._exec_task is not None and self._exec_id == kid:
                self._exec_task.cancel()
            return

        if f.type == "EXEC":
            eid = f.headers.get("id", "")
            if self._exec_task is not None:
                # win/rpc/rpc.c: one job at a time; a second EXEC while one
                # runs is answered busy, not queued.
                await self.send(Frame("EXECRESULT",
                                      {"id": eid, "status": "busy",
                                       "code": "0", "truncated": "0"}))
                return
            cwd = f.headers.get("cwd") or None
            timeout_ms = int(f.headers.get("timeout_ms", "20000") or "20000")
            cmd = f.body.decode("cp1252", errors="replace")
            # THE SHELL HEADER, WHICH THIS USED TO IGNORE ENTIRELY.
            #
            # It always ran through a shell, so the one branch the real guest
            # has -- spawn the program directly -- was never exercised from
            # the conformance tool at all. The header is the invocation
            # prefix now rather than a flag, which is what makes honouring it
            # meaningful here: a Win98 prefix is nonsense on Linux, but the
            # SHAPE is the thing under test, and it is the same shape.
            shell = f.headers.get("shell") or ""
            print(f"\n{CYAN}[exec {eid}]{RESET} {DIM}{cmd[:160]}{RESET}")
            self._exec_id = eid
            self._exec_task = asyncio.create_task(
                self._exec_run(eid, cmd, cwd, timeout_ms, shell))
            return

        # SYNC*: fakegui has no mirrored directory, so it answers exactly as
        # a real guest with no configured sync root does. SYNCSCAN's `no_root`
        # is enough on its own -- bridge/files/syncrun.py's scan_guest turns
        # any non-"ok" status into a SyncError and aborts the whole pass
        # before ever sending a SYNCPUT/SYNCGET/SYNCMKDIR -- so the rest of
        # these replies exist only so a frame sent out of that order still
        # gets an honest answer instead of the bridge waiting out a 600s
        # timeout.
        sid = f.headers.get("id", "")
        print(f"{DIM}[{f.type} id={sid}]{RESET}")
        if f.type == "SYNCSCAN":
            await self.send(Frame("SYNCEND", {
                "id": sid, "status": "no_root", "files": "0",
                "tree_id": "", "root": ""}))
        elif f.type in ("SYNCPUT", "SYNCPUTEND"):
            await self.send(Frame("SYNCPUTACK", {"id": sid, "status": "readonly"}))
        elif f.type == "SYNCMKDIR":
            await self.send(Frame("SYNCMKDIRACK", {"id": sid, "status": "bad_path"}))
        elif f.type == "SYNCGET":
            await self.send(Frame("SYNCDATEND",
                                  {"id": sid, "status": "missing",
                                   "crc": "0", "cls": "t"}))

    async def _exec_run(self, eid: str, cmd: str, cwd: str | None,
                        timeout_ms: int, shell: str = "") -> None:
        """Actually run it. This is the one RPC frame family worth truly
        implementing here: EXECOUT/EXECRESULT exercise the bridge's exec
        plumbing end to end, and a subprocess is not a second implementation
        of anything -- unlike SYNC*, there is no manifest format to agree
        with, just bytes out and a status in.

        `shell` is the invocation prefix from the EXEC frame, spliced in
        front of the command exactly as `rc_exec_run` splices it, or empty to
        spawn the program directly. A prefix naming a Win98 shell will not
        run here and is not meant to: what conformance means for this branch
        is that an empty header spawns directly and a non-empty one does not,
        which is the distinction the guest makes."""
        try:
            if shell:
                proc = await asyncio.create_subprocess_shell(
                    f"{shell} {cmd}", cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
            else:
                # Directly, the way CreateProcessA is given the command line
                # with no shell in front of it.
                proc = await asyncio.create_subprocess_exec(
                    *shlex.split(cmd), cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
        except Exception as exc:
            print(f"{RED}[exec {eid}] spawn failed: {exc}{RESET}")
            await self.send(Frame("EXECRESULT",
                                  {"id": eid, "status": "spawn_failed",
                                   "code": "0", "truncated": "0"}))
            self._exec_task = None
            self._exec_id = None
            return

        sent = 0
        truncated = False
        status = "ok"
        code = 0
        try:
            async def pump_out() -> None:
                nonlocal sent, truncated
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        return
                    if sent >= EXEC_MAX_OUT:
                        truncated = True
                        continue
                    room = EXEC_MAX_OUT - sent
                    if len(chunk) > room:
                        chunk = chunk[:room]
                        truncated = True
                    sent += len(chunk)
                    await self.send(Frame("EXECOUT", {"id": eid}, chunk))

            await asyncio.wait_for(pump_out(), timeout=timeout_ms / 1000.0)
            code = await proc.wait()
        except asyncio.TimeoutError:
            status = "timeout"
            proc.kill()
            await proc.wait()
        except asyncio.CancelledError:
            status = "cancelled"
            proc.kill()
            await proc.wait()
        finally:
            self._exec_task = None
            self._exec_id = None

        await self.send(Frame("EXECRESULT", {
            "id": eid, "status": status, "code": str(max(code, 0)),
            "truncated": "1" if truncated else "0"}))
        print(f"{DIM}[exec {eid}] {status} code={code}"
              f"{' truncated' if truncated else ''}{RESET}")

    # --- halt: the other wedge case, answered the way /continue does -------

    async def answer_halt(self) -> bool:
        """Send RESUME for the halt currently on screen, exactly what
        pressing Continue on the real guest's halt bar does (main.cpp's
        BAR_HALT click handler). Pulled out of input_loop so 03.31's "fakegui
        cannot answer a HALT" gap can be closed by a test that drives this
        directly, rather than only by a human typing into a live prompt --
        input_loop is still the one real caller, calling this exact method.

        Returns False when there is nothing to answer, the same guard
        input_loop applied inline before there was anything here to call.
        """
        if not self.halt_id:
            return False
        await self.send(Frame("RESUME", {"id": self.halt_id}))
        return True

    # --- input -------------------------------------------------------------

    async def input_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, self._prompt)
            if line is None:
                return
            line = line.strip()
            if not line:
                continue

            if line in ("/quit", "/q"):
                return
            if line in ("/stop", "/s"):
                await self.send(Frame("INTERRUPT", {}))
                continue
            if line == "/ping":
                await self.send(Frame("PING", {}))
                continue
            if line.startswith("/model "):
                await self.send(Frame("SETMODEL", {"model": line.split(None, 1)[1]}))
                continue
            if line == "/raw":
                self.raw = not self.raw
                print(f"{DIM}raw={self.raw}{RESET}")
                continue
            if line == "/resume":
                print(f"{DIM}reconnecting as sess={self.sess}{RESET}")
                await self.close()
                await self.connect(resume=True)
                asyncio.create_task(self.pump())
                continue
            if line in ("/continue", "/c"):
                if not await self.answer_halt():
                    print(f"{DIM}no halt is pending{RESET}")
                continue
            if line in ("/allow", "/always", "/deny"):
                if not self.perm_id:
                    print(f"{DIM}no permission request is pending{RESET}")
                    continue
                await self.send(Frame("PERMREPLY", {
                    "id": self.perm_id, "action": line[1:]}))
                continue
            if line.startswith("/"):
                print(f"{DIM}commands: /quit /stop /ping /model <n> /resume "
                      f"/raw /continue /allow /always /deny{RESET}")
                continue

            await self.send(Frame("INPUT", {}, line.encode("cp1252",
                                                           errors="replace")))

    def _prompt(self) -> str | None:
        try:
            return input(f"{BOLD}> {RESET}")
        except (EOFError, KeyboardInterrupt):
            return None


async def run(args) -> None:
    gui = FakeGui(args.host, args.port, args.secret, raw=args.raw)
    try:
        await gui.connect()
    except OSError as exc:
        print(f"{RED}cannot reach bridge at {args.host}:{args.port}: {exc}{RESET}")
        return
    pump = asyncio.create_task(gui.pump())
    try:
        await gui.input_loop()
    finally:
        pump.cancel()
        await gui.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Linux stand-in for retroclaude.exe")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7890)
    ap.add_argument("--secret",
                    default=os.environ.get("RETROCLAUDE_SECRET", "dev-secret"))
    ap.add_argument("--raw", action="store_true",
                    help="dump frames instead of rendering them")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
