# 01 — Guest audit: win/

**Date** 2026-09-21. **Scope**: every `.c`, `.cpp` and `.h` under `win/` except
`win/third_party/` — the ~13k shipping lines plus the check binaries and
probes (then `win/checks/`, now `tests/guest/` and `research/guest/`) and
`win/Makefile`. **Not** the bridge, except where a guest behaviour can only be
judged against what the bridge actually sends, in which case the bridge line is
cited too.

**Method.** The code was read, not the comments. This tree's comments are its
design record, so where a comment and the code disagree the code is what is
reported and the disagreement is reported with it — five findings below are
exactly that shape, including three where a previous audit finding was marked
`Fixed:` and the fix does not do what it claims. The previous round is
`docs/audit/01-guest-c.md`; nothing marked `Fixed:` there is re-raised unless
the fix is inert. Items `docs/BACKLOG.md` already owns as deliberate deferrals
are not repeated, with one exception, noted where it appears, whose deferral
has become load-bearing.

Every finding names a `file:line` and a concrete failure: the input or sequence,
and the wrong result. Things that looked wrong and were not are listed at the
end under **Checked and clean**, because an audit that lists only defects makes
the rest of the tree look unexamined. Ordering is by real consequence on a 32MB
Win98 SE box on a slow link, not by how bad the code looks.

**The short version.** The architecture is in good shape and the last round's
structural work held: the frame dispatch table is complete against the bridge's
own frame vocabulary, the four-lane router is right, the COM ownership in
`htmlview.cpp` is correct throughout, there are no NT-only static imports, and
there are no raw `strcpy`/`sprintf` on wire data anywhere. What is wrong is
concentrated in two places: **guards that were written and then wired up
backwards or into the wrong branch**, and **state that outlives the connection
it belongs to**. Findings 01.1, 01.3, 01.5 and 01.24 are the first kind;
01.8, 01.14, 01.18, 01.19 and 01.20 are the second.

---

# Findings

## 01.1 — The permission/halt bar steals focus on every push, so the next space bar answers it

**Where:** `win/app/bar.cpp:221` (`SetFocus(allow)` in `show()`), reached from
`push()` at `:262` and `:280`, driven by `frame()` at `:317`/`:333`.

`show()` calls `SetFocus(allow)` unconditionally on every push. `push()` runs
straight off `WM_RC_FRAME` on the window thread, at whatever moment the bridge
chooses. Focus normally lives on the input box (`main.cpp:1458-1460`,
`input.cpp:116`). A `BS_PUSHBUTTON` with focus is clicked by `VK_SPACE`.

**The failure.** The user is typing. The guest's sync pass finds a conflicting
edit and the bridge raises a HALT — `bar.cpp:13-17` says outright that a halt
"owes nothing to the agent's turn", so this is the normal case, not a race.
Focus jumps from the edit control to **Continue**, and the next space character
in the sentence being typed sends `RESUME`: the refusal is lifted and the agent
merges over the file the user was editing. The same sequence with `PERMREQ`
approves a tool call nobody read.

`CLAUDE.md`'s standing rule is *"Pressing Continue **is** the confirmation — it
lifts the refusal and the agent merges."* The whole conflict-safety design
rests on that keypress being deliberate. Here it is a keystroke in a sentence.

The module already knows about focus theft: `relabel()` exists
(`bar.cpp:226-229`) specifically so a PERMWAIT queue-depth tick does not steal
focus. Only that case was fixed. The taking of focus is deliberate and argued
at `bar.cpp:86-89` (*"the bar is the one control the program STOPS for"*) — the
argument is sound for a bar raised while the user is idle and wrong for one
raised mid-keystroke, and nothing distinguishes the two.

**Fix.** Take focus only when the input box does not already have it, or never
take it — the buttons already carry A/L/D mnemonics and `WS_TABSTOP`, which is
what `bar.cpp:86-92` added them for. Either way, a bar that appears while
someone is typing must not be answerable by the character they were about to
type.

---

## 01.2 — `rc_sync_contained` is handed the file's CONTENT as part of its path, so no source file with `//` in its first 8KB can be pushed

**Where:** `win/sync/syncrpc.cpp:262-285`, against
`bridge/files/synctransport.py:141-149`.

A `SYNCPUT` body is `"<len> <rel>" + data-chunk` — the bridge builds it as
`prefix + piece` (`synctransport.py:149`). `do_put` parses the length prefix and
then:

```c
rel = body + plen + 1;                                                /* :264 */
...
if (!rc_sync_contained(rel)) { put_fail(net, id, "bad_path"); return; } /* :285 */
rc_strf(g_put.rel, sizeof(g_put.rel), "%.*s", (int)rn, rel);            /* :287 */
```

The copy on line 287 is correctly bounded to `rn` bytes. The containment check
on line 285 is **not** — it gets the bare `rel` pointer, which is the path
followed immediately by up to 8192 bytes of the file's own contents, running to
the frame's NUL. `rc_sync_contained` (`win/sync/sync.cpp:292-300`) splits on
`/` and `\` and refuses an empty, `.` or `..` component.

**The failure.** Push any text file whose first 8KB contains `//`. The two
adjacent slashes are an empty component; the guest answers
`SYNCPUTACK status=bad_path`. `//` is the line-comment token of C, C++, C#,
Java, JavaScript, Go, Rust, PHP and Swift, and it is in every `http://` URL.
`\\` in a Windows path literal does it too, as does `/./`, or a chunk that
happens to end on a separator. **The agent cannot write a source file with a
line comment onto a machine whose entire purpose is writing code on it**, and
the refusal names the path, which is the one thing not wrong with it.

Invisible to the suite because
`tests/python/guest/test_guest_agreement.py:252-291` drives
`rc_sync_contained` through `synccheck --contained` with bare paths. Nothing
tests it with a payload glued to the tail, which is the only way `do_put` ever
calls it.

**Fix.** Copy first, check second: build `g_put.rel` from the `rn`-bounded copy
(checking the return, 01.6), then `rc_sync_contained(g_put.rel)`. Two lines
swapped. Add a `--contained` case with binary junk appended, so it cannot come
back silently.

---

## 01.3 — `rc_exec_run`'s "refuse rather than truncate" guard is inverted: an over-long command is truncated and then executed

**Where:** `win/rpc/exec.cpp:116-126`.

```c
if (use_shell) n = rc_strf(cmd, sizeof(cmd), "%s /c %s", shell_name(), command);
else           n = rc_strf(cmd, sizeof(cmd), "%s", command);
if (n < 0 || n >= (int)sizeof(cmd)) { ... out->status = RC_EXEC_TOO_LONG; return -1; }
```

`rc_strf` returns **1 if the whole string fit, 0 if it was truncated**
(`win/core/str.c:25`; contract at `win/core/str.h:33`). It never returns a
length and never a negative. So `n ∈ {0,1}`: `n < 0` is unreachable,
`n >= 1024` is unreachable, and **the guard never fires**. Execution falls
through to `CreateProcessA` at `:180`.

**The failure.** This file's own header states the defect it believes it fixed
(`exec.cpp:12-14`): *"`char cmd[512]` filled with `_snprintf`, which TRUNCATES
SILENTLY — and a truncated command is still executed, so `del c:\temp\*.tmp`
cut short becomes `del c:\temp\*`. Rejected here instead."* It is not rejected
here. A command over ~1008 bytes (1024 minus `"command.com /c "`) is cut at
byte 1023 and handed to the shell. `bridge/agent/tools.py` applies no length
cap of its own, so this is the only guard in the system and it is dead.

