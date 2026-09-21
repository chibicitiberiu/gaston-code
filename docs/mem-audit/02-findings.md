# Defects found

> **Status, 2026-09-21 evening.** Nine of the fifteen are **fixed**: F1, F2,
> F3, F5, F6, F7, F8, L1, L5. Two carry new regression tests, and the
> difference between them is worth reading:
>
> - **F1** — `rpccheck --putend`, driven through the real `rc_syncrpc_handle`.
>   Verified to FAIL against the bug (the temp survives the refusal), so it
>   discriminates.
> - **F2** — `synccheck --commit`, four cases. Verified NOT to fail against
>   the bug, which is recorded below because it changes what F2 is worth.
>
> Left alone deliberately: F4 (an armed trap, not a live bug), F9 (an
> undeclared invariant), L2, L3, L4, L6. See `04-recommendations.md`.

Ordered by consequence. Every entry says how it was established, because the
three that matter most are the ones a reader should re-check before acting.

**Verified** means I read the code paths myself and traced the control flow.
**Reported** meant it came out of a file-by-file pass without an independent
trace. All six such leads have since been traced; see "The six leads, now verified".

---

## F1. A rejected conditional push leaves its temp file on disk, every time

> **FIXED** — `putend_fail()` in `syncrpc.cpp` deletes by NAME, which is what still exists once the handle is closed. Regression test: `test_a_refused_conditional_push_leaves_no_temp_file`.

**Verified.** `win/sync/syncrpc.cpp`

`do_putend` closes the temp file's handle and invalidates it:

```c
423:    CloseHandle(g_put.h);
424:    g_put.h = INVALID_HANDLE_VALUE;
```

Everything after that is the compare-and-swap block, and every refusal in it
calls `put_fail` — lines 437 (`too_long`), 444 (`changed`), 446 (`locked`),
454 (`changed`), 463 (`changed`). `put_fail`'s cleanup is:

```c
275:    if (g_put.h != INVALID_HANDLE_VALUE && g_put.h != NULL) {
276:        CloseHandle(g_put.h);
277:        DeleteFileA(g_put.tmp);
278:    }
```

The handle is already `INVALID_HANDLE_VALUE`, so the condition is false and
`DeleteFileA(g_put.tmp)` **never runs on any of those five paths**. A
fully-written `.gaston-tmp` is left in the user's sync tree.

This is not a rare corner. The compare-and-swap *is* the normal path for a
conditional push, so an ordinary edit conflict — the user saved the file
between the scan and the push, which is the exact case the mechanism exists
to detect — leaks a temp file every time. `rc_sync_recover()` sweeps them on
the next start, so the symptom is "stray files until restart" rather than
unbounded growth, and `walk()` skips them so they are invisible to the live
scanner.

`win/sync/sync.h:138-140` already records this happening once in the field:
*"the bridge's tree grew `café.md.gaston-tmp` from a put that failed on a
path bug, and nothing ever removed it."* Same outcome, different cause.

**Why this belongs in a report about RAII.** The temp file is one resource
with two release actions — close the handle, delete the file. Line 423 does
half of it early, for a good reason (the commit needs the handle closed), and
in doing so silently disarms the other half, because the cleanup was keyed on
the handle's liveness rather than on the file's existence. A guard owning the
*file*, with an explicit `commit()` to disarm it, makes the whole class
unrepresentable. See R1.

## F2. `rc_sync_commit`'s one failure path that does not clean up

> **FIXED**, and downgraded — see the correction at the end of this entry.

**Verified.** `win/sync/sync.cpp:651`

```c
if (rc_sync_stat_file(tmp, &got) != 0 || got.type != 'f') return -1;
```

Every other failure return in this function deletes the temp first — `-4` at
646, `-2` at 662, `-4` at 686, `-3` at 695. This one does not.

Two things compound here. `rc_sync_stat_file` **always returns 0** — all six
of its returns are `return 0` (sync.cpp:214, 215, 222, 239, 247, 264), with
real status carried in `out->type`. So the `!= 0` half of that condition can
never fire, and the only live half is `got.type != 'f'`. The function reads
as though it checks two things and checks one.

