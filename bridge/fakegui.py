"""A Linux stand-in for retroclaude.exe that speaks the real wire protocol.

This is the highest-leverage file in the project. It means the entire bridge can
be built and tested before any Win98 code exists, and afterwards it stays as:

  - the protocol conformance harness (if fakegui works and the guest does not,
    the bug is in the C, not the bridge),
  - the break-glass tool when a permission prompt has the session wedged.

It is a ~200 line script and should stay one. Resist making it a framework.

    python -m bridge.fakegui --port 7890
    /quit  /stop  /model <name>  /resume  /raw
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .protocol import Frame, FrameReader, ProtocolError, hello

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"


class FakeGui:
    def __init__(self, host: str, port: int, secret: str, *, raw: bool = False):
        self.host = host
        self.port = port
        self.secret = secret
        self.raw = raw
        # The id of the permission request currently on screen, if any.
        self.perm_id: str | None = None
        self.sess: str | None = None
        self.next_seq: int | None = None   # what to resume from
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._turn_active = False

    # --- connection --------------------------------------------------------

    async def connect(self, resume: bool = False) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port
        )
        f = hello(
            self.secret,
            sess=self.sess if resume else None,
            from_seq=self.next_seq if resume else None,
        )
        self.writer.write(f.encode())
        await self.writer.drain()

    async def close(self) -> None:
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
        self.writer.write(f.encode())
        await self.writer.drain()

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
                    self.render(f)
        except ProtocolError as exc:
            print(f"\n{RED}protocol error: {exc}{RESET}")
        except (ConnectionResetError, asyncio.IncompleteReadError):
            print(f"\n{DIM}-- connection reset --{RESET}")

    def render(self, f: Frame) -> None:
        if f.seq is not None:
            self.next_seq = f.seq + 1

        if self.raw:
            body = f.body[:80]
            print(f"{DIM}[{f.type} {f.headers} {body!r}]{RESET}")
            return

        t = f.type
        text = f.body.decode("cp1252", errors="replace")

        if t == "READY":
            self.sess = f.headers.get("sess")
            self.next_seq = int(f.headers.get("next_seq", "1"))
            print(f"{GREEN}connected{RESET} {DIM}sess={self.sess} "
                  f"model={f.headers.get('model')}{RESET}")
        elif t == "TEXT":
            style = f.headers.get("style", "plain")
            if style == "thinking":
                sys.stdout.write(f"{DIM}{text}{RESET}")
            else:
                sys.stdout.write(text)
            sys.stdout.flush()
        elif t == "TOOL":
            print(f"\n{CYAN}  -> {f.headers.get('name')}{RESET} {DIM}{text}{RESET}")
        elif t == "TOOLRESULT":
            colour = RED if f.headers.get("error") == "1" else DIM
            first = text.strip().splitlines()[:1]
            print(f"{colour}     {first[0] if first else ''}{RESET}")
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
        elif t == "NOTICE":
            print(f"{YELLOW}[{f.headers.get('kind')}] {text}{RESET}")
        elif t == "STATUS":
            print(f"{DIM}[status {f.headers}]{RESET}")
        elif t == "RESYNC":
            print(f"{YELLOW}[resync needed: {f.headers.get('reason')}]{RESET}")
        elif t == "ERROR":
            print(f"{RED}[{f.headers.get('code')}] {f.headers.get('msg')}{RESET}")
        elif t == "PONG":
            print(f"{DIM}pong{RESET}")
        elif t in ("PERMREQ", "PERMSTATE"):
            # The break-glass case this tool exists for. Without it a wedged
            # prompt can only be cleared by restarting the bridge.
            self.perm_id = f.headers.get("id")
            again = " (re-issued)" if t == "PERMSTATE" else ""
            waiting = f.headers.get("waiting", "0")
            queued = f" {DIM}(+{waiting} queued){RESET}" if waiting != "0" else ""
            print(f"\n{YELLOW}{BOLD}PERMISSION{RESET}{again} "
                  f"{BOLD}{text}{RESET}{queued}")
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
        else:
            print(f"{DIM}[{t} {f.headers}] {text[:200]}{RESET}")

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
                print(f"{DIM}reconnecting with from={self.next_seq}{RESET}")
                await self.close()
                await self.connect(resume=True)
                asyncio.create_task(self.pump())
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
                      f"/raw /allow /always /deny{RESET}")
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
