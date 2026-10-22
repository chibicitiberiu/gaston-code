"""Drive the `input` step list on a real guest, list by list, no model.

WHY. The step runner's Wine cases prove its logic; what they cannot prove is
the platform -- the real keyboard driver turning keystrokes into characters
on the layout the target window has, USER's foreground rules, a real
Notepad's "save changes?" dialog, the context menu's class. This sends the
lists the `input` tool would, compiled by the same `bridge/agent/steps.py`,
and prints each list's UIEND and per-step lines. Most lists verify
themselves: they end in a `wait_text` that only passes if the keystrokes
became the right characters.

    python3 -u research/probes/inputprobe.py --host 127.0.0.1 --port 7893

Start it INSTEAD OF the bridge, then launch the client. It needs a Notepad
open on the guest (`--launch` asks gremote to start one), and it ends that
Notepad as part of the `close` case -- not on a machine somebody is using.

`--remote host:secret` lets it launch programs mid-list through gremote, which
is how the focus-stealing case is made to happen on cue.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tools"))
import probelib
import toolkit

toolkit.on_path()

from bridge.agent import steps as S  # noqa: E402
from bridge.wire import fold  # noqa: E402
from bridge.wire import protocol as PR  # noqa: E402
from bridge.wire.protocol import Frame, FrameReader  # noqa: E402

TYPED = "H\u00e9llo w\u00f6rld \u20ac{}|\\~^`'\"@ 1&"
#: The same, as a string step: `[` and `{` open a key name, so they double.
TYPED_STEP = TYPED.replace("[", "[[").replace("{", "{{")
EDIT = "Notepad Edit"
CLEAR = "[Ctrl+Home][Ctrl+Shift+End][Delete]"

#: (name, what should happen, steps, expected status, extra action)
CASES = [
    ("type-layout",
     "every character typed exactly, accents, euro and AltGr ones included",
     [{"focus": "Notepad"}, CLEAR, TYPED_STEP,
      {"wait_text": EDIT, "text": TYPED, "timeout": 3000}], "ok", None),

    ("drag-select-by-text",
     "press on 'H\u00e9llo', drag to after 'w\u00f6rld', type X over it",
     [{"down": "LButton", "where": EDIT, "text": "H\u00e9llo"},
      {"move": EDIT, "text": "w\u00f6rld", "edge": "end"},
      {"up": "LButton"}, "X",
      {"wait_text": EDIT, "text": "X \u20ac", "timeout": 3000}], "ok", None),

    ("click-on-text",
     "click puts the caret before '\u20ac'; typing goes there",
     [{"click": EDIT, "text": "\u20ac"}, "<",
      {"wait_text": EDIT, "text": "X <\u20ac", "timeout": 3000}], "ok", None),

    ("held-shift",
     "Shift held across a run of letters types capitals",
     [CLEAR, {"down": "Shift"}, "abc", {"up": "Shift"}, "d",
      {"wait_text": EDIT, "text": "ABCd", "timeout": 3000}], "ok", None),

    # A list that FAILS while Shift is down -- the wait times out before the
    # `up` -- must leave nothing held. The next list types "x", and the probe
    # reads the Edit back exactly: wait_text ignores case, so it cannot tell.
    ("released-after-failure",
     "Shift held, then the list fails; nothing stays held",
     [{"focus": "Notepad"}, CLEAR, {"down": "Shift"},
      {"wait_for": "NoSuchClassAnywhere", "timeout": 500},
      {"up": "Shift"}], "timeout", "then-lowercase"),

    ("context-menu",
     "right-click opens the menu, Escape closes it",
     [{"click": EDIT, "button": "right"},
      {"wait_for": "[class=\"#32768\"]", "timeout": 3000}, "[Esc]",
      {"wait_gone": "[class=\"#32768\"]", "timeout": 3000}], "ok", None),

    # A selector that is ambiguous on ANY desktop: every window with a
    # caption. `Button` alone matched only the taskbar's Start button on a
    # desktop with no dialog open -- and the case clicked it.
    ("ambiguous",
     "a selector matching several controls names them",
     [{"click": "*[text~=\"?*\"]", "timeout": 3000}], "ambiguous", None),

    ("wait-timeout",
     "a wait for nothing ends at its own timeout",
     [{"wait_for": "NoSuchClassAnywhere", "timeout": 1500}], "timeout",
     None),

    ("item-no-msaa-or-found",
     "an item target: no_msaa on a box without oleacc, ok where it exists",
     [{"move": "Progman SysListView32", "item": "My Computer"}],
     ("no_msaa", "ok"), None),

    ("cancel",
     "a long wait stopped by UICANCEL returns promptly",
     [{"wait": 60000}], "cancelled", "cancel"),

    ("focus-lost",
     "a window stealing the front mid-list stops the typing",
     [{"focus": "Notepad"}, {"wait": 6000}, "this must not be typed"],
     "focus_lost", "steal"),

    ("close-prompts",
     "closing a modified Notepad asks first, and says so",
     [{"focus": "Notepad"}, {"close": "Notepad", "timeout": 4000}],
     "prompted", None),

    ("answer-the-prompt",
     "the prompt is where typing goes now; No closes Notepad",
     [{"click": "[class=\"#32770\"] Button[text~=\"no\"]"},
      {"wait_gone": "Notepad", "timeout": 5000}], "ok", None),
]


class Guest:
    def __init__(self, conn):
        self.conn = conn
        self.reader = FrameReader()
        self.pending: list[Frame] = []
        self.seq = 0

    def recv_until(self, ui_id: str, timeout: float) -> Frame:
        self.conn.settimeout(timeout)
        while True:
            for i, f in enumerate(self.pending):
                if f.type == "UIEND" and f.headers.get("id") == ui_id:
                    return self.pending.pop(i)
            for f in self.reader.feed(self.conn.recv(65536)):
                if f.type == "PING":
                    self.conn.sendall(Frame("PONG", {}).encode())
                elif f.type == "UIEND":
                    self.pending.append(f)


def run_case(g: Guest, name, steps, timeout_s, extra, remote) -> Frame:
    c = S.compile_steps(steps, encode=lambda t: fold.encode_exact(t,
                                                                  "cp1252"))
    g.seq += 1
    ui_id = f"p{g.seq}"
    g.conn.sendall(PR.ui_run(ui_id, c.body, timeout_ms=c.timeout_ms,
                             step_ms=S.STEP_TIMEOUT_DEFAULT).encode())
    if extra == "cancel":
        time.sleep(1.5)
        g.conn.sendall(PR.ui_cancel(ui_id).encode())
    elif extra == "steal" and remote:
        time.sleep(2.0)
        remote.launch("C:\\WINDOWS\\CALC.EXE")
    t0 = time.monotonic()
    end = g.recv_until(ui_id, timeout_s)
    end.headers["_took"] = f"{time.monotonic() - t0:.1f}s"
    if extra == "then-lowercase":
        follow = S.compile_steps([{"focus": "Notepad"}, "x"])
        g.seq += 1
        fid = f"p{g.seq}"
        g.conn.sendall(PR.ui_run(fid, follow.body,
                                 timeout_ms=follow.timeout_ms,
                                 step_ms=S.STEP_TIMEOUT_DEFAULT).encode())
        g.recv_until(fid, timeout_s)
        text = edit_text(g)
        end.headers["_edit"] = repr(text)
        if text != "x":
            end.headers["status"] = f"stuck (edit holds {text!r})"
    return end


def edit_text(g: Guest) -> str:
    """What Notepad's Edit holds, read with UITREE -- exact, case and all."""
    from bridge.wire import uirec
    g.seq += 1
    uid = f"p{g.seq}"
    g.conn.sendall(PR.ui_tree(uid, scope="foreground", max_nodes=20).encode())
    recs = bytearray()
    g.conn.settimeout(20)
    while True:
        for f in g.reader.feed(g.conn.recv(65536)):
            if f.type == "UIENT" and f.headers.get("id") == uid:
                recs += f.body
            elif f.type == "UIEND" and f.headers.get("id") == uid:
                for n in uirec.parse_records(bytes(recs), "cp1252"):
                    if n.cls == "Edit":
                        return n.text
                return ""
            elif f.type == "UIEND":
                g.pending.append(f)