**Correction, after building the regression test.** I first wrote this up as
though any stat failure would strand a temp file. It will not, and the test
proved it: reintroducing the bug leaves all four new cases green. There are
only three ways out of `rc_sync_stat_file` with a non-`'f'` type, and none of
them leaves something the cleanup could have removed — the file is absent
(nothing to delete), it is a directory (`DeleteFileA` cannot remove one), or
another handle holds it with no sharing (`DeleteFileA` fails for the same
reason).

The one case that does leak is narrower: `ReadFile` failing **after** a
successful open (`sync.cpp:55-61`) closes the handle and reports `'u'` with
the file present and nothing holding it — a bad sector, or a network drive
dropping mid-read. Real, reachable in the field, not reachable from a test
without fault injection.

So F2 is a genuine inconsistency among five sibling paths and worth fixing on
those grounds. It is **not** the routine leak F1 is, and the original
write-up above overstated it.

## F3. `inflate_finish()` runs even when the reader thread is still alive

> **FIXED** — both `inflate_finish()` and `WSACleanup()` now sit inside the join gate.

**Verified independently, and by the net/RPC pass.** `win/net/net.cpp:1008`

`rc_net_stop`'s last steps:

```c
1001:    if (rw == WAIT_OBJECT_0 && sw == WAIT_OBJECT_0 && rpcj) {
1005:        if (n->outq_wake) { CloseHandle(n->outq_wake); ... }
1006:        if (n->outq_room) { CloseHandle(n->outq_room); ... }
1007:    }
1008:    inflate_finish();
1009:    WSACleanup();
```

Everything above is gated on all three threads being confirmed joined. That
gating is deliberate and well argued in the surrounding comments: the socket
is *leaked rather than closed* if a thread might still be in `send()`, and
`send_lock` is *leaked rather than deleted* for the same reason —
"a leaked descriptor at process exit costs nothing; one freed under a live
send() is a frame written into whatever the OS handed out next."

`inflate_finish()` is not gated. It calls `mz_inflateEnd(&g_inf)` and
`HeapFree(g_inf_scratch)` — a 131,073-byte block that **only the reader
thread reads or writes**, inside `inflate_body`. If the reader's 5-second
join timed out, that free lands under a live `mz_inflate`. `WSACleanup()` on
the next line tears down Winsock while that same reader may still be in
`recv()` on the socket the code just decided to leak *specifically because*
the reader might still be using it.

The fix is the condition already computed two lines above. `inflate_finish()`
only needs `rw`, since the reader is the only thread involved.

## F4. Two adjacent error paths free the same buffer two different ways

**Verified.** `win/net/net.cpp:243-254`

`rc_frame_parse` takes ownership of `payload` at its *first statement*, not
on success:

```c
frameproto.cpp:73:    memset(out, 0, sizeof(*out));
frameproto.cpp:74:    out->_raw = payload;
```

Every one of its `return -1` paths runs after that, and `rc_frame_free` frees
`_raw`. So in the reader loop:

- parse failure (net.cpp:245) calls `HeapFree(payload)` directly and must
  **not** call `rc_frame_free`;
- inflate failure nine lines later (net.cpp:253) calls `rc_frame_free(f.get())`
  and must **not** also free `payload`.

Both are correct today. They are correct for opposite reasons, nine lines
apart, and the difference is invisible at the call site — it lives in a
callee's first statement. Normalising the first to match the second is a
double free, and normalising the second to match the first is a leak. On 9x a
double free is a reboot, which `net.h`'s ownership rules say in as many words.

Not currently a bug. Listed because it is a trap that is armed, and because a
`HeapPtr<char>` released immediately before the parse call would put the
transfer at the point where it actually happens.

## F5. A reconnect after a failed teardown re-initialises live objects

> **FIXED** — `rc_net` carries a sticky `leaked` flag and `rc_net_start` refuses rather than re-initialising objects a surviving thread still holds.

**Verified.** `win/net/net.cpp:404-411` against `:990-1007`

`rc_net_stop` deliberately skips `DeleteCriticalSection(&n->send_lock)` and
skips closing `outq_wake`/`outq_room` when any thread failed to join. It then
sets `n->started = 0`, so the *next* `rc_net_start` has no way to know it is
looking at a connection whose teardown was abandoned. It runs:

```c
404:    InitializeCriticalSection(&n->send_lock);
408:    n->outq_wake = CreateEventA(NULL, FALSE, FALSE, NULL);
411:    n->outq_room = CreateEventA(NULL, FALSE, FALSE, NULL);
```

unconditionally. That re-initialises a critical section that a surviving
thread may hold, and overwrites two event handles without closing them — an
accumulating handle leak across repeated failed-teardown reconnects.

`net.cpp:413-425` documents the *mirror image* of this bug being fixed (a
failed connect left `started = 0`, so the next attempt re-initialised a live
section). The delete side learned the lesson; the re-init side did not.

## F6. The logger holds its lock across a synchronous disk flush

> **FIXED** — `g_fh` is read and closed under the lock, and the section is no longer deleted. The flush stays inside the lock deliberately; the reasoning is in the code.

**Verified.** `win/core/log.cpp:58-64`

```c
58:    if (g_ready) EnterCriticalSection(&g_lock);
59:    WriteFile(g_fh, stamp, ...);
60:    WriteFile(g_fh, line, ...);
63:    FlushFileBuffers(g_fh);
64:    if (g_ready) LeaveCriticalSection(&g_lock);
```

Four threads log, through 189 call sites in shipping code. `FlushFileBuffers`
is a physical flush on a 200MHz machine with a spinning disk, and it is
inside the lock. A reader thread logging a frame can block the UI thread's
log call for the length of a disk write.

`win/net/net.cpp:649-653` states the principle this violates, about its own
send path: holding a lock across a blocking call "meant the message pump
stopped". The flush is deliberate and right — "a buffered log loses exactly
the lines that matter" — but it does not have to happen with the lock held.

Two smaller races in the same file, both **verified**:

- `g_fh` is read at line 33 outside the lock and closed at line 71 outside
  it. A thread past the check can `WriteFile` on a closed handle; Win9x
  recycles handle values, so that write can land in an unrelated file. This
  is the same hazard `net.cpp:924-936` documents for the socket and solves by
  ordering.
- `rc_log_close` calls `DeleteCriticalSection(&g_lock)` (line 74) without
  establishing that no thread is inside it — the failure mode `net.cpp:950`
  describes as "a GPF dialog at exit, every time."

## F7. `g_home` crosses threads without the lock its neighbour has

> **FIXED** — `rc_syncrpc_home_copy()` copies under `g_root_lock`, replacing the bare-pointer accessor.

**Verified.** `win/sync/syncrpc.cpp:69-86`

`g_root` gets a critical section, a `root_snapshot()` accessor that copies
under it, and thirteen lines of comment explaining why. `g_home`, declared
seventeen lines later in the same file, gets neither:

```c
83:    rc_strf(g_home, sizeof(g_home), "%s", home ? home : "");
86:    const char *rc_syncrpc_home(void) { return g_home; }
```

It is written from the UI thread on options-apply and read while building the
HELLO headers. Same shape, same file, no hardening.

Also **verified**: `rc_syncrpc_root()` (line 79) returns the bare `g_root`
pointer with no lock, so the protected string has an unprotected accessor
sitting next to the protected one.

And `root_lock_init()` (lines 42-48) is a check-then-act on
`g_root_lock_ready` with no synchronisation of its own. Narrow — it only
matters before the first successful init — but the comment claims it is
"called before any thread but the first can reach g_root", and the function
is reachable from both the UI thread and the RPC worker.

## F8. `g_ui_font` is not nulled after it is deleted

> **FIXED** — one line.

**Verified.** `win/app/main.cpp:1951-1956`

```c
if (g_ui_font_bold && g_ui_font_bold != g_ui_font)
    DeleteObject(g_ui_font_bold);
g_ui_font_bold = NULL;
if (g_ui_font) DeleteObject(g_ui_font);
```

`g_ui_font_bold` is nulled; `g_ui_font` is not. Every one of the ten module
pointers above this is deleted and nulled on the next line. Small, and
probably harmless given `PostQuitMessage` follows — but it is the one
asymmetry in an otherwise scrupulous teardown, and it is the kind that a
guard removes rather than a reviewer having to notice.