`RC_EXEC_TOO_LONG` is therefore unreachable, which makes
`docs/BACKLOG.md:103-105` (*"`RC_EXEC_TOO_LONG` is reported as `spawn_failed` —
**Closed 2026-09-18.** It has its own status"*) true on paper only. The status
exists; nothing can produce it.
`tests/python/server/test_server_integration.py:1841` asserts the **bridge**
handles `status=too_long` with the reply hand-written by a fake guest, so the
guest half of that pair has never been exercised.

**Fix.** `if (!rc_strf(...)) { out->status = RC_EXEC_TOO_LONG; return -1; }`,
plus an `rpccheck`/`toolcheck` case driving `rc_exec_run` with a 1200-byte
command.

---

## 01.4 — A work directory over 127 bytes is silently truncated, then created on disk and mirrored into

**Where:** `win/app/optmenu.cpp:147` into `cur_work[128]`
(`win/app/optmenu.h:95`), and `win/app/workdir.cpp:26`. Both discard
`rc_strf`'s return.

```c
rc_strf(cur_work, sizeof(cur_work), "%s", id);    /* optmenu.cpp:147, 128 bytes */
...
rc_strf(want, sizeof(want), "%s", path ? path : "");  /* workdir.cpp:26 */
```

`id` is a wire header value, so up to `RC_MAX_VAL` = 255 bytes reach it. The
guest declares `maxpath = 260` at handshake (`net/net.cpp:509`,
`RC_CAP_MAXPATH`), so the bridge is entitled to send a 260-byte path, and
`bridge/server.py:1895` echoes back verbatim whatever the user picked through
`SHBrowseForFolder`. `main.cpp:763-765` then hands `work_path()` to
`rc_workdir_apply`, which **`rc_mkpath`s it, parents and all**
(`workdir.cpp:58`) and re-points both `rc_syncrpc_set_root` and
`rc_rpc_set_sync_root`.

**The failure.** A user picks
`C:\Projects\ClientWork\2026\RetroAI\guest\staging\conversations\c17` — an
ordinary answer to "where should this conversation work". The guest cuts it at
127 bytes, **creates that prefix as a directory**, and mirrors the
conversation's files into a folder the user never named. The folder they did
name reads as empty, which the bridge sees as a mass deletion. Two sides, two
trees, no error anywhere.

This is verbatim the failure `core/str.h:10-19` was written about, and
`frameproto.cpp:142-156` drops an entire frame rather than clip a `cwd` header
for exactly this reason. The guarantee the parser pays for is thrown away two
layers up.

**Fix.** Widen `cur_work`/`cur_work_short` to `MAX_PATH` and check both returns;
on truncation refuse the path, log it and surface it through `g_html->error()`
rather than calling `rc_workdir_apply`.

---

## 01.5 — HELLO can carry uninitialised stack bytes as the machine's PATH

**Where:** `win/app/tooldet.cpp:146` (and identically at `:85` for every
`env1..env8` a section names).

```c
char path[MAX_PATH];                                        /* :123, uninitialised */
...
if (GetEnvironmentVariableA("PATH", path, sizeof(path)) && path[0]) {
    char line[MAX_PATH + 8];
    if (rc_strf(line, sizeof(line), "PATH=%s", path)) emit(&s, "machine", "env", line);
}
```

`GetEnvironmentVariableA` returns the **required buffer size** when the buffer
is too small — a non-zero value — and leaves the buffer's contents **undefined**
and unterminated. The code reads any non-zero return as success.

**The failure.** A Win98 box with several toolchains wired up in `autoexec.bat`
has a `PATH` well over 259 bytes; that is the common case on exactly the machine
this feature exists for. `path` is then uninitialised stack, `path[0]` is
whatever was there, and `rc_strf("%s", path)` reads forward until it finds a
zero byte — a stack over-read — and formats the result into the fact block.
`rc_tooldet_cached` hands that block straight to `rc_net_start` as the HELLO
**body** (`main.cpp:1406-1408`), which the bridge folds into the agent's system
prompt. So: this machine's stack contents go on the wire and into a model
prompt, and the agent is told a PATH that does not exist.

**Fix.** `DWORD n = GetEnvironmentVariableA(...); if (n == 0 || n >=
sizeof(path)) continue;` at both sites. A variable too long to read is a fact
you do not have, not one you invent — which is the rule `tooldet.h:1-23`
already states for everything else in the file.

---

## 01.6 — `do_put` ignores `rc_strf`'s return on a path built from a wire-supplied length; two more `rc_sync_join` returns are still discarded

**Where:** `win/sync/syncrpc.cpp:286-287`; also `:352` and
`win/sync/sync.cpp:559`.

```c
rc_strf(g_put.id,  sizeof(g_put.id),  "%s", id);
rc_strf(g_put.rel, sizeof(g_put.rel), "%.*s", (int)rn, rel);
```

`rn` is `atoi` of a length prefix the bridge supplies (`:263`) and is unbounded.
`g_put.rel` is `MAX_PATH`. A relative path of 260 bytes or more is silently cut,
and the cut path is the input to `rc_sync_begin_write` (which opens
`<root>\<rel>.gaston-tmp`) and then, at `do_putend`, to `rc_sync_commit` — which
`DeleteFileA`s, moves the target aside and moves the temp into place. All of
that acts on a path naming a **different file**, and the transfer then acks
`status=ok` with a matching CRC, so the bridge advances its baseline over a file
it never wrote.

`win/core/str.h:17-24` names this exact site: *"Two of those sites are reachable
from the wire: the manifest id and the put path, both filled from a length the
BRIDGE supplies."* One of the two still discards the answer.

Two of the same shape:

- **`syncrpc.cpp:352`** — `rc_sync_join(full, sizeof(full), g_root, g_put.rel)`
  in `do_putend`'s compare-and-swap. A truncated join runs the `if_crc`
  precondition against a *different* file, so the guard that stops a concurrent
  local edit being overwritten passes or fails on the wrong evidence. Audit
  01.61 closed this class and named `do_get` and `do_mkdir`; this third site was
  missed.
- **`sync.cpp:559`** — the same call opening `rc_sync_commit`. Safe only
  transitively, via `rc_sync_begin_write`'s earlier refusal — and only if `rel`
  is the same string, which the truncation above breaks.

**Fix.** `if (!rc_strf(g_put.rel, ...)) { put_fail(net, id, "too_long");
return; }`, and check the two joins the way `do_get` and `do_mkdir` already do.

---

## 01.7 — Every compressed frame costs a fixed 256KB heap block, with a 256-frame in-flight cap behind it

**Where:** `win/net/net.cpp:49-86` (`inflate_body`), `:22`
(`RC_MAX_INFLIGHT 256`), `:228-229` (the backpressure gate).

```c
unsigned cap = RC_MAX_FRAME * 4;              /* 262144 */
out = (char *)HeapAlloc(GetProcessHeap(), 0, cap + 1);
```

Allocated at full size for every `enc=deflate` frame whatever it contains, and
**never shrunk**: `f->body_len` records the real length (`:82`) but
`f->_body_alloc` holds all 262,145 bytes until the UI thread frees the frame.

The 4× headroom cannot be reached. `bridge/wire/link.py:121` caps an
*uncompressed* chunk at `MAX_FRAME - 1024` = 64,512 bytes before compressing, so
an inflated body is never larger than ~63KB. The allocation is four times the
largest value the protocol permits. The bridge compresses anything over 64 bytes
when the link is slow (`bridge/wire/compress.py:41`), so during a streamed
answer essentially every `TEXT` frame takes this path.

**The failure.** The reader stops posting only at 256 frames in flight. Each
posted frame holds its `_raw` payload (≤64KB) *plus* this 256KB block. At the
cap that is ~83MB demanded on a 32MB machine. The cap is not the realistic
number; the realistic one is bad enough. The UI thread stalls whenever MSHTML
reflows, a folder browser is open, or a page is being written to the cache, and
a dozen queued compressed `TEXT` frames is 3MB. When `HeapAlloc` fails,
`inflate_body` returns -1 and `net.cpp:193-195` logs `"net: cannot inflate
frame, dropping link"` and **breaks the reader loop**. Memory pressure becomes a
dropped connection mid-answer rather than a slow one.

`docs/BACKLOG.md:76-80` rightly corrects "28KB free" as `dwAvailPhys` excluding
the shrinkable disk cache. That correction applies to a steady 86KB of BSS. It
does not survive megabytes of transient demand proportional to frames-in-flight.
This is the one item overlapping BACKLOG §2's deferral that is reported anyway,
because §2's own worked example is the 66KB static build buffer — three orders
of magnitude smaller than this and not the thing that will actually fail.

**Fix.** (a) Inflate into one reader-thread-owned scratch of `RC_MAX_FRAME +
slack`, then `HeapAlloc` exactly `body_len` and copy: per-frame cost becomes the
real size, peak concurrent cost becomes one scratch buffer. (b) Lower
`RC_MAX_INFLIGHT`; 256 was never derived from the memory budget.

---

## 01.8 — An export interrupted by a dropped link wedges File > Export for the rest of the process

**Where:** `win/app/export.cpp:106-113` (the `if (fp)` guard), with no
counterpart anywhere — `g_export`'s only uses are `main.cpp:1002, 1336, 1704,
1707, 1834`.

`begin()` refuses while `fp != 0`. `fp` is cleared only by `EXPORTEND`, by
`give_up()` (called solely when `ui_send` fails immediately after `begin`), or
by `~rc_export` at `WM_DESTROY`. **Nothing resets the sink on a disconnect.**

**The failure.** Start an export; the link drops after `EXPORTSTART` and before
`EXPORTEND` — which on a 40kbit link is the expected way a few-hundred-KB
transfer ends. `fp` stays open forever. Every later File > Export prints *"An
export is already in progress."* and does nothing, with no way out short of
restarting. It also holds a `FILE*` on the `.$$$` for the process lifetime and
leaves that file on disk — on a floppy, holding the volume.

What makes this worth its rank is that the identical bug class was found, named
and fixed for the sync PUT three lines away in the same handler:
`main.cpp:1548-1558` calls `rc_syncrpc_abort()` from the disconnect branch
precisely because *"a PUT with no closing SYNCPUTEND left `g_put.active` latched
forever, refusing every later PUT as busy"*. The export sink has the same latch
and was not given the same call.

**Fix.** Call `g_export->give_up()` from `WM_RC_STATE`'s failure branch beside
`rc_syncrpc_abort()`, and/or make `begin()` abandon a stale transfer with a log
line rather than refuse.

---

## 01.9 — `pump()` re-enters the connection machinery from inside the copy walk

**Where:** `win/app/workdlg.cpp:131-138`, with `main.cpp:1508-1511` and
`main.cpp:1391`.

```c
void pump(void) { MSG m; while (PeekMessageA(&m, NULL, 0, 0, PM_REMOVE)) {
    TranslateMessage(&m); DispatchMessageA(&m); } }
```

`EnableWindow(parent, FALSE)` (`:686`) does not suppress `WM_TIMER`, and
`PeekMessage(NULL, ...)` retrieves messages for the **whole thread**, not just
the progress dialog.

**The failure.** The bridge drops during a copy of a real project — minutes, on
this link. `TIMER_RECONNECT` fires inside `progress_say` → `pump` →
`main.cpp:1509` → `try_connect` → **`rc_workdir_apply("")`**
(`main.cpp:1391`, sharing off) → `rc_net_stop` → `rc_net_start`. Sharing is
switched off underneath a running copy; the copy finishes; `propose(out, mode)`
at `:694` sends `SETWORK` on a socket that was just torn down and rebuilt, so
the proposal is lost; `wait_for_sync` watches `rc_syncrpc_moved()` never change
and gives up after 3s; the user is told "N files copied" while the conversation
is still pointed at the old folder and nothing will ever move it.

The same pump also dispatches `WM_RC_FRAME` → `OPTIONSEND` →
`rc_workdir_apply(want)`, re-pointing the mirror while the UI thread is ~20KB
deep in a recursive directory walk. The `State` comment at `:405-413` shows the
loop being pumped was understood; the `try_connect` consequence was not.

**Fix.** `KillTimer(hwnd, TIMER_RECONNECT)` for the duration of the copy, or
restrict `pump()` to `PeekMessage(&m, p->dlg, ...)` plus `WM_PAINT`. Separately,
`pump()` also removes and discards `WM_QUIT` (`:134`), so a shutdown arriving
during a copy is swallowed — one line: re-post it and break.

---

## 01.10 — "Cancel" on a per-file copy error is silently downgraded to "carry on and switch anyway"

**Where:** `win/app/workdlg.cpp:347` (`on_error` returns `RC_WORK_CANCEL`)
against `:687` and `:693`.

`copy_sink` sets `c->cancelled` (`workcopy.cpp:384`) and `rc_workcopy_run`
returns 0 (`workcopy.cpp:423`). Line 687 **discards that return**, and line 693
tests `p.cancelled` — which only the progress window's Cancel button ever sets
(`workdlg.cpp:156`).

**The failure.** Copying 200 files; file 6 is locked by an open editor; the user
clicks Abort. The walk stops. `p.cancelled` is still 0, so `propose()` fires and
the work directory changes anyway. The completion box says *"5 files copied. 1
could not be, and is still in C:\Old"* — the 194 files never attempted are not
mentioned anywhere. That is exactly the *"the folder then looks complete and is
not"* outcome the comment at `workdlg.cpp:333-336` says must not happen.

**Fix.** In `on_error`, `if (answer == IDABORT) { r->p->cancelled = 1; return
RC_WORK_CANCEL; }` — or honour `rc_workcopy_run`'s return at `:687`.

---

## 01.11 — A worker that misses `rc_rpc_join`'s deadline disables `exec` and `sync` for the rest of the process, silently

**Where:** `win/rpc/rpc.cpp:556-567` and `:731-742`, with `:669-673`.

If `rc_rpc_join` times out it returns 0, leaving `g_thread` non-NULL and
`g_running` 0. `rc_net_stop` logs *"net: RPC worker did not exit"*
(`net.cpp:768`) and carries on. On the next `try_connect`:

```c
g_net = net;
if (g_thread) return 0;     /* <-- returns before g_running = 1 */
```

so `g_running` stays 0 for the new connection. Every frame then goes
`rc_rpc_route` → `rc_rpc_submit` → `if (!g_thread || !g_running) return -1` →
the caller frees it and returns `RC_ROUTE_TAKEN`.

**The failure.** After one slow shutdown — the 30-second join expiring because a
child would not die, or a `WriteFile` to a floppy or a network share hanging —
the reconnect looks completely healthy. The transcript streams, the chat list
loads, the light is green. But every `EXEC` and every `SYNC*` frame is dropped
on the floor with no answer, so every tool call waits out its full bridge-side
budget (120s for exec, 600s for a scan) and reports nothing useful. One log
line, from the *previous* shutdown, explains it.

**Fix.** Move `g_running = 1` above the `if (g_thread) return 0;` — the same
one-line reordering 01.48 made for `g_net` on the line above — or have
`rc_rpc_start` return failure when it could not take ownership of a live worker,
so the caller can say so.

---

## 01.12 — `to_bstr` substitutes an empty string on allocation failure, so a rendered block vanishes with no error anywhere

**Where:** `win/app/htmlview.cpp:306-321`, consumed at `:850` (`append`), `:889`
(`live`), `:903` (`status`), `:922-925` (`clear`).

```c
w = (WCHAR *)HeapAlloc(GetProcessHeap(), 0, (unsigned)n * sizeof(WCHAR));
if (!w) return SysAllocString(L"");
```

**The failure.** On `HeapAlloc` failure, `append()` calls `insertAdjacentHTML`
with an empty string, which **succeeds** — so the `if (FAILED(hr))` log at `:855`
does not fire, `d->diag` reports "first append ok", and the bridge's block
silently disappears from the transcript with no trace at any layer. `live()` at
`:889` is worse: it `put_innerHTML("")`s `#l`, **wiping** the partial answer
instead of leaving it.

Peak cost per call is about 5× the fragment — the frame body, plus
`n * sizeof(WCHAR)` for the intermediate, plus the `SysAllocString` copy, the
last two alive together across `:315-319`. Against 01.7's 256KB inflated body
that is ~1.25MB transient. Pages are budgeted to 16KB so the normal case is
~80KB, but the OOM path is not hypothetical on this machine and it is the one
that loses data in silence.

**Fix.** Return `NULL` on allocation failure and let each caller log and refuse.
`insertAdjacentHTML(NULL)` already fails loudly, so the NULL alone closes most
of it.

---

## 01.13 — The permission bar's STATIC eats `&`, so the command shown is not the command approved

**Where:** `win/app/bar.cpp:83-85` — `SS_LEFTNOWORDWRAP | SS_ENDELLIPSIS`, no
`SS_NOPREFIX`; text set at `:208` and `:237`.

The `PERMREQ` body is `permissions.summary()`, which for an MCP `exec` with no
description falls back to the command line itself
(`bridge/agent/permissions.py:228-249`, whose own docstring says *"this is all a
user paging through history will see, and the whole decision rests on it"*).

**The failure.** The agent asks to run `rm -rf build && make`. A STATIC without
`SS_NOPREFIX` renders `&&` as a single `&`, and a lone `&` disappears entirely
and underlines the next character. So `cmd /c backup & del *.bak` is displayed
as `cmd /c backup  del *.bak`. The user approves a visibly different command
from the one that runs — and `&`, `&&` and `||` are precisely the operators that
turn one command into two.

The codebase already knows the hazard: `chatlist.cpp:314` and `:343` pass
`DT_NOPREFIX` explicitly. `SS_NOPREFIX` appears nowhere in `win/app/`.

**Fix.** Add `SS_NOPREFIX` at `bar.cpp:84`. One flag.

---

## 01.14 — The sync root is re-read from an unlocked global on every SYNC frame, and is not captured per transfer

**Where:** `win/sync/syncrpc.cpp:21` (`g_root`), `:33-38` (setter), `:136`,
`:288`, `:352`, `:381`, `:422-432`, `:518-524` (readers);
`win/app/workdir.cpp:70`.

`rc_syncrpc_set_root` writes `g_root` with `rc_strf` on the **UI thread** —
from `on_optmenu` → `rc_workdir_apply` on an `OPTIONSEND` naming a different
folder, and from `try_connect` on every reconnect attempt. `do_scan`, `do_put`,
`do_putend`, `do_mkdir` and `do_get` all read it on the **RPC worker thread**,
with no lock on either side. `rpc.cpp` keeps its own copy of the same string and
guards it with `g_lock` (`rpc.cpp:249-251`); `syncrpc.cpp` does not.

**(a) Torn read.** An `rc_strf` into `g_root` interleaved with the worker's
`rc_sync_join(full, sizeof(full), g_root, rel)` produces a path spliced from the
old root and the new. The worker then `CreateFileA(CREATE_ALWAYS)`s,
`DeleteFileA`s and `MoveFileA`s at that path. Narrow window, destructive result.

**(b) A transfer straddling a switch — no race needed.** `do_put`'s first chunk
opens `<root A>\<rel>.gaston-tmp` and stores the full temp path in `g_put.tmp`.
If the work directory changes before `SYNCPUTEND`, `do_putend` calls
`rc_sync_commit(g_root, g_put.rel, g_put.tmp, ...)` with the **new** root, so
`full` is `<root B>\<rel>` and `MoveFileA(tmp, full)` (`sync.cpp:605`) moves the
file out of tree A and into tree B — or fails and leaves an orphaned
`.gaston-tmp` in A that the next scan offers to the bridge as a new file.
`g_put` already captures the id, the relative path and the temp name for the
life of the transfer; the root is the one piece it looks up fresh.

**Fix.** Capture the root into `g_put` at `off == 0` alongside `rel` and `tmp`,
and commit against that. Guard `g_root` with the `ScopedLock` idiom `rpc.cpp`
already uses.

---

## 01.15 — A failed `DoVerb` or `QueryInterface` wedges the transcript permanently, because the fingerprint was already recorded

**Where:** `win/app/htmlview.cpp:516-520` (fingerprint stored), `:554-556`
(`DoVerb` failure return), `:558-560` (QI failure return).

`doc_len`/`doc_sum` are written at `:519-520`, **before** the control is built.
If `DoVerb` fails — plausible on this target under memory pressure, or with a
partially registered `shdocvw` — the function returns with `d->obj` **valid** and
`d->web` null. On the next reconnect the bridge re-sends the identical DOC
skeleton, the guard at `:517` sees `d->obj.valid() && len == d->doc_len && sum
== d->doc_sum` and returns immediately. Nothing ever retries.

**The failure.** `poll()` returns 0 forever at `:596`, every frame goes into the
pre-ready queue until it hits its 64KB cap, and from then on each block is
dropped with a log line. The window is a blank rectangle for the rest of the
session and reconnecting cannot fix it. Note `CoCreateInstance` failing at `:548`
does **not** have this problem — `obj` stays null, so it retries. Only the two
later failures latch.

The same latch also makes 01.16 unrecoverable.

**Fix.** Record `doc_len`/`doc_sum` only after the control is fully up (after
`:560`), or `d->obj.reset(NULL)` on the two failure returns.

---

## 01.16 — `view.htm` is written with unchecked `fputs`/`fclose`

**Where:** `win/app/htmlview.cpp:528-531`.

`fopen` is checked; `fputs(doc, f)` and `fclose(f)` are not. A full disk
truncates the skeleton mid-document. `renavigate()` loads it, MSHTML renders
whatever parsed, `find_id` fails for `#t`/`#l`, `poll()` logs *"document has no
#t/#l containers"* at `:615` and returns 0 — forever, because the bridge re-sends
identical bytes and 01.15's guard suppresses the retry. Blank transcript, one
log line, no user-visible error. A read-only exe directory (CD-ROM or network
install) produces the same blank window through the `fopen` failure at `:529`,
which is logged and never surfaced.

**Fix.** Check both returns; on failure clear the recorded fingerprint and
surface the error through `notice()`/`error()`.

---

## 01.17 — `rc_net_send` can block the UI thread for five seconds, and then reports a full queue as "the connection is down"

**Where:** `win/net/net.cpp:709-726`, `win/app/main.cpp:512-527`.

`RC_OUTQ_MAX_BYTES` is a hard 64KB cap (`net.h:69`), correctly. When it is
reached `rc_net_send` waits 50 × `WaitForSingleObject(outq_room, 100)` — up to
**five seconds** — then fails. `ui_send` calls it directly on the UI thread from
the bar's Allow/Always/Deny, Send, the chat-list switch, `IDM_NEW`,
`IDM_EXPORT`, `IDM_PERMS` and `SETWORK`. The message pump does not run during
that wait.

**The failure.** A `SYNCGET` is streaming: `do_get` pushes 8KB `SYNCDAT` frames
from the worker (`syncrpc.cpp:563`) and fills the queue. At 40kbit — the link
`docs/BACKLOG.md:14` names as the gate — 64KB takes about thirteen seconds to
drain. Press Allow in that window and the window stops repainting for five
seconds, long enough for Win98's ghost frame, and then `ui_send` writes into the
transcript:

> ... could not be sent — **the connection is down**. It will not have taken
> effect. Try again once it reconnects.

The connection is not down and no reconnect is coming. The message sends the
user to look at the network for a backpressure event, and the permission request
it was answering is still outstanding on the bridge.

**This is also what will bite hardest in Phase 5 and Phase 6.** An attachment
upload and a screenshot strip stream are both guest-initiated bulk transfers;
written the obvious way — a loop calling `rc_net_send` from the handler where
the button was pressed — each inherits a five-second stall per chunk on the UI
thread.

**Fix.** (a) Distinguish "queue full, try again" from "link dead" in the return,
and have `ui_send` say which. (b) Give UI-originated frames a non-blocking path
— a much shorter wait, or a small reserved slice of the queue only the UI thread
may use — so a user gesture is never parked behind a bulk transfer. (b) is a
prerequisite for Phase 5's upload, not a nicety.

---

## 01.18 — A lost OPTIONSEND permanently breaks batch-start detection: menus duplicate and the current model can vanish

**Where:** `win/app/optmenu.cpp:80` (`opts_open == 0` gate) and `:179`
(`opts_open = 0`, only on OPTIONSEND).

`opts_open` is the only thing that makes the first `kind=model` row clear the
five popups. Nothing resets it on disconnect: `main.cpp:1531-1547` resets the
bar and aborts the sync PUT but never touches `g_optmenu`, and `rc_optmenu`
exposes no reset.

**The failure.** On a dialup link the connection drops between the last OPTION
row and OPTIONSEND — the bridge sends models → efforts → work → modes →
commands → optionsend, so the window is several frames wide. `opts_open` stays
1. On reconnect the bridge resends the whole batch, the first model row does not
reset, and every row **appends**. Per outage: the Model menu lists every model
again; `menu_work` gains three more rows; `effort_ok` (`:92`) is never cleared,
so switching to Haiku leaves the Effort button claiming a level applies to a
model that has none (`toolbar.cpp:260`). After four outages `n_model` hits
`MAX_OPTS` (16) and further rows are dropped at `:97` — possibly including the
row carrying `current=1`, so `cur_model` stays empty and the toolbar shows the
bare noun "Model" for the rest of the session.

Related, same file: the `work` branch (`:138-164`) has no counter and no
`< MAX_OPTS` guard at all, unlike model/effort/mode/command. Today's bridge
sends exactly one row (`server.py:3487`), so it is bounded by the peer's good
behaviour alone — the assumption `frameproto.cpp`'s comments repeatedly refuse
to make.

**Fix.** Reset on the first row of *any* kind when `opts_open == 0`, and give
the class a `reset()` that the disconnect branch calls beside
`g_bar->hide_all()`. Add the missing cap to the `work` branch.

---

## 01.19 — `WM_SETREDRAW(FALSE)` latches if SESSIONSEND never arrives, so the visible list and the backing array disagree

**Where:** `win/app/chatlist.cpp:225-232` (`begin_rows`) vs `:234-241`
(`end_rows`, the only thing that turns redraw back on), driven from `:153` and
`:221`.

**The failure.** The link drops between SESSIONS and SESSIONSEND — a 64-row list
on a slow link is a wide window. `LB_RESETCONTENT` has already run with redraw
off, so the **pixels** still show the old list while `chat_ids[]` and
`chat_is_header[]` hold the new one. `list_proc` (`:145`) posts
`WM_RC_OPENCHAT` on any `WM_LBUTTONUP`, and `open_selected` reads
`chat_ids[sel]` (`:111`). The user clicks the row *showing* "Fix the parser" and
switches to whatever conversation now occupies that index — tearing down the
live session, which `chats_confirm` warns about but cannot prevent, because the
user consented to a different switch from the one that happens. The redraw flag
also stays off for every later `LB_ADDSTRING` until a complete batch lands.

**Fix.** A public `abort_rows()` called from the disconnect branch, or a
watchdog so `begin_rows` cannot leave the flag down indefinitely.

---

## 01.20 — `rc_rpc_join` closes the watcher handle from the calling thread while the worker may be waiting on it

**Where:** `win/rpc/rpc.cpp:731-737`, against `win/rpc/rpc.cpp:236-245`.

```c
int rc_rpc_join(DWORD timeout_ms) {
    if (g_watch != INVALID_HANDLE_VALUE && p_close_change) {
        p_close_change(g_watch); g_watch = INVALID_HANDLE_VALUE;
    }
    if (!g_thread) return 1;
    w = WaitForSingleObject(g_thread, timeout_ms);
```

The handle is closed **before** the join. `rpc_thread` waits on it at `:492-497`
(`hs[1] = g_watch; WaitForMultipleObjects(...)`). `rc_rpc_join` is called from
`rc_net_stop` on the UI thread (`net.cpp:766`) and from `rc_net_start`'s
sender-failure path (`net.cpp:542`); `rc_rpc_stop` has only cleared `g_running`
and signalled `g_wake`, so the worker can easily still be inside the wait.

This module already states the hazard, 495 lines earlier, as the reason for
*not* doing exactly this:

> It does NOT touch `g_watch`. That handle belongs to the worker thread — it is
> waited on there, reset there and closed there — and closing it from the UI
> thread while `WaitForMultipleObjects` holds it is exactly the kind of race
> that fails once a month on someone else's machine. — `rpc.cpp:238-243`

**The failure.** On shutdown the wait returns `WAIT_FAILED` and the loop exits,
usually harmlessly. The dangerous case is the reconnect one, where Win9x has
recycled the handle value: the worker then waits on, and may be signalled by, an
unrelated object, and `p_next_change(g_watch)` at `:503` is called on it.

**Fix.** Move the three lines below the `WaitForSingleObject`, or have
`rc_rpc_stop` set a flag and let the worker close its own handle — which is what
`:236` already prescribes for the other caller.

---

## 01.21 — `fclose` before the export rename is unchecked, defeating the temp-file design

**Where:** `win/app/export.cpp:196`, rename at `:222`.

Every `fwrite` at `:177` is checked, but stdio buffers. On a full floppy or
network drive the last buffer's write fails inside `fclose`, which returns EOF.
That return is discarded, `got == total` still holds — it counts bytes handed to
`fwrite`, not bytes on disk — and `MoveFileA` renames a **truncated** file into
the user's chosen name.

`export.h:17-21` states exactly this outcome as the thing the temp file exists
to prevent: *"a truncated file with the name the user chose, which is worse than
no file at all — they would open it, see most of a conversation, and have no way
to tell that the end is missing."* The code does not implement the comment.

**Fix.** `if (fclose(fp) != 0) { fp = NULL; report + cleanup(); return 1; }`.

---

## 01.22 — `Gaston-Replaced` overwrites its own previous generation

**Where:** `win/app/workcopy.cpp:327` — `CopyFileA(from, aside, FALSE)`. The
third argument `FALSE` means *overwrite if present*.

`workcopy.h:93` says `Gaston-Replaced` holds *"what was overwritten and would
otherwise be gone"*.

**The failure.** The user changes the work directory into `C:\Work` with
REPLACE, realises they wanted the other copy, changes back, then changes into
`C:\Work` with REPLACE again. The second run's stash overwrites
`C:\Work\Gaston-Replaced\notes.txt` — which was the only surviving copy of the
first generation. Bytes gone, nothing reported. This is the one finding in the
set that destroys user data on a path the user explicitly chose *in order to
keep* it.

**Fix.** `CopyFileA(from, aside, TRUE)`; on `ERROR_FILE_EXISTS` stage as
`<name>.1`, `<name>.2`. A stash failure must be a refusal to overwrite, which
`:374` already does correctly.

---

## 01.23 — Hiding the focused button drops the keyboard on the floor

**Where:** `win/app/bar.cpp:194-198` — `show()`'s no-entry branch hides all four
controls, one of which (`allow`) is the one `show()` gave focus at `:221`.
`bar_changed` (`main.cpp:497-501`) re-runs layout and enables but never restores
focus.

**The failure**, on the most common path in the app: the user presses Allow →
PERMEND arrives → `pop()` → `show()` hides the focused button. Focus lands on
the frame window, which has no text handling. The user carries on typing and the
characters go nowhere until they click the input box.

**Fix.** `SetFocus` back to the input box before hiding, or have `bar_changed`
call `g_inputmod->focus()` when `!g_bar->active()`. Fixing 01.1 by not taking
focus at all fixes this too.

---

## 01.24 — `set_autoscroll` is called only on the branch where the view failed to boot, and the `set_follow` beside it is a dangling `else`

**Where:** `win/app/main.cpp:1249-1281`.

```c
    else
        /* The INI's autoscroll, applied. Audit 01.2 said this setting was
         * "loaded from the INI, ... and read by nothing", gave the view a
         * set_autoscroll() to fix it -- and then nobody called it. The
         * finding was marked closed on the strength of the method existing.
         * One line makes the claim true. */
        g_html->set_autoscroll(g_cfg.autoscroll);
        if (g_pagebar) g_pagebar->set_follow(g_cfg.autoscroll);
```

Two defects from one pair of missing braces.

1. `set_autoscroll` is inside the `else`, so it runs **only when `boot_local()`
   returns false** — when the cold-start document beside the exe could not be
   loaded. On every normal launch it is not called at all.
2. `if (g_pagebar) g_pagebar->set_follow(...)` is not part of the `else` — it is
   unconditional — but `g_pagebar` is NULL at line 1281, because
   `rc_pagebar::create` does not run until line **1309**. So it is always a
   no-op, whichever branch ran.

**The failure.** Set `autoscroll=0` in `retroclaude.ini`. The INI round-trips
(`inicheck.exe` proves it), and the transcript still yanks to the bottom on
every append, because the view was never told. Toggling Follow at runtime does
work (`main.cpp:1616-1623` is a separate, correct path), so only the *persisted*
preference is dead — the one that matters across restarts.

Worth noting for its own sake: audit 01.2 was closed once on the method
existing, then closed again on this call existing. It is still not true. A
finding about a setting nothing reads needs a test that reads it, not a call
site.

**Fix.** Brace the `else` and move both calls below line 1309.

---

## 01.25 — `recover_dir` deletes and renames files at paths produced by an unchecked join

**Where:** `win/sync/sync.cpp:652`, `:659-670`.

```c
rc_sync_join(aside, sizeof(aside), root, child);   /* :652, return dropped */
... MoveFileA(aside, real);                        /* :662 */
rc_sync_join(aside, sizeof(aside), root, child);   /* :669, return dropped */
DeleteFileA(aside);                                /* :670 */
```

`child` is built at `:642-643` by another unchecked `rc_strf` and grows with the
tree's depth; `root` is a user-chosen work directory.

**The failure.** `rc_sync_recover` runs at the top of every `rc_workdir_apply`
(`workdir.cpp:69`), i.e. whenever a conversation commits to a folder. In a deep
tree under a long root, a truncated `aside` names some *other* existing path,
and the guest then either `DeleteFileA`s it or `MoveFileA`s something over it —
silently, at startup, inside the user's own work directory. Low probability,
unrecoverable, and a direct breach of *"the person at the keyboard never loses a
byte"*.

`rc_sync_join`'s own header (`sync.h:122-128`) states the rule: *"a caller that
ignores the return value stats, opens or renames the wrong thing with no way to
find out."* The walk in `walk()` at `:452` does check, and is the model.

**Fix.** `if (!rc_sync_join(...)) { rc_log(...); continue; }` at both sites.

---

## 01.26 — Cancelling during the sync wait tells the user the opposite of what happened

**Where:** `win/app/workdlg.cpp:694` → `:749-763`.

`propose(out, mode)` at `:694` has already sent `SETWORK`. If the user then
presses Cancel while "Sending to the assistant..." is up, `wait_for_sync`
returns on `p->cancelled` and `:755` shows *"Stopped. This conversation still
works in C:\Old"*. It does not — the proposal is in flight, and the next OPTION
batch applies the new folder. The user is told their files are in one place and
the agent is working in another.

**Fix.** Latch whether cancellation happened before or after `propose`; after,
say the folder changed and the transfer was interrupted.

---

## 01.27 — The chat-list cache is written unchecked and renamed over the good copy

**Where:** `win/app/cache.cpp:236-241` (`add_chat`), `:243-254` (`end_chats`).

`rc_cache::add` at `:151` correctly checks `fwrite` and drops the page on
failure. `add_chat` checks nothing, and `end_chats` ignores `fclose` and then
does `DeleteFileA(dest); MoveFileA(tmp, dest)` unconditionally. A disk that
fills partway through a 40-conversation list replaces a complete `CHATS.TXT`
with a truncated one, and the next cold start shows a chat list missing its tail
with nothing to say why. `begin_chats`'s `fopen` failure at `:233` is also
unlogged, unlike every other `fopen` in the file.

**Fix.** Track a write-failed flag the way `add`/`page_fp` does, and have
`end_chats` delete the temp rather than rename when it is set.

---

## 01.28 — The page bar is the only user-initiated send in the tree that swallows failure, and it has no loading gate

**Where:** `win/app/pagebar.cpp:43` (unchecked `rc_net_send`) and `:195`
(`command()` ignores `loading_page`).

`main.cpp:503-511` carries a seven-line comment stating the rule — *"Ignoring
that turns a button press into silence"* — and `bar.cpp:418` follows it for
RESUME. Here, pressing Next on a link that has just dropped does nothing at all:
no message, no log line. Separately, holding Next on a slow link queues N
`GETPAGE`s, each of whose PAGESTART calls `html->clear()` (`:169`) — N page
loads of bandwidth and a flickering transcript.

**Fix.** Route through `ui_send` like the bar and chat list do; gate `command()`
on `!loading_page`.

---

## 01.29 — `make -C win check` audits for `W`-suffixed imports, not for NT-only ones

**Where:** `win/Makefile:306-321`, against `CLAUDE.md`'s standing rule and
`win/Makefile:158-165` (the `probe` target, which does it properly).

The `check:` target prints the DLL list and greps the import table for names
ending in `W`. That catches the Unicode-stub class. It does not catch the class
the rule is actually about: an **ANSI** export that does not exist on 9x. The
`probe` target forty lines above greps four such names by hand
(`SetHandleInformation|OpenSCManager|CM_Get|GetGuiResources`) — the mechanism
exists and was not carried into the gate guarding the shipping binary.

**The failure.** `win/sync/sync.cpp:585` says, in a comment, exactly what
somebody will eventually want to do:

> There is still an instant with no file at the path —
> `MoveFileExA(REPLACE_EXISTING)` is NT-only so this cannot be made atomic

Add that call to make the commit atomic and `make check` passes clean: the DLL
list still says KERNEL32, there is no `W` suffix, CMOV is unchanged, BSS is
unchanged. The image then dies at load on Win98, before `WinMain`, with no log
file — the one failure mode `CLAUDE.md` names as undebuggable. The same hole is
open for `SetHandleInformation` (which `exec.cpp:158` documents avoiding),
`GetDiskFreeSpaceExA` (`workcopy.cpp:80` documents avoiding), `CreateHardLinkA`,
`GetFileAttributesExA`, `TryEnterCriticalSection` and
`InitializeCriticalSectionAndSpinCount`.

The tree is clean today — a grep across `win/` for two dozen such names finds
only the comments saying why each was avoided — so this is a missing gate, not a
live defect. It is ranked here because the rule it fails to enforce is the one
whose violation is both unrecoverable and undiagnosable.

**Fix.** A deny-list grep over the import table in `check:`, seeded from the
names the codebase's own comments already say it avoids. Better: an allow-list
generated once from a Win98 SE `kernel32`/`user32`/`gdi32` export dump, checked
into `tools/` — a deny-list only catches what someone thought of.

---

## 01.30 — `do_put` can read past the end of the frame allocation when the length prefix has no separator

**Where:** `win/sync/syncrpc.cpp:262-264`.

```c
while (body[plen] && body[plen] != ' ' && plen < 16) plen++;
rel = body + plen + 1;
```

The loop is correctly bounded by the frame's NUL, which the reader guarantees
(`net.cpp:172`, `payload[len] = '\0'` over a `len + 1` block). But if the loop
stops on that NUL rather than on a space, `rel` is one byte **past** the
allocation. `rc_sync_contained(rel)` at `:285` then reads from there and scans
forward until it happens to find a zero byte.

**The failure.** A `SYNCPUT` whose body is a bare number with no space — a
malformed or truncated frame, or a bridge bug in `put()` — walks the guest off
the end of a heap block and interprets whatever follows as a path. Win98 puts no
guard page after a small heap block, so this does not fault; it reads adjacent
heap and acts on it. `sync.cpp:288-291` is the reason this matters: *"this
function exists for the case where the bridge is the thing that is wrong. It
cannot lean on a guarantee made by the party it is checking."*

**Fix.** `if (body[plen] != ' ') { put_fail(net, id, "bad_path"); return; }`
before computing `rel`.

---

## 01.31 — The copy pre-flight does not reserve the staging prefix, so REPLACE/MERGE refuses file-by-file after the copy has started

**Where:** `win/app/workcopy.cpp:145` (`need = strlen(dst) + 1 + strlen(rel) +
1`) against `:315-327` (`stash` forms `dst\Gaston-Replaced\rel`, 16 bytes
longer).

**The failure.** `dst = "C:\Projects\Retro"` (17 chars), a 230-char `rel`. The
survey computes 249 ≤ 260 and passes. During the copy, `dosjoin(aside, ...)`
needs 265, fails, `stash` returns 0, and the file is counted `failed` at
`:368`/`:374` — with no `on_error` call, no retry offer and no per-file
explanation. `workcopy.h:12` promises *"every refusal is made while nothing has
changed, so a refusal never leaves half a tree behind"*; this one does. It is
the same class `RC_SYNC_WORK_SUFFIX` (`sync/sync.h:25-41`) exists to prevent on
the sync side, where the reserve *is* computed and declared to the bridge.

**Fix.** Add `sizeof(RC_WORK_REPLACED)` to `need` in `survey_sink` when
collisions are possible, and refuse at survey time.

---

## 01.32 — `say_id` drops the message silently when it cannot allocate, and needs ~12× the text

**Where:** `win/app/htmlview.cpp:728-734`.

`cap = n * 6 + 64`, then a second block of `cap + 96`, both alive at once —
about 12× the input, twice over. Both `if (!esc.valid()) return;` and
`if (!div.valid()) return;` return with **no log line**, unlike `queue_add` at
`:393-396`, which was explicitly fixed for exactly this. The callers are
`notice()`, `error()` and `user()`; `error()` is reached from `main.cpp:524`
(connection down), `:697` (a bridge ERROR body, which under 01.7 can be a 256KB
inflated body) and `:724`.

**The failure.** On the machine's binding constraint, the message that explains
why things are failing is the message that disappears without trace.

The arithmetic itself is sound — 6 bytes is the true worst case (`&quot;`) and
the `+64`/`+96` margins cover `esc_into`'s early stop and the wrapper. Only the
silence is the defect.

**Fix.** `rc_log` on both failure returns.

---

## 01.33 — `status_refresh()` runs on every inbound frame and writes MSHTML each time; its own comment says "once a second"

**Where:** `win/app/main.cpp:1056` (the call), `:330-361` (the body),
`win/app/htmlview.cpp:898-914`.

`render_frame` calls `status_refresh()` unconditionally before dispatch. While a
turn is active that does eight `SendMessageA`s to the status bar and then builds
a 256-byte `<div class="status">` and calls `g_html->status(row)` — a
`MultiByteToWideChar` + `SysAllocString` + `put_innerHTML`, i.e. a real MSHTML
reflow. `rc_html::status`'s comment assumes this cannot happen:

> This line re-renders once a second for the whole turn. — `htmlview.cpp:909`

It re-renders once a second *from `TIMER_STATUS`*, **and** once per frame from
`render_frame`, with identical content between frames because the only variable
is a whole-second counter.

**The failure.** Throughput, on the machine least able to afford it: an extra
DOM write and reflow per streamed frame on a 200MHz Pentium, on top of the
`live()` write the frame actually carries. It also makes the streaming path's
cost invisible to anyone reading `htmlview.cpp`, because the comment there
states a frequency that is not true.

**Fix.** Drop `status_refresh()` from `render_frame` — `TIMER_STATUS` already
fires every second and `g_xfer->note_rx` is separate and cheap — or gate the
`status()` write on the string having changed. Correct the comment either way.

---

## 01.34 — The nesting check is a string compare, so 8.3 aliases and mapped drives walk past it

**Where:** `win/app/workcopy.cpp:18-46`, used at `:217`. `ID_PATH` is an
editable EDIT control (`workdlg.cpp:600-601`), so a typed path reaches this
unchanged.

**The failure.** Current folder `C:\Gaston Work`; the user types
`C:\GASTON~1\Copy`. `inside()` says no. The copy then writes into the tree it is
reading: `rel` `Copy/x` becomes `C:\GASTON~1\Copy\Copy\x`, the walk descends into
it, and the destination grows `Copy\Copy\Copy…` until `RC_SYNC_MAX_DEPTH` (16)
or `RC_SYNC_MAX_FILES` (2000) stops it. Bounded, so no hang and nothing
destroyed — but the destination is ruined, the walk returns −2, and the whole
garbage tree is then mirrored to the bridge. A mapped drive against its UNC path
does the same.

**Fix.** Run both through `GetFullPathNameA` and `GetShortPathNameA` (via
`GetProcAddress`, per the standing rule) and compare both forms. SUBST is not
resolvable on 9x — say so in the comment rather than implying the check is
complete.

---

## 01.35 — `rc_net_start`'s reader-thread failure path cleans up nothing, unlike the sender path twelve lines above it

**Where:** `win/net/net.cpp:535-554`.

The sender path is thorough (`rc_rpc_stop`, `rc_rpc_join`, `closesocket`,
`running = 0`, `set_state`) and its comment explains why. The reader path does
none of it: it logs, sets `RC_FAILED` and returns with a live sender thread on
an open socket, `n->running` still 1, and the RPC worker still running.

`_beginthreadex` fails under the same USER/GDI/thread pressure that makes
`CreateWindowExA` fail — a concern this codebase cites six times. Recovery does
save it today: `WM_RC_STATE` arms `TIMER_RECONNECT`, `try_connect` calls
`rc_net_stop` first (`main.cpp:1399`), and `n->started` is 1 so teardown runs.
So this is a latent asymmetry, not a live leak — but it depends entirely on
every future caller remembering that ordering, which `net.cpp:353-361` records
as having already been broken once at the cost of "three bridge restarts".

**Fix.** Make the reader path do what the sender path does, or restructure both
as one `goto fail` with the teardown written once.

---

## 01.36 — Phase 6: the lane table invites `list_windows` onto the reader thread, where `net.h`'s non-negotiable rule forbids it

**Where:** `win/net/frameproto.h:210-214` and `win/rpc/rpc.cpp:106-121`, against
`win/net/net.h:5-8` and `docs/BACKLOG.md:674-694`.

`frameproto.h` documents the `IMMEDIATE` lane and names its intended second
member:

> **IMMEDIATE** — answered on the READER thread, before either worker queue is
> even touched. [...] EXECKILL is the only member today, and **a quick syscall
> like Phase 6's `list_windows` belongs here too.**

`net.h:6-7`, marked "not negotiable":

> the reader thread **NEVER** calls a window API that can block or re-enter.

`list_windows` is `EnumWindows` plus a cross-process
`SendMessage(WM_GETTEXT)` per node — `docs/PLAN.md:1577-1579` requires
`WM_GETTEXT` specifically. A cross-process `SendMessage` blocks until the
*target's* message loop answers.

**The failure.** A `list_windows` implemented where the lane table says to put it
will, the first time it walks a hung application, block the socket reader
indefinitely. No frames are read: the transcript freezes, no `PERMEND` arrives,
no `SYNCDAT` moves, and the bridge times out every outstanding future. Nothing
in the guest says why; the log's last line is the frame before the one that
wedged.

`docs/BACKLOG.md:674-676` records the thread-affinity gap but states the opposite
premise — *"`EnumWindows` and `SendMessage(WM_GETTEXT)` are safe from the RPC
worker thread"* — which is true, and about a **different thread** from the one
`frameproto.h` routes them to. So the backlog entry does not cover this; the two
documents disagree and neither knows it.

**Fix.** Change `frameproto.h:212-214` now, while it costs a sentence: state
that `IMMEDIATE` is for calls that cannot block and cannot re-enter, and that
anything touching another process's message queue belongs in `JOB` or a lane of
its own. Today's only member genuinely qualifies; the comment is what will
mislead whoever writes the next one.

---

## 01.37 — Smaller, verified, each with a real but narrow failure

- **`goto_n` clamps in the wrong order and `total` is unvalidated wire data.**
  `win/app/pagebar.cpp:46-53`: `if (n < 1) n = 1; if (n > total) n = total;`.
  `total` is `atoi` of a wire header at `:164`, `:185`, `:223`, `:231` with no
  validation — `atoi` of an absent or non-numeric value is 0, `atoi("-5")` is
  -5. With `total == 0`, `goto_n(1)` yields `n = 0` and `GETPAGE page=0` goes on
  the wire. Nothing indexes an array with it, so this is protocol hygiene rather
  than memory safety — but it is the one place in the UI where a wire integer
  drives arithmetic unchecked. *Fix:* clamp `total >= 1` where it is parsed and
  swap the two clamps.

- **`chatlist` writes `net->sess`, which the connect path reads unsynchronised.**
  `win/app/chatlist.cpp:246`. `sess` is `rc_net` state (`net/net.h:90`), written
  here on the window thread, also written at `main.cpp:563`, and read by HELLO's
  builder at `net/net.cpp:463` on the connect path. A reconnect racing a
  `SWITCHED` reads a half-written 64-byte buffer and resumes the wrong
  conversation or none. Narrow, cheap to fix, and the clearest module-boundary
  break in the tree: `chatlist.h:5-10` claims this class owns "id-keyed
  bookkeeping", and `sess` is not its. *Fix:* hand the id back through the
  existing `switched` callback and let `main.cpp` be the single writer.

- **`reject_busy` and `reject_sync_busy` answer nothing when the frame has no
  `id`.** `win/rpc/rpc.cpp:440` and `:621`; same shape at `:342`,
  `syncrpc.cpp:256`, `:501`. The frame is dropped and the bridge — which
  allocated a future — waits out its whole budget: 120s exec, 300s put/get, 600s
  scan. Only `run_one` logs. `rpc.cpp:597-600` explains this exact cost for a
  *wrongly shaped* ack and treats it as the reason lane-specific ack types
  exist; the no-ack case is worse and uncovered. *Fix:* log at every site.

- **The open conversation's label is measured with the wrong font.**
  `win/app/chatlist.cpp:72` selects `font_normal` for `fit_label`'s
  `GetTextExtentExPointA`, but `:339-340` draws the row at `chat_current` with
  `font_bold`. The bold string is wider than what was measured, so the row
  overflows — which is the exact condition `chatlist.cpp:392-395` documents as
  making Win98's listbox paint over its own `WS_EX_CLIENTEDGE` border,
  *"measured as white pixels at x=8,9"*. The documented fix does not cover the
  one row most likely to hit it.

- **`rc_config_save` reports nothing when the INI cannot be written.**
  `win/core/ini.cpp:51-59`, five `WritePrivateProfileStringA` returns discarded.
  On read-only or full media the user changes host or sync root, the dialog
  closes as if accepted, and the next launch reads the old values back. No log,
  no message. `cache.cpp:47-51` has a careful comment about precisely this shape
  (*"a cache that quietly does nothing is exactly the kind of thing that gets
  reported ... a month later"*) and `ini.cpp` does not follow it.

- **The survey creates the destination.** `win/app/workcopy.cpp:225` calls
  `rc_mkpath(dst)` inside `rc_worksurvey_run`, whose header (`workcopy.h:53`)
  says *"Look, and change nothing"*. A mistyped deep path leaves an empty tree
  on disk after a TOODEEP/TOOBIG refusal. Cosmetic in itself; the cost is that
  the rest of the module reasons from the header's guarantee.

- **`tail[200]` cannot hold the sentence that says where the staged files went.**
  `win/app/workdlg.cpp:711`, used at `:714`/`:721`. `out` can be 259 bytes, so
  the template plus `"%s\\%s"` guarantees truncation for any deep destination —
  cutting the one sentence that is the recovery instruction for finding 40
  replaced files. *Fix:* `MAX_PATH + 96`, or put the folder on its own line.

- **The cache path chain ignores four `rc_strf` returns.**
  `win/app/cache.cpp:43`, `:69`, `:89`, `:142`, `:166`, `:101`, `:118`, `:232`,
  `:250`, `:262`. With an exe directory near 250 characters, `path_for`
  truncates the `P%03d.HTM` suffix so `P001.HTM` and `P002.HTM` resolve to one
  name; page 2 overwrites page 1 and `page_count()` stops at 1, so paging back
  offline shows the wrong page silently. Unlikely on a `C:\GASTON` install;
  listed because it is the ignored-return class this codebase set out to close.

- **`tmp` can collide with `dest` at the MAX_PATH boundary.**
  `win/app/export.cpp:120` — `rc_strf(tmp, sizeof(tmp), "%s.$$$", path)`, return
  ignored, with `path` up to 259 chars from `GetSaveFileNameA`. At 259 the
  format truncates back to exactly `path`, so `tmp == dest`: `fopen(tmp, "wb")`
  truncates the user's chosen file in place, `EXPORTEND` deletes the file just
  written, and every mid-transfer `cleanup()` (`:173`, `:182`, `:206`, `:214`)
  does `DeleteFileA(tmp)` — i.e. deletes `dest`. Needs a deep directory, so the
  probability is low; the outcome is losing a pre-existing file the bridge never
  touched.

- **One `HBITMAP` and two `HMENU`s leaked for the process lifetime.**
  `win/app/pagebar.cpp:107-111` loads without `LR_SHARED`, hands it to
  `BM_SETIMAGE` (which does not take ownership), and `~rc_pagebar()` is `{}`
  (`pagebar.h:88`). `win/app/optmenu.cpp:30` and `:40` create `menu_work` and
  `menu_attach`, which unlike the other four are never appended to the menu bar
  (`:54-59`), so `DestroyMenu(bar)` does not reach them and `~rc_optmenu()` is
  empty. Three handles on a fixed 64KB USER/GDI heap, one instance each — small,
  but this is the heap the codebase is otherwise careful about.

---

## 01.38 — Latent: correct today, and only because of an invariant nothing states

None of these has a reachable failure. They are listed because each is one edit
away from one.

- **`chatlist.cpp:297`** — `LB_GETTEXT` into `char text[192]` with no length
  bound; `LB_GETTEXT` has no size parameter. Safe only because the only strings
  added are `label[160]` (`:175`) and `chat_group[40]`. Widening `label` past
  192 makes this a stack overflow with no diagnostic. An `LB_GETTEXTLEN` check,
  or sizing `text[]` from `label[]`, closes it.
- **Option ids truncate at 63 bytes with the bound written as a literal.**
  `optmenu.cpp:109`, `:123`, `:129`, `:170` — `rc_strf(opt_model[n_model], 64,
  ...)` rather than `sizeof`, return ignored, against a wire value of up to 255.
  Unreachable with today's ids; the buffer is sized by a literal three lines
  from the array declaration, which is the shape that goes wrong on the next
  widening.
- **`bar.cpp:276` / `chatlist.cpp:205`** — id truncation into 80- and 64-byte
  buffers, returns discarded. Halt ids are 9 bytes (`bridge/synccoord.py`), tool_use
  ids ~30, and `chat_ids[64]` is symmetric with `rc_net::sess[64]` so the
  `strcmp`s still agree. The guest cannot represent a session id ≥ 64 bytes at
  all and nothing says so out loud.
- **`ini.cpp:10-11`** — `out[n - 1] = '\0'` with no lower bound on `n`;
  `rc_strf` returns early without touching `out` when `cap == 0`, so `cap == 0`
  writes `out[-1]`. Both callers pass `sizeof(path)`. The line is redundant
  anyway (`rc_strf` always terminates) and its real cost is looking like a
  safety net.
- **`htmlview.cpp:427` and `:525`** — `GetModuleFileNameA(NULL, dir,
  sizeof(dir) - 1)`, return discarded, into an uninitialised `MAX_PATH` stack
  buffer; `strrchr` then reads garbage on failure. The `- 1` also reintroduces
  the `_snprintf` non-termination hazard `core/str.h` exists to kill, through a
  different API. Same shape at `main.cpp:247`.
- **`workdir.cpp:150` and `workdlg.cpp:628`** — `cap`-taking copies whose
  returns are discarded. Safe because the only caller passes `MAX_PATH`
  (`main.cpp:1746`), but both signatures accept an arbitrary `cap` and the
  truncated result becomes the copy destination (`workdlg.cpp:660/687`) and the
  `SETWORK` payload.
- **`xferstat.cpp:140-146`** — `note_tx_start` runs before `rc_net_send` and
  `note_tx_done` only on success, so a failed send leaves `tx_total` set with
  `tx_done` at 0; it self-clears after 2500ms. And `note_tx_done` fires when the
  frame is *queued*, so on a slow link the pane reads "tx 4096/4096" before a
  byte reaches the wire. Both cosmetic.

---

# Architecture

**`main.cpp` is 2113 lines, up from the 1302 the last round's extraction left —
and that is mostly legitimate.** Six modules did come out and stayed out
(`bar`, `chatlist`, `xferstat`, `pagebar`, `optmenu`, `input`), and the growth
since is nine new frame types plus `export`, `cache`, `toolbar`, `workdlg`,
`hostdlg`, `tooldet` and `workdir` — all of which are their own files. The
extraction pattern held: every new feature landed as a class with a `create()`,
a destructor called from `WM_DESTROY`, and callbacks back into `main.cpp` for
the policy questions it does not own. That is the right shape and it was
followed consistently.

**The frame dispatch table is complete.** Cross-checking every `Frame("TYPE"`
the bridge constructs against `g_dispatch` (`main.cpp:1016-1045`) and
`RC_LANE_TABLE`: every inbound type is handled, with no fall-through to
"unhandled frame type" for anything the bridge can actually send. `PING` is
guest→bridge only and `PONG` is handled. This was a real risk the last round
named and it is closed.

**One clean cut remains.** The status bar — `g_status`, `g_ico_ok/warn/bad`,
`g_ico_shown`, `g_ico_pie[]`, `g_ctx_pct`, `status_set`, `status_refresh`, the
four-pane setup at `main.cpp:1349-1361` and the icon loading at `:2015-2035` —
is ~180 lines and six globals whose only reader is `status_refresh`. It is the
same shape as the five modules already out, it would take 01.33 with it, and it
is the piece Phase 6 will want to add a state to. Nothing else in `main.cpp`
still wants moving: `layout`, `create_children`, `wnd_proc`, `try_connect`, the
turn enum and the frame handlers are what an entry point is.

**The `ui_send` convention is now followed almost everywhere** — the last round's
finding 01.6 was real and the fix took. `pagebar.cpp:43` (01.28) is the single
remaining user-gesture send that does not go through it.

---

# Phase 5 and Phase 6

Three things in the way that are not already owned by `docs/BACKLOG.md` §6 or §9:

1. **`rc_net_send` blocks the calling thread for up to five seconds under
   backpressure (01.17).** Phase 5's attachment upload and Phase 6's screenshot
   strips are both guest-initiated chunk loops. Written from a button handler
   they inherit a five-second UI stall per chunk on a slow link. The outbound
   path needs a non-blocking option before either lands.
2. **The `IMMEDIATE` lane's own comment routes `list_windows` onto the reader
   thread (01.36)**, where it will block the socket on the first hung window.
   One sentence to fix now; a day to find later.
3. **`inflate_body`'s fixed 256KB per frame (01.7)** is the memory headroom
   Phase 6's strip capture is supposed to be operating inside. A capture that
   carefully streams 4KB strips into a fixed buffer shares a heap with a reader
   thread taking 256KB per compressed frame.

`docs/BACKLOG.md` §6 (the one chunked-transfer primitive), §9 (thread affinity,
per-job cancellation, `list_windows` scope, inline images) and §2 (the memory
pass) already own the rest and are accurate as written.

---

# Checked and clean

Stated so the examined parts do not look unexamined.

- **No NT-only static imports.** A grep across `win/` for two dozen NT-only
  names finds only the comments explaining why each was avoided. `shell32`
  (`workdir.cpp:112-119`, `workdlg.cpp:507-509`) and `GetDiskFreeSpaceExA`
  (`workcopy.cpp:96-97`) go through `LoadLibrary`/`GetProcAddress` with correct
  fallbacks and `FreeLibrary` on every path including the failure ones. The
  directory watcher in `rpc.cpp:255-281` does the same and treats absence as a
  supported outcome. 01.29 is about the missing *gate*, not a live violation.
- **No raw `strcpy`/`strcat`/`sprintf`/`gets`/`alloca` anywhere in `win/`**, and
  only two live `_vsnprintf` calls — `core/str.c:18` (the helper itself) and
  `core/log.cpp:41`, which terminates before measuring and is correct at every
  length including exactly-full.
- **`frameproto.cpp` is clean.** `rc_frame_parse` never advances past `end`,
  every segment is NUL-terminated in place before it is read, over-long types,
  keys and values and duplicate keys all drop the frame rather than truncate,
  and `append_quoted` is a correct mirror of `unquote`. The reader's
  `payload[len] = '\0'` over a `len + 1` allocation is what makes every
  `strlen(f->body)` downstream safe; I checked that specifically, because most
  of the sync and view code depends on it.
- **COM ownership in `htmlview.cpp` is correct throughout** — ~20 `ComPtr`/`Bstr`
  sites traced, no leak, no double-release, no early return past a `Release`.
  `comptr.h` is clean: `ComPtr::reset` reads the member into a local before
  releasing (`:57-62`), which is the right handling for MSHTML teardown
  re-entrancy; both classes are non-copyable; `addr()` resets first; `Bstr` uses
  the OLE allocator.
- **`core/path.c` is clean.** Refuses rather than truncates, handles all three
  root shapes including UNC server *and* share, the cut-and-restore loop
  terminates on every input traced, and it uses `is_dir` rather than a bare
  attribute test so a file in the way is a failure.
- **`app/hostdlg.cpp` is clean and unusually careful** — the hand-built
  `DLGTEMPLATE` byte order is right including the `DS_SETFONT` trailer, the
  overflow sentinel is sticky and re-checked, `EM_LIMITTEXT` is derived from
  `sizeof(cfg->host)-1` rather than a literal, the port is validated and refused
  rather than silently kept.
- **`app/xferstat.cpp` is clean.** Bounds check out, the `GetTickCount` wrap is
  handled correctly by unsigned arithmetic *and* self-heals, wire `total`/`off`
  are display-only and clamped.
- **`app/input.cpp` and `app/toolbar.cpp` are clean.** `input`'s heap buffer is
  freed on every path, the `blocked_fn` re-check in `send_text` closes the
  Enter-key bypass, and the destructor un-subclasses before dropping
  `s_instance`. `toolbar` reads `clip_ix` back from `TB_ADDBITMAP` and handles
  its -1, and its `TTN_NEEDTEXTA` handler correctly uses `lpszText` with
  static-lifetime literals.
- **The bar's state machine itself is right.** The priority-derived `visible()`,
  the kind+id-keyed `pop()` and the refusal of a second same-kind different-id
  push (`bar.cpp:265`) genuinely close the pairwise-arbitration bugs the last
  round described. Findings 01.1, 01.13 and 01.23 are about the *controls*, not
  the machine.
- **Concurrency is clean outside the four findings that name it.** Every `app/`
  module runs entirely on the window thread; frames arrive by `PostMessage`, not
  `SendMessage`. The four-thread contract in `net.h` is honoured: the reader
  posts and never calls a blocking window API, the sender owns the wire alone,
  the three-phase shutdown ordering in `rc_net_stop:747-884` is correct
  (including the deliberate socket and critical-section leaks when a thread did
  not join, each argued at the point it happens). The exceptions are 01.14
  (`g_root`), 01.20 (`g_watch`), 01.37's `net->sess`, and `rpc.cpp:258`'s
  unlocked read of `g_sync_root` inside `rpc_watch_start` — the last of which is
  benign today because the string it re-reads is the one it just copied.
- **"The guest transforms nothing" holds.** No markdown, no pagination, no
  encoding folds, no JSON, no image diffing. `esc_into` (`htmlview.cpp:657-683`)
  escapes plain-text protocol fields the bridge deliberately sends unescaped and
  argues the case at length; `key_of` and `doc_fingerprint` are guest-local
  derivations; `tooldet` reports evidence and never conclusions
  (`tooldet.h:1-23`). The one genuine mirror is `rc_html::user()`
  (`htmlview.cpp:795-817`) duplicating `bridge/view/html.py`'s `text.strip()` —
  which is rendering policy living in the guest, kept in sync by a comment with
  no test linking the two. Not a bug; recorded as the one place the rule bends.
- **No file-scope objects with non-trivial constructors.** Every file-scope
  aggregate in `win/` is a POD array or a plain struct — `g_tmpl`
  (`workdlg.cpp:63`) is a `BYTE` array, `g_sections`/`g_rows` are `char` arrays,
  the four toolbar label buffers are `char` arrays. `make check`'s
  `__CPP_INIT|__sti__|_STI__` gate backs this up mechanically.
- **Stack.** `walk()` (`sync.cpp:374`) is the only recursion: ~1.15KB per frame
  (three `MAX_PATH` buffers, `WIN32_FIND_DATAA`, `rc_sync_stat`, saved regs),
  capped at depth 16, so ~19.6KB; the deepest sink chain hanging off it
  (`copy_sink` → `stash` → `make_parents` → `rc_mkpath`) adds ~1.7KB. Worst case
  ~21.3KB of a 64KB commit. The heap-allocation decisions in `do_scan`
  (`syncrpc.cpp:123`), `do_get` (`:502`) and `rc_sync_stat_file`
  (`sync.cpp:216`) are what keep it there, and the postmortem at
  `sync.cpp:167-182` explains why. No single frame approaches the 4096-byte
  gate. The one caveat is 01.9: `pump()` dispatches arbitrary message handlers —
  MSHTML re-entry, `try_connect` — on top of that 21KB, and nothing bounds that.

---

# Resolution — 2026-09-21

All 38 findings are closed. Written as LIVE text, per `00-audit-method.md`:
nothing above is commented out, so the reasoning that justified each finding
stays readable and re-checkable. One verdict per finding, and the verdict says
which kind of close it was.

**Verified how.** `make -C win` and `make -C win check` green (the latter now
actually enforcing — see 01.29). `make test`: **2861 passed, 2 xfailed, 0
failed**, up from 2829 before this work; the 32 new tests are listed below.
The two criticals were each proved by *reintroducing the original bug and
watching the new test fail*, then restoring the fix. And the whole thing was
run on the real Windows 98 guest, not only under Wine — see "On the guest".

Fixed: 01.1 THE BAR STOLE FOCUS ON EVERY PUSH -- `show()` now calls `SetFocus(allow)` only when no EDIT control has focus (`typing_now()`, bar.cpp). Taking focus is still right for a bar raised while the user is idle, which was always the real argument; the test is whether somebody is mid-sentence, not whether the bar deserves it. **Verified on the guest**: with a permission prompt up, typing "spacebar test" put the text in the input box and left the bar untouched. Before, the space pressed Allow.
Fixed: 01.2 `rc_sync_contained` WAS HANDED THE FILE'S CONTENT AS PART OF ITS PATH -- the parse is extracted to `rc_syncrpc_put_path` (sync.cpp), which copies the `rn`-bounded path first and checks containment on that copy. It does not fail open: the scan reaches the real path before any content byte, so the escape defence was intact throughout; only ordinary files were refused. **Verified on the guest end to end**: the agent wrote `hello.js` containing `// Prints a greeting to the console`, the bridge logged `pushed: 1` with zero refusals, and `TYPE C:\RCWORK\CREATEAF\HELLO.JS` on the guest printed the comment back.
Fixed: 01.3 `rc_exec_run`'s TRUNCATION GUARD WAS INVERTED -- `if (n < 0 || n >= (int)sizeof(cmd))` was `_snprintf`'s contract against `rc_strf`'s 1/0 return, so neither value could satisfy it and `RC_EXEC_TOO_LONG` was unreachable. Now `if (!fit)`. Covered by `exec_refuses_an_overlong_command` in rpccheck, which drives the real `rc_exec_run` and also asserts a short command is NOT refused, so a guard that refuses everything fails too.
Fixed: 01.4 A WORK DIRECTORY OVER 127 BYTES WAS TRUNCATED THEN CREATED AND MIRRORED INTO -- `cur_work` is `MAX_PATH` (optmenu.h) and both `rc_strf` returns are checked; `rc_workdir_apply` refuses a path that does not fit rather than `rc_mkpath`-ing a truncated one.
Fixed: 01.5 HELLO COULD CARRY UNINITIALISED STACK AS THE MACHINE'S PATH -- `GetEnvironmentVariableA` returns the REQUIRED size when the buffer is too small and leaves it undefined, so both sites now range-check (`n == 0 || n >= sizeof(buf)`) instead of testing non-zero. A PATH too long to read is reported as absent, with a log line, rather than invented.
Fixed: 01.6 `do_put` IGNORED `rc_strf` ON A WIRE-SUPPLIED LENGTH -- checked, inside `rc_syncrpc_put_path`, and the two remaining `rc_sync_join` returns are checked too: `do_putend`'s compare-and-swap and `rc_sync_commit`'s own join. The latter also now measures from the INPUTS, which is what `rc_sync_begin_write` has always done and why it was correct (this also closes 05.3 from the sync report).
Fixed: 01.7 EVERY COMPRESSED FRAME COST A FIXED 256KB HEAP BLOCK -- one connection-lifetime scratch (`g_inf_scratch`, heap not BSS, `2 * RC_MAX_FRAME`), then an allocation of exactly the inflated size. Per-frame cost is now the real size; peak concurrent cost is one scratch. `RC_MAX_INFLIGHT` 256 -> 64, so the cap is derived from the memory budget (64 x 64KB = 4MB worst case) rather than from nowhere.
Fixed: 01.8 AN INTERRUPTED EXPORT WEDGED File > Export FOR THE PROCESS -- `g_export->give_up()` is called from the disconnect branch beside `rc_syncrpc_abort()`, which had the same latch and was the only one given the treatment. `give_up` now reports how far it got rather than asserting "before any data arrived", which was only true of its first caller.
Fixed: 01.9 `pump()` RE-ENTERED THE CONNECTION MACHINERY FROM INSIDE THE COPY WALK -- it now dispatches the progress dialog's own messages and repaints for everything else, defers the rest by re-posting (bounded, so it cannot spin on its own re-posts), and re-posts `WM_QUIT` instead of swallowing it.
Fixed: 01.10 "CANCEL" ON A PER-FILE COPY ERROR WAS DOWNGRADED TO "CARRY ON" -- `on_error` sets `r->p->cancelled` on IDABORT, which is the flag the caller actually tests. Aborting at file 6 of 200 no longer changes the work directory and reports "5 copied, 1 failed" with 194 unmentioned.
Fixed: 01.11 A MISSED `rc_rpc_join` DEADLINE DISABLED exec AND sync SILENTLY -- `g_running = 1` moved above the `if (g_thread) return 0;` early return, the same one-line reordering 01.48 made for `g_net` on the line above.
Fixed: 01.12 `to_bstr` SUBSTITUTED AN EMPTY STRING ON OOM -- it returns NULL and logs. Because `put_innerHTML(NULL)` CLEARS rather than fails, `live()`, `status()` and `clear()` check explicitly; `append()` refuses with a log line. Returning NULL alone would have been half a fix, and the half that mattered least.
Fixed: 01.13 THE BAR'S STATIC ATE `&` -- `SS_NOPREFIX`. `rm -rf build && make` now displays as itself. One flag, and the bar's whole job is that what is shown is what is approved.
Fixed: 01.14 THE SYNC ROOT WAS READ UNLOCKED AND NOT CAPTURED PER TRANSFER -- `g_root` is guarded by a critical section with snapshot readers (holding it across file I/O would put the UI thread behind a floppy), and `g_put.root` captures the root at `off == 0` so `do_putend` commits against the tree the transfer began in. The straddling case needed no race at all.
Fixed: 01.15 A FAILED `DoVerb`/`QueryInterface` WEDGED THE TRANSCRIPT PERMANENTLY -- `boot_failed()` clears `doc_len`/`doc_sum` and drops `obj`, so the identical skeleton the bridge re-sends on the next attach retries instead of matching a fingerprint for a document that was never installed.
Fixed: 01.16 `view.htm` WAS WRITTEN WITH UNCHECKED `fputs`/`fclose` -- all three returns checked, each calling `boot_failed()`. A full disk or a read-only exe directory is now a retry on the next attach rather than a blank window for the session.
Fixed: 01.17 `rc_net_send` BLOCKED THE UI THREAD FOR FIVE SECONDS AND CALLED A FULL QUEUE "THE CONNECTION IS DOWN" -- the return distinguishes `RC_SEND_FULL` from `RC_SEND_DEAD`, `rc_net_send_ui` gives user gestures a 0.5s budget instead of 5s, and `ui_send` says which happened. "A file transfer is using the connection, try again in a moment" is true and actionable; the old message sent the reader to look at the network.
Fixed: 01.18 A LOST OPTIONSEND BROKE BATCH-START DETECTION PERMANENTLY -- the reset triggers on the first row of ANY kind, `rc_optmenu::reset()` is called from the disconnect branch, and the `work` branch got the `< MAX_OPTS` cap the other four always had.
Fixed: 01.19 `WM_SETREDRAW(FALSE)` LATCHED IF SESSIONSEND NEVER ARRIVED -- `rc_chats::abort_rows()`, called from the same disconnect branch. The pixels and the backing array agree again, which is the invariant `open_selected` indexes on.
Fixed: 01.20 `rc_rpc_join` CLOSED THE WATCHER HANDLE BEFORE THE JOIN -- moved after. On a timeout the handle is deliberately left alone, because the worker is still running and still owns it. This module already stated the rule 495 lines earlier as the reason NOT to do it.
Fixed: 01.21 `fclose` BEFORE THE EXPORT RENAME WAS UNCHECKED -- checked. `got == total` counts bytes handed to `fwrite`, not bytes on disk, so this was the only thing standing between a full floppy and a truncated file wearing the name the user chose.
Fixed: 01.22 `Gaston-Replaced` OVERWROTE ITS OWN PREVIOUS GENERATION -- `CopyFileA(..., TRUE)` plus `.1`/`.2` generations, the same shape the bridge's conflict staging uses. This was the one finding that destroyed user data on a path the user chose in order to keep it.
Fixed: 01.23 HIDING THE FOCUSED BUTTON DROPPED THE KEYBOARD -- `bar_changed()` returns focus to the input box when the bar comes down. Placed there rather than in bar.cpp because that is the layer that knows where focus belongs.
Fixed: 01.24 `set_autoscroll` RAN ONLY ON THE BRANCH WHERE THE VIEW FAILED TO BOOT -- both calls moved below `rc_pagebar::create` and made unconditional. `autoscroll=0` in the INI now reaches the view. Audit 01.2 was closed twice on this setting; this is the first time the value is actually read.
Fixed: 01.25 `recover_dir` DELETED AND RENAMED AT PATHS FROM UNCHECKED JOINS -- `child` and both joins checked. This runs at the top of every `rc_workdir_apply`, inside the user's own work directory, and one branch is a `DeleteFileA`.
Fixed: 01.26 CANCELLING DURING THE SYNC WAIT TOLD THE USER THE OPPOSITE OF WHAT HAPPENED -- a `proposed` latch set beside the `propose()` call splits the message: cancelled before it, the folder did not change; after it, the folder DID change and only the transfer was interrupted.
Fixed: 01.27 THE CHAT CACHE WAS WRITTEN UNCHECKED AND RENAMED OVER THE GOOD COPY -- `chat_bad` tracks write failures the way `rc_cache::add` already did for a page, `fclose` is part of the verdict, and a failed batch keeps the previous `CHATS.TXT` instead of replacing it with a truncated one. `begin_chats`'s `fopen` failure is logged like every other in the file.
Fixed: 01.28 THE PAGE BAR SWALLOWED FAILURE AND HAD NO LOADING GATE -- routed through `ui_send` like the bar and chat list (the header's claim that it needed no such wrapper was wrong on its own terms), and `command()` is gated on `!loading_page` with Live exempt, since Live is the way back if a page request is lost.
Fixed: 01.29 `make check` AUDITED FOR `W`-SUFFIXED IMPORTS, NOT NT-ONLY ONES -- replaced by `tools/check/imports.py`: a DLL allow-list, an NT-only deny-list seeded from the names this codebase's own comments say it avoids, the W-suffix assertion and the PE 4.0/4.0 gate. **It can fail**, which the three `objdump | grep` pipelines it replaces could not -- they printed and exited 0. `tests/python/meta/test_import_gate.py` proves it goes red for `MoveFileExA`, `SetHandleInformation`, `GetDiskFreeSpaceExA`, `ReadDirectoryChangesW`, `CreateHardLinkA`, `GetFileAttributesExA`, a new DLL and a stray `W`. It also caught a bug in itself during development: the first parse took the field after `<none>`, which is the ORDINAL, so 199 imports came back as 96 "names" and no deny-list entry could ever have matched.
Fixed: 01.30 `do_put` COULD READ PAST THE FRAME ALLOCATION -- the separator must be present, and the bridge's declared length must lie inside the frame. Both are refusals now, covered by the `no-separator` and `length-past-end` cases in `synccheck --putpath`.
Fixed: 01.31 THE COPY PRE-FLIGHT DID NOT RESERVE THE STAGING PREFIX -- `sizeof(RC_WORK_REPLACED)` is charged in `survey_sink`, unconditionally, because whether a given file collides is not known until the destination is walked and a conditional reserve is missing exactly when it is needed.
Fixed: 01.32 `say_id` DROPPED THE MESSAGE SILENTLY ON OOM -- both allocation failures logged. `error()` lands here, so this was the path on which the message explaining a failure was the message that vanished.
Fixed: 01.33 `status_refresh()` RAN ON EVERY INBOUND FRAME -- removed from `render_frame`; `TIMER_STATUS` was always the once-a-second source `rc_html::status`'s comment claimed. The cheap half (`note_rx`) stays. The comment that asserted the untrue frequency is corrected.
Fixed: 01.34 THE NESTING CHECK WAS A STRING COMPARE -- `inside()` now compares the raw strings, the `GetFullPathNameA` forms and the 8.3 short forms, with `GetShortPathNameA` resolved through `GetProcAddress` per the standing rule (confirmed absent from the import table). What it still cannot catch is stated rather than implied: SUBST and network mappings are not resolvable on 9x.
Fixed: 01.35 THE READER-THREAD FAILURE PATH CLEANED UP NOTHING -- it now does what the sender path twelve lines above does. Recovery did save it, but only by depending on an ordering this file records as already having been broken once at the cost of three bridge restarts.
Fixed: 01.36 THE LANE TABLE INVITED `list_windows` ONTO THE READER THREAD -- `frameproto.h` now states the real bar for `IMMEDIATE` (cannot block, cannot re-enter, nothing touching another process's message queue) and says explicitly that `list_windows` is not a member and why, with the note that BACKLOG's "safe from the RPC worker" is true and about a different thread.
Fixed: 01.37 THE TEN SMALLER ONES -- all ten. `goto_n`'s clamp order plus `wire_total`/`wire_page` clamping at the parse site; `net->sess` written only by `main.cpp` through the existing `switched` callback; the four no-`id` drops logged; `fit_label` measuring with the font the row is drawn in; `rc_config_save` returning its verdict and the caller surfacing it; the survey's one side effect documented rather than denied; `tail[]` sized for a `MAX_PATH` destination; `path_for` returning its verdict with all three callers checking; the export `tmp`/`dest` collision refused; and the `HBITMAP` plus two `HMENU`s freed in destructors that were `{}`.
Fixed: 01.38 THE THREE LATENT ONES -- `text[]` sized from `RC_CHAT_LABEL` with an `LB_GETTEXTLEN` guard (LB_GETTEXT takes no size parameter); the four `opt_*` bounds written as `sizeof` rather than a literal `64`; and the `sess`/`chat_ids` 64-byte symmetry documented on both sides as the one invariant that makes their `strcmp` agree.

Fixed: 01.39 (NEW, found ON THE GUEST) THE BAR'S Alt-KEYS WERE SHADOWED BY THE MENU BAR -- pressing Alt+A with a permission prompt up opened the **Commands** menu, not Allow. `bar.cpp` said "A/L/D avoid the menu bar's F/C/M/E/H", which was true when written and stopped being true when `optmenu.cpp` added "Mo&de" and "Comm&ands" -- taking D and A. A menu-bar mnemonic beats a child button's, so two of the bar's three answers were unreachable from the keyboard. This mattered more because of 01.1: removing the automatic focus grab is only safe if the keyboard route left behind works. Now `A&llow`/`Al&ways`/`De&ny` (L/W/N), with the descriptor table carrying the `&` too because `show()` writes `primary` over the caption on every push. `tests/python/meta/test_mnemonics.py` fails on the collision, and would have caught it: reverting the captions makes it name both `{'A': ... Comm&ands, 'D': ... Mo&de}`. **Verified on the guest**: Alt+L logged `permission allow for Write`.

## The 32 new tests

- `tests/python/guest/test_guest_agreement.py` (+14) -- `synccheck --putpath` drives the REAL `rc_syncrpc_put_path` over bodies shaped as the bridge builds them, with file content glued to the path. Ordinary `//`, `http://` and `/../` bodies must parse; escapes, absolute paths and drive specs must still be refused; the no-separator and length-past-end cases must refuse; and the data offset must be where the content starts.
- `tests/guest/rpccheck.c` (+3 assertions) -- `rc_exec_run` refuses a 4095-byte command and an 1100-byte shell command as `too_long`, and does NOT refuse a short one.
- `tests/python/meta/test_import_gate.py` (+11) -- the import gate goes red for six NT-only symbols, a new DLL and a stray W-suffix; stays green for the real binary and the two accepted W imports; and is checked for parsing symbol names rather than ordinals.
- `tests/python/meta/test_mnemonics.py` (+5) -- no bar button shares an Alt-key with a menu-bar entry, the menu bar has no internal collision, the descriptor table keeps its mnemonics, and both regexes are asserted to match something so the checks cannot pass vacuously.

## Proving the tests would have caught the bugs

A test written after a fix proves nothing until it is shown to fail without it.
Both criticals were reverted in place and re-run:

```
exec guard, with the OLD code:
  FAIL  a 4095-byte command is refused as too_long   got rc=-1 status=2
  FAIL  a 1100-byte shell command is refused as too_long  got rc=-1 status=2
  pass  a short command is not refused as too_long

putpath, with the OLD code:
  bad_path      0  cpp-line-comment
  bad_path      0  js-spdx
  bad_path      0  url-in-text
  ok  src/main.c  13  c-block-comment
```

and the mnemonic test, with the old captions:

```
FAILED test_no_bar_button_is_shadowed_by_the_menu_bar
  {'A': (['button &Allow'], ['optmenu.cpp: Comm&ands']),
   'D': (['button &Deny'],  ['optmenu.cpp: Mo&de'])}
```

## On the guest

Run on `retroclaude-win98` against an isolated bridge, with the build shipped
through `tools/vm/vmship.sh`:

- **The image loads.** It connected and reported itself (`Windows 4.10 build 2222`, 31MB, 800x600x16). An NT-only import would have killed it before `WinMain` with no log, so this is the gate `make check` now enforces statically and this confirms dynamically.
- **01.1** -- with a permission prompt up, typing landed in the input box and the bar stayed unpressed.
- **01.39** -- Alt+L pressed Allow (`bridge.permdesk: permission allow for Write`).
- **01.2** -- the agent wrote a `.js` file with a `//` comment; the bridge logged `pushed: 1`, and `TYPE C:\RCWORK\CREATEAF\HELLO.JS` on the guest printed it back, 68 bytes, long filename intact.
- **01.23** -- after the bar came down, the caret was back in the input box.
- The turn completed normally (`done 34s $0.0557`), the transcript rendered, the chat list renamed the conversation, and a reconnect mid-test correctly re-issued the pending permission prompt.

## What this cost

`total committed at load` 363,580 -> 372,060 bytes (+8,480), from `cur_work`
widening to `MAX_PATH`, the per-transfer root capture and the new buffers.
Against that, the largest transient demand in the program is gone: a streamed
answer no longer holds a 256KB block per in-flight compressed frame, and the
in-flight cap fell from 256 to 64.