def main() -> int:
    ap = probelib.arg_parser()
    ap.add_argument("--remote", default="",
                    help="host:secret of gremote on the guest, for --launch "
                         "and the focus-stealing case")
    ap.add_argument("--launch", action="store_true",
                    help="start Notepad on the guest first, through gremote")
    ap.add_argument("--only", default="", help="comma-separated case names")
    ap.add_argument("--json", default="", help="also write results here")
    a = ap.parse_args()

    remote = None
    if a.remote:
        from gremote.client import Remote
        host, secret = a.remote.rsplit(":", 1)
        remote = Remote(host, secret=secret)
        remote.connect()
        if a.launch:
            remote.launch("C:\\WINDOWS\\NOTEPAD.EXE")

    conn, _ = probelib.listen_for_guest(a.host, a.port)
    g = Guest(conn)
    conn.settimeout(30)
    hello = None
    while hello is None:
        for f in g.reader.feed(conn.recv(65536)):
            if f.type == "HELLO":
                hello = f
    if hello.headers.get("secret") != a.secret:
        print("bad secret")
        return 2
    caps = {k: hello.headers.get(k) for k in ("automation", "keys", "acp")}
    print(f"guest: {caps} mem_kb={hello.headers.get('mem_kb')}", flush=True)
    if caps["automation"] != "2":
        print("this client does not run step lists (automation != 2)")
        return 2
    conn.sendall(PR.ready("inputprobe", "none").encode())
    time.sleep(3)

    only = set(filter(None, a.only.split(",")))
    results = []
    failures = 0
    for name, what, steps, want, extra in CASES:
        if only and name not in only:
            continue
        end = run_case(g, name, steps, 90, extra, remote)
        status = end.headers.get("status")
        wanted = want if isinstance(want, tuple) else (want,)
        ok = status in wanted
        failures += not ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] {name}: {what}", flush=True)
        print(f"  status={status} want={'/'.join(wanted)} "
              f"step={end.headers.get('step', '-')} "
              f"took={end.headers['_took']}")
        if end.headers.get("detail"):
            print(f"  detail: {end.headers['detail']}")
        for k in ("cand1", "cand2", "cand3"):
            if end.headers.get(k):
                print(f"  {k}: {end.headers[k]}")
        for row in S.parse_reply(end.body)[0]:
            print("   ", {k: v.decode("cp1252", "replace")
                         for k, v in row.items()})
        results.append({"case": name, "ok": ok, "status": status,
                        "headers": end.headers,
                        "body": end.body.decode("cp1252", "replace")})
        time.sleep(1)

    print(f"\n{len(results) - failures}/{len(results)} cases as expected")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1)
    if remote:
        remote.close()
    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