## F9. The teardown order is a load-bearing, undeclared invariant

**Verified.** `win/app/main.cpp:1931-1950`

`g_html` is deleted **first**. Three live objects hold a non-owning
`rc_html *` — `rc_pagebar` (pagebar.h:72), `rc_input` (input.h:53),
`rc_export` (export.h:42) — and are deleted after it, `g_pagebar` last of all.
For the rest of the teardown, three objects hold a pointer to a destroyed
one.

It is safe today: `~rc_pagebar` (pagebar.h:106) touches only `follow_bmp`,
and the other two destructors do not dereference `html`. Nothing anywhere
declares that they must not. A destructor that logs one line through `html`
would be a use-after-free, and the ordering that prevents it is expressed
only as the sequence of ten `delete` statements.

---

## The six leads, now verified

All six were traced in the code on 2026-09-21. **All six hold.** Three turned
out to be worse or more general than first reported; those differences are
marked.

### L1. `boot_failed()` releases a live control without closing it

> **FIXED** — it now does `Close` + `SetClientSite(NULL)` first, the same sequence as the destructor.

**Verified, and worse than reported.** `win/app/htmlview.cpp:510-517`

```c
static void boot_failed(rc_html_impl *d)
{
    d->doc_len = 0; d->doc_sum = 0; d->ready = 0;
    d->web.reset(NULL);
    d->obj.reset(NULL);
}
```

The destructor at `:292` is careful in exactly the way this is not:

```c
if (obj.valid()) {
    IOleObject_Close(obj.get(), OLECLOSE_NOSAVE);
    IOleObject_SetClientSite(obj.get(), NULL);
}
```

Three of the five `boot_failed` callers — `:564` (`fopen` failed), `:571`
(`fputs` failed), `:576` (`fclose` failed) — sit **before** the
`if (d->obj.valid())` re-navigation branch at `:583`. On a mid-session
document update, when the control is already created and in-place active,
any of those three releases it with neither `Close` nor
`SetClientSite(NULL)`.

**What the original lead missed:** the client site is `&d->host.site`, a
sub-object of `rc_html_impl` itself (`:597`, and again at `:615`). So a
control released without having its site dropped can still be holding a
pointer into the impl — and the impl is what owns the queue and the window.
That is the difference between an untidy teardown and a dangling pointer into
a live object.

Then, because `boot_failed` nulls `obj`, the next successful
`set_document()` fails the `d->obj.valid()` test and falls through to
`CoCreateInstance` — which the comment at `:580-582` names as the thing to
avoid: *"Creating a second control would leak the first and lose the
transcript."*

The two later callers, `:618` and `:626`, are lower risk: `obj` was created
moments earlier in the same call and may never have been activated.

### L2. `cache.cpp` force-closes and orphans the temp file

**Verified.** Four sites, all the same idiom at the top of a function:
`:85` (`open_session`), `:148` (`begin_page`), `:249` (`begin_chats`),
`:298` (`chats_open`).

```c
if (page_fp) { fclose(page_fp); page_fp = NULL; }
```

At `:148` and `:249` the file being force-closed is a half-written `.TMP`
that `end_page`/`end_chats` would have renamed. It is neither renamed nor
deleted, and — unlike every other failure branch in the file — not logged.
Same family as F1: a temp file whose cleanup depends on the caller reaching
the right function in the right order.

### L3. `EnableWindow` pairing in `workdlg.cpp`

**Verified correct today.** `:710`/`:712` are adjacent. `:756`/`:773` are
seventeen lines apart, and the returns at `:735` and `:741` both sit *before*
the `EnableWindow(parent, FALSE)` at `:756`, so no path returns with the
parent disabled.

Worth stating what the failure would cost, because it is not a leak: a missed
`EnableWindow(parent, TRUE)` leaves the application's main window
permanently unclickable, with no modal dialog visible to explain why. The
user's only move is to kill it.

### L4. A `WM_RC_FRAME` in flight at `WM_DESTROY`

**Verified, benign.** `net.cpp:291` posts frames the UI thread frees.
`rc_net_stop` runs from the `WM_DESTROY` handler, after which the pump never
services the queue again, and nothing drains it. Those frames are never
freed — at process exit, where it costs nothing. Real gap in the ownership
contract `net.h:9-20` calls "not negotiable", no practical consequence.

### L5. `TerminateProcess` under `g_child_lock`

> **PARTLY FIXED, and the finding was wrong.** Tracing the start-up order showed `set_child`'s unlocked branch is **unreachable** — `rc_exec_init()` runs at `rpc.cpp:599` and the worker is created at `:612`, both on the UI thread. It is dead defensive code, not "the worst of the three". It now logs if it ever fires, and the ordering guarantee is written down in both `exec.cpp` and `syncrpc.cpp`'s `root_lock_init`.

**Verified, and it generalises.** `exec.cpp:73-75` does call
`TerminateProcess` with the lock held. On its own that is minor.

**What makes it worth listing is the line above it:**

```c
void rc_exec_cancel(void)
{
    g_cancel = 1;
    if (!g_child_lock_ready) return;      /* <-- */
    EnterCriticalSection(&g_child_lock);
```

and `set_child` two lines later:

```c
if (!g_child_lock_ready) { g_child = h; return; }   /* writes UNLOCKED */
```

That is the same unsynchronised check-then-act on a lazy lock-init flag as
`syncrpc.cpp`'s `root_lock_init` (F7) and `log.cpp`'s `g_ready` (F6) — **the
third instance in three different files**, and `set_child`'s fallback is the
worst of the three because it does not skip the work, it does the write
without the lock.

This should be one shared idiom, initialised once at startup from the UI
thread before any worker exists, rather than three hand-rolled lazy inits
that each get it slightly differently wrong.

### L6. Recursion with the parent's find handle still open

**Verified.** `sync.cpp`: `:449` `FindFirstFileA`, `:452` `do {`, `:512`
`walk(root, child, depth + 1, w)`, `:546` `} while (FindNextFileA(h, &fd))`,
`:548` `FindClose(h)`. The recursive call is inside the loop, so every level
holds its handle for the whole of the subtree beneath it. `:436` caps depth
at `RC_SYNC_MAX_DEPTH` (16), giving a worst case of seventeen simultaneously
open find handles and roughly 17.6 KB of stacked frames on the RPC worker's
stack. `recover_dir` (`:722`/`:734`/`:775`) has the identical shape.

`sync.cpp:168-183` records a real invalid-page-fault caused by adding one
directory level to a function in this file.

## What was checked and found sound

Recording this matters as much as the defects, because it says where *not* to
spend effort.

- **`exec.cpp`'s pipe and process handles.** Four pipe handles, two process
  handles, six `CloseHandle` calls across four paths (`:165, 168, 179, 198,
  201, 202, 277, 278, 279`). I traced every path including cancel and
  timeout. Every handle is closed exactly once on every path. Hand-written,
  and correct.
- **Frame ownership across four threads.** `net.h:9-20`'s three-way rule is
  implemented correctly at every site: `f.release()` on both transfer paths,
  no release on the free-here path, and `rc_rpc_route`'s `RC_ROUTE_TAKEN`
  contract honoured by all three of its branches.
- **Lock discipline.** No nested locking anywhere. No hand-written
  `Enter`/`Leave` pair has an early return between the two that skips the
  `Leave`. No lock is held across a blocking call — except F6.
- **Unchecked `new`.** Five factories `return new rc_foo(args...)` with no
  null check, which looks like a defect and is not. `rc_cpp.h:51` makes
  `operator new` return `HeapAlloc`'s result, and with `-xd` a null return
  makes Watcom **skip the constructor** — so `create()` returns NULL cleanly
  and NULL propagates. `main.cpp:1389-1391` says so explicitly, and every use
  site guards. Working as designed.
- **COM in `htmlview.cpp`.** Zero hand-managed `Release` or `SysFreeString`
  outside the guard bodies themselves. The 2026-09-18 conversion did what it
  set out to do.
- **Icons and menus.** Resource icons are deliberately never `DestroyIcon`'d
  (correct on 9x, documented twice); the menu bar is owned by the window and
  cascades. Both look like leaks and are not.
